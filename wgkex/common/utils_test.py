import unittest
from wgkex.common import utils


class UtilsTest(unittest.TestCase):
    def test_mac2eui64_success(self):
        """Verify mac2eui64 can convert mac successfully."""
        ret = utils.mac2eui64("c4:91:0c:b2:c5:a0")
        self.assertEqual("c691:0cff:feb2:c5a0", ret)

    def test_mac2eui64_fails_bad_mac(self):
        """Verify mac2eui64 fails with bad mac address."""
        with self.assertRaises(ValueError):
            utils.mac2eui64("not_a_mac_address")

    def test_mac2eui64_success_with_prefix(self):
        """Verify mac2eui64 succeeds with prefix."""
        ret = utils.mac2eui64("c4:91:0c:b2:c5:a0", "FE80::/10")
        self.assertEqual("fe80::c691:cff:feb2:c5a0/10", ret)

    def test_mac2eui64_fails_bad_prefix(self):
        """Verify mac2eui64 fails with bad prefix."""
        with self.assertRaises(ValueError):
            utils.mac2eui64("c4:91:0c:b2:c5:a0", "not_ipv6_addr")


if __name__ == "__main__":
    unittest.main()
