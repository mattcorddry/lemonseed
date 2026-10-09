#!/usr/bin/env python3
"""Unit tests for the Open WebUI API client (mocked HTTP, no server)."""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Optional
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import owui_client  # noqa: E402

BASE = "http://127.0.0.1:18080"


class _Resp:
    """Stand-in for a successful urlopen() response."""

    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.status = status
        self._body = body

    def read(self, _n: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _http_error(code: int, body: Any) -> urllib.error.HTTPError:
    payload = json.dumps(body).encode("utf-8") if not isinstance(body, str) else body.encode("utf-8")
    return urllib.error.HTTPError(
        f"{BASE}/api/v1/test", code, "Err", hdrs=None, fp=io.BytesIO(payload)
    )


def _signin_body(token: str = "jwt-1", role: str = "admin", user_id: str = "cp-1") -> bytes:
    return json.dumps(
        {
            "token": token,
            "token_type": "Bearer",
            "id": user_id,
            "email": "lemonseed-cp@localhost",
            "name": "Lemonseed Control Plane",
            "role": role,
        }
    ).encode("utf-8")


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = owui_client.OwuiClient(BASE + "/")

    def test_get_json_ok(self) -> None:
        resp = _Resp(200, json.dumps({"status": True}).encode("utf-8"))
        with mock.patch.object(owui_client, "urlopen", return_value=resp) as opener:
            out = self.client.get("/health")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["data"], {"status": True})
        self.assertIsNone(out["error"])
        # Trailing slash on base_url must not double up or vanish.
        self.assertEqual(opener.call_args.args[0].full_url, f"{BASE}/health")

    def test_non_json_body_survives_as_text(self) -> None:
        with mock.patch.object(owui_client, "urlopen", return_value=_Resp(200, b"Success")):
            out = self.client.get("/api/v1/auths/signout")
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"], "Success")

    def test_http_error_surfaces_detail(self) -> None:
        err = _http_error(403, {"detail": "Unauthorized"})
        self.addCleanup(err.close)
        with mock.patch.object(owui_client, "urlopen", side_effect=err):
            out = self.client.get("/api/v1/users/all")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 403)
        self.assertEqual(out["error"], "Unauthorized")

    def test_http_error_pydantic_422_detail_list(self) -> None:
        detail = [{"loc": ["body", "email"], "msg": "field required"}]
        err = _http_error(422, {"detail": detail})
        with mock.patch.object(owui_client, "urlopen", side_effect=err):
            out = self.client.post("/api/v1/auths/signin", json_body={})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 422)
        self.assertIn("field required", out["error"])

    def test_url_error_never_raises(self) -> None:
        with mock.patch.object(
            owui_client, "urlopen", side_effect=urllib.error.URLError("connection refused")
        ):
            out = self.client.get("/health")
        self.assertFalse(out["ok"])
        self.assertIsNone(out["status"])
        self.assertIn("connection refused", out["error"])

    def test_timeout_surfaces_as_error(self) -> None:
        with mock.patch.object(owui_client, "urlopen", side_effect=TimeoutError()):
            out = self.client.get("/health")
        self.assertFalse(out["ok"])
        self.assertIsNone(out["status"])
        self.assertIn("TimeoutError", out["error"])

    def test_health_probes_are_unauthenticated(self) -> None:
        calls: list[Any] = []

        def opener(req: Any, timeout: Optional[float] = None) -> _Resp:
            calls.append(req)
            return _Resp(200, b'{"status": true}')

        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            self.client.health()
            self.client.ready()
            self.client.health_db()
        self.assertEqual(
            [c.full_url for c in calls],
            [f"{BASE}/health", f"{BASE}/ready", f"{BASE}/health/db"],
        )
        self.assertTrue(all(c.get_header("Authorization") is None for c in calls))


class SigninTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = owui_client.OwuiClient(BASE)

    def test_signin_sends_trusted_headers_and_form_body(self) -> None:
        with mock.patch.object(
            owui_client, "urlopen", return_value=_Resp(200, _signin_body())
        ) as opener:
            out = self.client.sign_in_trusted(
                "lemonseed-cp@localhost", name="Lemonseed Control Plane", role="admin"
            )
        req = opener.call_args.args[0]
        self.assertEqual(req.full_url, f"{BASE}/api/v1/auths/signin")
        self.assertEqual(req.method, "POST")
        # urllib stores constructor headers via str.capitalize(); HTTP headers
        # are case-insensitive and uvicorn lowercases on receipt, so the
        # capitalization only matters for how we query the Request here.
        self.assertEqual(req.get_header("X-forwarded-email"), "lemonseed-cp@localhost")
        self.assertEqual(req.get_header("X-forwarded-name"), "Lemonseed Control Plane")
        self.assertEqual(req.get_header("X-forwarded-role"), "admin")
        # Empty groups must not send the header (empty header = wipe attempt).
        self.assertIsNone(req.get_header("X-forwarded-groups"))
        # SigninForm validates email+password even though the trusted-header
        # branch ignores them — the body must carry both fields.
        body = json.loads(req.data)
        self.assertEqual(body["email"], "lemonseed-cp@localhost")
        self.assertTrue(body["password"])
        self.assertTrue(out["ok"])

    def test_signin_stores_token_and_user(self) -> None:
        with mock.patch.object(owui_client, "urlopen", return_value=_Resp(200, _signin_body())):
            self.client.sign_in_trusted("lemonseed-cp@localhost", role="admin")
        self.assertEqual(self.client.token, "jwt-1")
        self.assertEqual(self.client.user["id"], "cp-1")
        self.assertEqual(self.client.user["role"], "admin")

    def test_signed_requests_send_bearer(self) -> None:
        with mock.patch.object(owui_client, "urlopen", return_value=_Resp(200, _signin_body())):
            self.client.sign_in_trusted("lemonseed-cp@localhost", role="admin")
        with mock.patch.object(
            owui_client, "urlopen", return_value=_Resp(200, b"[]")
        ) as opener:
            out = self.client.get("/api/v1/users/all")
        self.assertTrue(out["ok"])
        self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "Bearer jwt-1")

    def test_401_reauths_once_then_retries(self) -> None:
        # Establish a session with token jwt-1.
        with mock.patch.object(owui_client, "urlopen", return_value=_Resp(200, _signin_body())):
            self.client.sign_in_trusted("lemonseed-cp@localhost", role="admin")

        seq: list[Any] = [
            _http_error(401, {"detail": "invalid token"}),  # authorized call, stale JWT
            _Resp(200, _signin_body(token="jwt-2")),        # transparent re-sign-in
            _Resp(200, b'[{"id": "u1"}]'),                  # retry of original call
        ]
        calls: list[str] = []

        def opener(req: Any, timeout: Optional[float] = None) -> _Resp:
            calls.append(req.full_url)
            item = seq.pop(0)
            if isinstance(item, urllib.error.HTTPError):
                raise item
            return item

        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            out = self.client.get("/api/v1/users/all")
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"], [{"id": "u1"}])
        self.assertEqual(
            calls,
            [
                f"{BASE}/api/v1/users/all",
                f"{BASE}/api/v1/auths/signin",
                f"{BASE}/api/v1/users/all",
            ],
        )
        self.assertEqual(self.client.token, "jwt-2")

    def test_401_without_reauthable_identity_does_not_loop(self) -> None:
        client = owui_client.OwuiClient(BASE)
        with mock.patch.object(
            owui_client, "urlopen", side_effect=_http_error(401, {"detail": "no session"})
        ) as opener:
            out = client.get("/api/v1/users/all", auth=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 401)
        opener.assert_called_once()

    def test_401_when_resignin_also_fails_returns_original_error(self) -> None:
        with mock.patch.object(owui_client, "urlopen", return_value=_Resp(200, _signin_body())):
            self.client.sign_in_trusted("lemonseed-cp@localhost", role="admin")
        seq: list[Any] = [
            _http_error(401, {"detail": "invalid token"}),
            _http_error(403, {"detail": "Action prohibited"}),  # re-sign-in fails
        ]

        def opener(req: Any, timeout: Optional[float] = None) -> _Resp:
            item = seq.pop(0)
            if isinstance(item, urllib.error.HTTPError):
                raise item
            return item

        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            out = self.client.get("/api/v1/users/all")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 401)
        self.assertEqual(out["error"], "invalid token")
        self.assertEqual(seq, [])

    def test_failed_signin_keeps_client_unauthenticated(self) -> None:
        err = _http_error(400, {"detail": "Invalid trusted header"})
        self.addCleanup(err.close)
        with mock.patch.object(owui_client, "urlopen", side_effect=err):
            out = self.client.sign_in_trusted("ghost@localhost")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 400)
        self.assertIn("trusted header", out["error"])
        self.assertIsNone(self.client.token)
        self.assertEqual(self.client.user, {})

    def test_session_user_hits_auths_root(self) -> None:
        with mock.patch.object(
            owui_client, "urlopen", return_value=_Resp(200, b'{"id": "cp-1"}')
        ) as opener:
            out = self.client.session_user()
        self.assertTrue(out["ok"])
        self.assertEqual(opener.call_args.args[0].full_url, f"{BASE}/api/v1/auths/")

    def test_refresh_without_identity_is_a_structured_error(self) -> None:
        client = owui_client.OwuiClient(BASE)
        with mock.patch.object(owui_client, "urlopen") as opener:
            out = client.refresh_session()
        self.assertFalse(out["ok"])
        self.assertIsNone(out["status"])
        self.assertIn("identity", out["error"])
        opener.assert_not_called()


