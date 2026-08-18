import asyncio
import unittest

from security import SessionManager, valid_public_url


class SecurityTests(unittest.TestCase):
    def test_session_is_generated_and_expires(self):
        manager = SessionManager(ttl_seconds=300)
        token, _ = manager.issue()
        self.assertTrue(manager.valid(token))
        self.assertFalse(manager.valid(token + "x"))

    def test_fixed_token_is_operator_only(self):
        manager = SessionManager(fixed_token="operator-secret")
        self.assertTrue(manager.valid("operator-secret"))
        self.assertFalse(manager.valid("wrong"))

    def test_url_shape_rejects_private_targets(self):
        self.assertTrue(valid_public_url("https://cdn.example.test/video.m3u8"))
        self.assertFalse(valid_public_url("http://127.0.0.1/video.m3u8"))
        self.assertFalse(valid_public_url("https://localhost/video.m3u8"))
        self.assertFalse(valid_public_url("https://user:pass@example.com/video.m3u8"))


if __name__ == "__main__":
    unittest.main()
