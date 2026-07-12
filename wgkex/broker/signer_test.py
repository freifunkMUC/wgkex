import base64
import hashlib
import importlib
import sys
import unittest

from wgkex.config import config


def _parker_config(signing_key: str) -> config.Config:
    return config.Config.from_dict(
        {
            "parker": {
                "enabled": True,
                "464xlat": True,
                "ipam": "json",
                "prefixes": {
                    "ipv4": {"clat_subnet": "10.80.96.0/22"},
                    "ipv6": {"length": 63},
                },
            },
            "broker_signing_key": signing_key,
            "domains": [],
            "domain_prefixes": [],
            "mqtt": {"broker_url": "", "username": "", "password": ""},
        }
    )


class TestSigner(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config._parsed_config = _parker_config(bytes(range(32)).hex())
        cls.signer = importlib.import_module("wgkex.broker.signer")

    @classmethod
    def tearDownClass(cls) -> None:
        config._parsed_config = None
        sys.modules.pop("wgkex.broker.signer", None)

    def tearDown(self) -> None:
        self.signer.get_private_key.cache_clear()

    def test_raw_key_signs_verifiable_response(self):
        key_type, private_key, fingerprint = self.signer.get_private_key()
        self.assertEqual(key_type, self.signer.KeyType.RAW)
        self.assertIsNone(fingerprint)

        data = b'{"result":"ok"}\n'
        signature = base64.b64decode(self.signer.sign_response(data))
        self.assertEqual(len(signature), 64)
        self.assertTrue(private_key.verifying_key.verify(signature, data))

    @staticmethod
    def _signify_key(kdfrounds: int = 0, valid_checksum: bool = True) -> bytearray:
        serialized_key = bytearray(104)
        serialized_key[0:2] = b"Ed"
        serialized_key[2:4] = b"BK"
        serialized_key[4:8] = kdfrounds.to_bytes(4, "big")
        serialized_key[32:40] = b"12345678"
        serialized_key[40:72] = bytes(range(32))
        if valid_checksum:
            serialized_key[24:32] = hashlib.sha512(serialized_key[40:104]).digest()[:8]
        return serialized_key

    def test_signify_key_preserves_fingerprint_in_signature(self):
        serialized_key = self._signify_key()
        fingerprint = bytes(serialized_key[32:40])
        config._parsed_config = _parker_config(
            base64.b64encode(serialized_key).decode()
        )
        self.signer.get_private_key.cache_clear()

        key_type, private_key, loaded_fingerprint = self.signer.get_private_key()
        self.assertEqual(key_type, self.signer.KeyType.SIGNIFY)
        self.assertEqual(loaded_fingerprint, fingerprint)

        data = b"response\n"
        signature = base64.b64decode(self.signer.sign_response(data))
        self.assertEqual(signature[:10], b"Ed" + fingerprint)
        self.assertTrue(private_key.verifying_key.verify(signature[10:], data))

    def test_signify_key_rejects_passphrase_protected_key(self):
        serialized_key = self._signify_key(kdfrounds=42)
        config._parsed_config = _parker_config(
            base64.b64encode(serialized_key).decode()
        )
        self.signer.get_private_key.cache_clear()

        with self.assertRaisesRegex(ValueError, "passphrase-protected"):
            self.signer.get_private_key()

    def test_signify_key_rejects_invalid_checksum(self):
        serialized_key = self._signify_key(valid_checksum=False)
        config._parsed_config = _parker_config(
            base64.b64encode(serialized_key).decode()
        )
        self.signer.get_private_key.cache_clear()

        with self.assertRaisesRegex(ValueError, "checksum"):
            self.signer.get_private_key()

    def test_missing_signing_key_raises_clear_error(self):
        previous_config = config._parsed_config
        self.addCleanup(setattr, config, "_parsed_config", previous_config)
        config._parsed_config = _parker_config(None)
        self.signer.get_private_key.cache_clear()

        with self.assertRaisesRegex(ValueError, "broker_signing_key"):
            self.signer.get_private_key()

    def test_signing_is_unavailable_when_parker_is_disabled(self):
        config._parsed_config = config.Config.from_dict(
            {
                "domains": [],
                "domain_prefixes": [],
                "mqtt": {"broker_url": "", "username": "", "password": ""},
            }
        )
        self.signer.get_private_key.cache_clear()

        with self.assertRaisesRegex(ValueError, "non-parker mode"):
            self.signer.get_private_key()


if __name__ == "__main__":
    unittest.main()
