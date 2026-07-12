import unittest
from pathlib import Path

from web_admin.routers import items


class SecurityHardeningTests(unittest.TestCase):
    def test_resource_proxy_rejects_local_and_private_targets(self):
        blocked = [
            "http://127.0.0.1/a.jpg",
            "http://localhost/a.jpg",
            "http://10.0.0.1/a.jpg",
            "http://172.16.0.1/a.jpg",
            "http://192.168.1.1/a.jpg",
            "file:///C:/Windows/win.ini",
            "ftp://example.com/a.jpg",
        ]
        for url in blocked:
            with self.subTest(url=url):
                self.assertFalse(items._is_safe_proxy_url(url))

        self.assertTrue(items._is_safe_proxy_url("https://img.example.com/a.jpg"))

    def test_gxcq_adapters_do_not_ship_default_appsecret(self):
        for path in Path("platform_adapters").glob("*gxcq_adapter*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            with self.subTest(path=str(path)):
                self.assertNotIn("PHPCMFA0EF8F01A56FF", text)

    def test_ali_token_helper_does_not_auto_persist_token(self):
        text = Path("_extract_ali_tk_token.py").read_text(encoding="utf-8", errors="replace")

        self.assertIn("--save-env", text)
        self.assertIn("args.save_env", text)
        self.assertNotIn("Auto-save to .env", text)

    def test_web_admin_does_not_use_wildcard_credentialed_cors_or_leak_500_detail(self):
        text = Path("web_admin/main.py").read_text(encoding="utf-8", errors="replace")

        self.assertNotIn('allow_origins=["*"]', text)
        self.assertIn("WEB_ADMIN_CORS_ALLOW_ORIGINS", text)
        self.assertNotIn('"detail": str(exc)', text)

    def test_resource_proxy_keeps_tls_verification_enabled(self):
        text = Path("web_admin/routers/items.py").read_text(encoding="utf-8", errors="replace")

        self.assertNotIn("verify=False", text)
