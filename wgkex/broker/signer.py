import base64
import hashlib
from functools import cache

import ecdsa

from wgkex.config import config


@cache
def get_private_key() -> ecdsa.SigningKey:
    """
    Retrieve the private key from the configuration.
    """
    if not config.get_config().parker:
        raise ValueError("Response signing is not available in non-parker mode.")

    privkey_encoded = config.get_config().broker_signature_key
    assert privkey_encoded is not None, "Private key must be set in the configuration."

    try:
        # ecdsautil-style format
        privkey_bytes = bytes.fromhex(privkey_encoded)
    except Exception:
        # signify-style and other base64-encoded formats
        privkey_bytes = base64.b64decode(privkey_encoded)

    if len(privkey_bytes) == 104:
        # signify-style format
        # only the second to last 32 bytes are the actual private key
        #
        # https://github.com/aperezdc/signify/blob/aa90571441df7bca8b7cadf74d36b3190b251061/signify.c#L52-L60
        # struct enckey {
        # 	uint8_t pkalg[2];
        # 	uint8_t kdfalg[2];
        # 	uint32_t kdfrounds;
        # 	uint8_t salt[16];
        # 	uint8_t checksum[8];
        # 	uint8_t keynum[KEYNUMLEN]; <-- KEYNUMLEN is 8
        # 	uint8_t seckey[SECRETBYTES];  <-- the first 32 bytes are the private key, the last 32 bytes are the public key
        # };
        privkey_bytes = privkey_bytes[-64:-32]

    privkey = ecdsa.SigningKey.from_string(
        privkey_bytes, curve=ecdsa.Ed25519, hashfunc=hashlib.sha512
    )
    return privkey


get_private_key()  # Ensure the private key is loaded at startup, and throws an error if it is invalid


def sign_response(data: bytes) -> bytes:
    """
    Sign the response data with the private key from the configuration.

    Arguments:
        data (bytes): The data to be signed.
    Returns:
        (bytes): The base64-encoded signature of the data.
    """
    privkey = get_private_key()
    signature = base64.b64encode(privkey.sign(data))
    return signature
