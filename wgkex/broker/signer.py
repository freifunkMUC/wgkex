import base64
import hashlib
from enum import Enum
from functools import cache
from typing import Optional, Tuple

import ecdsa

from wgkex.config import config

# signify / usign serialization format:

# private key:
# only the second to last 32 bytes are the actual private key
#
# https://github.com/aperezdc/signify/blob/aa90571441df7bca8b7cadf74d36b3190b251061/signify.c#L52-L72
# struct enckey {
# 	uint8_t pkalg[2]; <-- always "Ed" (Ed25519)
# 	uint8_t kdfalg[2]; <-- always "BK" (bcrypt KDF)
# 	uint32_t kdfrounds;
# 	uint8_t salt[16];
# 	uint8_t checksum[8];
# 	uint8_t keynum[KEYNUMLEN]; <-- KEYNUMLEN is 8, also called fingerprint, a random number present in privkey, pubkey and sig
# 	uint8_t seckey[SECRETBYTES];  <-- the first 32 bytes are the private key, the last 32 bytes are the public key
# };
#
# public key:
#
# struct pubkey {
# 	uint8_t pkalg[2];
# 	uint8_t keynum[KEYNUMLEN]; <-- KEYNUMLEN is 8
# 	uint8_t pubkey[PUBLICBYTES]; <-- PUBLICBYTES is 32
# };
#
# signature:
# only the last 64 bytes are the actual signature
#
# struct sig {
# 	uint8_t pkalg[2];
# 	uint8_t keynum[KEYNUMLEN];
# 	uint8_t sig[SIGBYTES]; <-- SIGBYTES is 64
# };


class KeyType(Enum):
    SIGNIFY = 1
    RAW = 2


@cache
def get_private_key() -> Tuple[KeyType, ecdsa.SigningKey, Optional[bytes]]:
    """
    Retrieve the private key from the configuration.

    Returns:
        (KeyType, ecdsa.SigningKey, Optional[bytes]): The type of the private key, the private key object and if KeyType == SIGNIFY the fingerprint.
    """
    if not config.get_config().parker.enabled:
        raise ValueError("Response signing is not available in non-parker mode.")

    privkey_encoded = config.get_config().broker_signing_key
    assert privkey_encoded is not None, "Private key must be set in the configuration."

    try:
        # ecdsautil-style format
        privkey_bytes = bytes.fromhex(privkey_encoded)
    except Exception:
        # signify-style and other base64-encoded formats
        privkey_bytes = base64.b64decode(privkey_encoded)

    keytype: KeyType
    fingerprint_bytes: Optional[bytes] = None
    if len(privkey_bytes) == 104:
        # signify-style format
        fingerprint_bytes = privkey_bytes[32:40]
        privkey_bytes = privkey_bytes[-64:-32]
        keytype = KeyType.SIGNIFY
    elif len(privkey_bytes) == 32:
        # raw format
        keytype = KeyType.RAW
    else:
        raise ValueError(
            f"Invalid private key length for signing key: {len(privkey_bytes)} bytes. Expected 32 (raw) or 104 (signify)."
        )

    privkey = ecdsa.SigningKey.from_string(
        privkey_bytes, curve=ecdsa.Ed25519, hashfunc=hashlib.sha512
    )

    return (keytype, privkey, fingerprint_bytes)


get_private_key()  # Ensure the private key is loaded at startup, and throws an error if it is invalid


def sign_response(data: bytes) -> bytes:
    """
    Sign the response data with the private key from the configuration.

    Arguments:
        data (bytes): The data to be signed.
    Returns:
        (bytes): The base64-encoded signature of the data.
    """
    (keytype, privkey, fingerprint) = get_private_key()
    raw_signature = privkey.sign_deterministic(data, hashfunc=hashlib.sha512)

    if keytype == KeyType.SIGNIFY:
        assert fingerprint is not None, "Fingerprint must be set for SIGNIFY key type."
        # Build the signature struct for the signify-format
        raw_signature = "Ed".encode("utf-8") + fingerprint + raw_signature

    return base64.b64encode(raw_signature)
