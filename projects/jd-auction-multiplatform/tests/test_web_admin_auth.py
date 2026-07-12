import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web_admin.config import WebConfig


class WebAdminAuthTests(unittest.TestCase):
    def _make_app(self, config: WebConfig) -> TestClient:
        from web_admin import auth

        auth.init(config)
        app = FastAPI()
        app.middleware("http")(auth.require_auth_middleware)
        app.include_router(auth.router)

        @app.get("/api/private")
        def private_api():
            return {"ok": True}

        return TestClient(app)

    def test_auth_disabled_keeps_local_api_open(self):
        client = self._make_app(WebConfig(auth_enabled=False))

        self.assertEqual(client.get("/api/private").status_code, 200)
        self.assertEqual(client.get("/api/auth/status").json()["enabled"], False)

    def test_auth_enabled_requires_login_for_api(self):
        client = self._make_app(
            WebConfig(auth_enabled=True, admin_username="admin", admin_password="secret")
        )

        self.assertEqual(client.get("/api/private").status_code, 401)

        login = client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["authenticated"], True)
        self.assertEqual(client.get("/api/private").status_code, 200)

    def test_auth_enabled_rejects_bad_password(self):
        client = self._make_app(
            WebConfig(auth_enabled=True, admin_username="admin", admin_password="secret")
        )

        login = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(login.status_code, 401)


class DatabaseResetSafetyTests(unittest.TestCase):
    def test_reset_requires_environment_gate_in_addition_to_cli_confirm(self):
        from jd_mysql_store import require_db_reset_allowed

        with self.assertRaises(SystemExit) as ctx:
            require_db_reset_allowed({})

        self.assertIn("ALLOW_DB_RESET=1", str(ctx.exception))

    def test_reset_gate_accepts_explicit_environment_confirmation(self):
        from jd_mysql_store import require_db_reset_allowed

        require_db_reset_allowed({"ALLOW_DB_RESET": "1"})