# ---------------------------------------------------------------------------
# M6.3b Phase 3: api-map routing + sk- machine key lifecycle
# ---------------------------------------------------------------------------


def _scripted(responses: list[Any]) -> Any:
    """Ordered fake urlopen: pops one responder per call, records requests."""
    calls: list[Any] = []

    def opener(req: Any, timeout: Optional[float] = None) -> Any:
        calls.append(req)
        item = responses.pop(0)
        if isinstance(item, Exception):  # mock only auto-raises direct side_effects
            raise item
        if callable(item):
            return item(req)
        return item

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


def _bearer(req: Any) -> Optional[str]:
    return req.get_header("Authorization")


class ApiMapTests(unittest.TestCase):
    def test_manifest_map_wins_over_defaults(self) -> None:
        client = owui_client.OwuiClient(BASE, api_map={"users.all": "GET /api/v2/people"})
        opener = _scripted([_Resp(200, b"[]")])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            out = client.call("users.all")
        self.assertTrue(out["ok"])
        self.assertEqual(opener.calls[0].full_url, f"{BASE}/api/v2/people")

    def test_missing_names_fall_back_to_default_api(self) -> None:
        client = owui_client.OwuiClient(BASE, api_map={"custom.thing": "GET /x"})
        opener = _scripted([_Resp(200, b"[]")])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            client.call("models.export")  # not in api_map -> DEFAULT_API
        self.assertEqual(opener.calls[0].full_url, f"{BASE}/api/v1/models/export")

    def test_unknown_api_name_is_structured_error(self) -> None:
        client = owui_client.OwuiClient(BASE)
        with mock.patch.object(owui_client, "urlopen") as opener:
            out = client.call("nope.nope")
        self.assertFalse(out["ok"])
        self.assertIn("unknown api name", out["error"])
        opener.assert_not_called()

    def test_unfilled_path_param_is_structured_error(self) -> None:
        client = owui_client.OwuiClient(BASE)
        with mock.patch.object(owui_client, "urlopen") as opener:
            out = client.call("users.update", json_body={"role": "user"})
        self.assertFalse(out["ok"])
        self.assertIn("user_id", out["error"])
        opener.assert_not_called()

    def test_path_params_are_url_quoted(self) -> None:
        client = owui_client.OwuiClient(BASE)
        opener = _scripted([_Resp(200, b"{}")])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            client.user_update("wee id/x", {"role": "user"})
        self.assertIn("/users/wee%20id%2Fx/update", opener.calls[0].full_url)
        self.assertEqual(json.loads(opener.calls[0].data), {"role": "user"})

    def test_config_import_wraps_payload(self) -> None:
        client = owui_client.OwuiClient(BASE)
        opener = _scripted([_Resp(200, b"{}")])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            client.config_import({"ollama.enable": False})
        self.assertEqual(
            json.loads(opener.calls[0].data), {"config": {"ollama.enable": False}}
        )

    def test_app_version_is_unauthenticated(self) -> None:
        client = owui_client.OwuiClient(BASE, api_key="sk-whatever")
        opener = _scripted([_Resp(200, b'{"version": "0.11.0"}')])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            out = client.app_version()
        self.assertEqual(out["data"]["version"], "0.11.0")
        self.assertIsNone(_bearer(opener.calls[0]))

    def test_models_sync_refuses_without_full_management(self) -> None:
        client = owui_client.OwuiClient(BASE)

        def _no_http(req: Any, timeout: Optional[float] = None) -> Any:
            self.fail("models.sync must not reach the network when refused")

        with mock.patch.object(owui_client, "urlopen", side_effect=_no_http):
            out = client.models_sync([{"id": "x"}], full_management=False)
        self.assertFalse(out["ok"])
        self.assertIn("full_management", out["error"])

    def test_models_sync_fires_under_full_management(self) -> None:
        client = owui_client.OwuiClient(BASE)
        opener = _scripted([_Resp(200, b"[]")])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            out = client.models_sync([{"id": "x"}], full_management=True)
        self.assertTrue(out["ok"])
        self.assertEqual(opener.calls[0].full_url, f"{BASE}/api/v1/models/sync")


class SkKeyLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.persisted: list[str] = []
        self.client = owui_client.OwuiClient(BASE, on_remint=self.persisted.append)

    def _signed_in(self) -> None:
        self.client.sign_in_trusted("lemonseed-cp@localhost")

    def test_mint_stores_notifies_and_uses_bearer(self) -> None:
        opener = _scripted([
            _Resp(200, _signin_body()),          # sign-in
            _Resp(200, b'{"api_key": "sk-1"}'),  # apikey.create
            _Resp(200, b'{"data": []}'),         # users.all
        ])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            self._signed_in()
            key = self.client.mint_api_key()
            self.client.call("users.all")
        self.assertEqual(key, "sk-1")
        self.assertEqual(self.persisted, ["sk-1"])
        self.assertEqual(_bearer(opener.calls[1]), "Bearer jwt-1")   # mint via JWT
        self.assertEqual(_bearer(opener.calls[2]), "Bearer sk-1")    # calls via sk-

    def test_mint_requires_jwt(self) -> None:
        with mock.patch.object(owui_client, "urlopen") as opener:
            self.assertIsNone(self.client.mint_api_key())
        opener.assert_not_called()

    def test_401_with_key_remints_once_and_persists(self) -> None:
        err = _http_error(401, {"detail": "Unauthorized"})
        opener = _scripted([
            _Resp(200, _signin_body()),          # sign-in
            _Resp(200, b'{"api_key": "sk-1"}'),  # initial mint
            err,                                 # users.all with sk-1 -> 401
            _Resp(200, b'{"api_key": "sk-2"}'),  # re-mint over JWT
            _Resp(200, b'[{"id": "u1"}]'),       # users.all retry with sk-2
        ])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            self._signed_in()
            self.client.mint_api_key()
            out = self.client.call("users.all")
        self.assertTrue(out["ok"])
        self.assertEqual(self.client.api_key, "sk-2")
        self.assertEqual(self.persisted, ["sk-1", "sk-2"])
        self.assertEqual(_bearer(opener.calls[2]), "Bearer sk-1")
        self.assertEqual(_bearer(opener.calls[3]), "Bearer jwt-1")  # re-mint via JWT
        self.assertEqual(_bearer(opener.calls[4]), "Bearer sk-2")
        self.assertEqual(len(opener.calls), 5)

    def test_401_after_remint_surfaces_without_looping(self) -> None:
        err = _http_error(401, {"detail": "Unauthorized"})
        opener = _scripted([
            _Resp(200, _signin_body()),
            _Resp(200, b'{"api_key": "sk-1"}'),
            err,  # users.all -> 401
            _Resp(200, b'{"api_key": "sk-2"}'),  # re-mint
            err,  # retry still 401 -> give up (no second re-mint)
        ])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            self._signed_in()
            self.client.mint_api_key()
            out = self.client.call("users.all")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 401)
        self.assertEqual(len(opener.calls), 5)

    def test_key_only_client_cannot_self_heal(self) -> None:
        """A key-only client (no JWT identity on file) surfaces the 401."""
        err = _http_error(401, {"detail": "Unauthorized"})
        client = owui_client.OwuiClient(BASE, api_key="sk-orphan")
        opener = _scripted([err])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            out = client.call("users.all")
        self.assertFalse(out["ok"])
        self.assertEqual(len(opener.calls), 1)

    def test_failed_remint_falls_through_as_error(self) -> None:
        err = _http_error(401, {"detail": "Unauthorized"})
        forbidden = _http_error(403, {"detail": "API key not allowed"})
        opener = _scripted([
            _Resp(200, _signin_body()),
            _Resp(200, b'{"api_key": "sk-1"}'),
            err,       # users.all -> 401
            forbidden,  # re-mint refused (e.g. enable_api_keys off)
        ])
        with mock.patch.object(owui_client, "urlopen", side_effect=opener):
            self._signed_in()
            self.client.mint_api_key()
            out = self.client.call("users.all")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 401)
        self.assertEqual(self.client.api_key, "sk-1")  # stale key retained


if __name__ == "__main__":
    unittest.main()
