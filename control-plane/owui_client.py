"""Thin Open WebUI API client for the Lemonseed control plane (M6.3b).

Purpose-built for the CP's OWUI integration: service-identity provisioning
now, config/identity reconciliation in later phases (DESIGN.md M6.3b). We
hand-roll a stdlib (urllib) client instead of adopting a third-party lib:
the CP is stdlib-first (AGENTS.md), the calls are synchronous and local, and
``lemond_client.py`` is the in-tree precedent for a bespoke client.

Every call returns a structured dict instead of raising:

    {"ok": bool, "status": int | None, "data": Any | None, "error": str | None}

``status`` is None when the request never completed (refused/timeout), and
``data`` is the decoded JSON body (raw text when the body is not JSON).

Auth model (verified against the Open WebUI v0.11.0 tag): with
``WEBUI_AUTH_TRUSTED_EMAIL_HEADER`` set, POST /api/v1/auths/signin ignores
the JSON credentials and authenticates the identity carried in the trusted
headers, auto-provisioning the user on first sign-in and syncing role and
groups. The CP reaches the slot port directly (127.0.0.1-only) with its own
trusted headers, so it can bootstrap its service identity without passing
through the gateway's auth_request gate. The JWT returned in the sign-in
body is replayed as ``Authorization: Bearer``; on 401 we re-sign-in once,
transparently (covers expiry via the ``auth.jwt_expiry`` config key).

Route names and machine auth (M6.3b Phase 3):

  - ``client.call("config.export")`` resolves verb+path from the manifest's
    ``api`` map (``api_map=`` ctor arg), falling back to ``DEFAULT_API`` which
    mirrors the DESIGN.md day-one table. No route strings at call sites: an
    OWUI pin bump becomes a manifest diff, not a code change.
  - The reconciler authenticates with the CP's ``sk-`` API key (minted once
    over the JWT session, persisted in module secrets). A 401 with the key
    triggers exactly one re-mint via the JWT identity path; a 401 on the JWT
    triggers exactly one re-sign-in. Both paths make the key recoverable by
    construction (endpoint-restriction lockouts included).

Relevant sources at the pinned tag:
  - backend/open_webui/routers/auths.py  (signin trusted-header branch, api_key)
  - backend/open_webui/utils/auth.py     (sk- key endpoint restrictions)
  - backend/open_webui/main.py           (/health, /ready, /health/db, /api/config)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import uuid
from typing import Any, Callable, Optional
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT_SEC = 10.0
# First API calls after boot can be slow (DB pool init, migrations still
# settling); sign-in gets a longer leash than routine probes.
SIGNIN_TIMEOUT_SEC = 20.0
_ERROR_MAX_LEN = 300

# Mirrors the owui-policy.json ``api`` map (DESIGN.md day-one table) so the
# client works before/without a manifest (e.g. the version gate). The manifest
# map wins wherever provided; keeping the two in sync is a manifest concern.
DEFAULT_API: dict[str, str] = {
    "app.version": "GET /api/config",
    "config.export": "GET /api/v1/configs/export",
    "config.import": "POST /api/v1/configs/import",
    "config.namespace": "GET /api/v1/configs/namespace/{namespace}",
    "models.export": "GET /api/v1/models/export",
    "models.list": "GET /api/v1/models/list",
    "models.create": "POST /api/v1/models/create",
    "models.update": "POST /api/v1/models/model/update",
    "models.delete": "POST /api/v1/models/model/delete",
    "models.sync": "POST /api/v1/models/sync",
    "users.all": "GET /api/v1/users/all",
    "users.update": "POST /api/v1/users/{user_id}/update",
    "users.delete": "DELETE /api/v1/users/{user_id}",
    "users.create": "POST /api/v1/auths/add",
    "apikey.create": "POST /api/v1/auths/api_key",
    "apikey.revoke": "DELETE /api/v1/auths/api_key",
    "apikey.get": "GET /api/v1/auths/api_key",
}


def _result(ok: bool, status: Optional[int], data: Any, error: Optional[str]) -> dict[str, Any]:
    return {"ok": ok, "status": status, "data": data, "error": error}


def _read_payload(resp: Any) -> Any:
    """Decode a JSON (or text) body from a response or HTTPError; None if empty."""
    try:
        raw = resp.read()
    except Exception:  # noqa: BLE001 — body is best-effort; status tells the story
        return None
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _error_text(payload: Any, fallback: str) -> str:
    """Best human-readable error from an OWUI error body (FastAPI uses ``detail``)."""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if detail is not None:
            return str(detail)[:_ERROR_MAX_LEN]
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:_ERROR_MAX_LEN]
    return fallback[:_ERROR_MAX_LEN]


class OwuiClient:
    """Synchronous Open WebUI client bound to one slot port (``base_url``).

    Not thread-safe by design: each caller (supervisor hook, reconciler)
    builds its own instance. Instances remember the trusted identity used at
    sign-in so an expired JWT heals with one silent re-sign-in.
    """

    def __init__(
        self,
        base_url: str,
        *,
        email_header: str = "X-Forwarded-Email",
        name_header: str = "X-Forwarded-Name",
        groups_header: str = "X-Forwarded-Groups",
        role_header: str = "X-Forwarded-Role",
        timeout: float = DEFAULT_TIMEOUT_SEC,
        api_map: Optional[dict[str, str]] = None,
        api_key: Optional[str] = None,
        on_remint: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email_header = email_header
        self.name_header = name_header
        self.groups_header = groups_header
        self.role_header = role_header
        self.timeout = timeout
        self.api_map = dict(api_map or DEFAULT_API)
        # sk- machine key (preferred auth when set); on_remint lets the owner
        # persist a freshly minted key (module secrets) the moment it heals.
        self.api_key: Optional[str] = api_key
        self.on_remint = on_remint
        self.token: Optional[str] = None
        # id/email/name/role from the last successful sign-in.
        self.user: dict[str, Any] = {}
        self._identity: Optional[dict[str, str]] = None

    # -- api-map transport ---------------------------------------------------

    def call(
        self,
        name: str,
        *,
        path_params: Optional[dict[str, str]] = None,
        json_body: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Route through the api map: ``call('users.update', path_params=...)``.

        ``kwargs`` pass through to :meth:`request`. Parameterized segments are
        URL-quoted. Unknown names and unfilled parameters return an error
        result (never raise) so the engine's fail-safe rules hold.
        """
        route = self.api_map.get(name) or DEFAULT_API.get(name)
        if not route:
            return _result(False, None, None, f"unknown api name {name!r}")
        method, _, path = route.partition(" ")
        for param, value in (path_params or {}).items():
            path = path.replace("{" + param + "}", urllib.parse.quote(str(value), safe=""))
        if "{" in path:
            missing = path.split("{", 1)[1].split("}", 1)[0]
            return _result(False, None, None, f"missing path param {missing!r} for {name!r}")
        return self.request(method, path, json_body=json_body, **kwargs)

    # -- transport ---------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        auth: bool = True,
        retry_on_401: bool = True,
    ) -> dict[str, Any]:
        """One HTTP call; never raises. See module docstring for the result shape.

        ``json_body`` must be JSON-serializable. With ``auth`` (default) the
        current Bearer token is attached, and a 401 triggers exactly one
        silent re-sign-in + retry when a trusted identity is on file.
        """
        url = f"{self.base_url}{path}"
        try:
            data: Optional[bytes] = None
            req_headers = dict(headers or {})
            if json_body is not None:
                data = json.dumps(json_body).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
            if auth:
                bearer = self.api_key or self.token
                if bearer:
                    req_headers.setdefault("Authorization", f"Bearer {bearer}")
            req = Request(url, data=data, headers=req_headers, method=method)
            with urlopen(req, timeout=timeout or self.timeout) as resp:
                return _result(True, getattr(resp, "status", None), _read_payload(resp), None)
        except urllib.error.HTTPError as exc:
            payload = _read_payload(exc)
            if exc.code == 401 and retry_on_401 and auth and self._heal_auth():
                return self.request(
                    method,
                    path,
                    json_body=json_body,
                    headers=headers,
                    timeout=timeout,
                    auth=auth,
                    retry_on_401=False,
                )
            fallback = f"HTTP {exc.code} {exc.reason}"
            return _result(False, exc.code, payload, _error_text(payload, fallback))
        except (urllib.error.URLError, OSError, TypeError, ValueError) as exc:
            # URLError wraps refused/DNS/SSL; timeouts and resets are OSError.
            name = type(exc).__name__
            return _result(False, None, None, f"{name}: {exc}"[:_ERROR_MAX_LEN])

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json_body: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.request("POST", path, json_body=json_body, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self.request("DELETE", path, **kwargs)

    # -- session -----------------------------------------------------------

    def sign_in_trusted(
        self,
        email: str,
        *,
        name: str = "",
        groups: str = "",
        role: str = "",
    ) -> dict[str, Any]:
        """Sign in via the trusted-header identity (requires trusted-header auth).

        On success stores the JWT for subsequent calls plus ``user``
        (id/email/name/role), and remembers the identity for 401 healing.
        First sign-in auto-provisions the OWUI user; the role header is
        enforced on every sign-in, so re-calling this heals the role.
        """
        headers = {self.email_header: email}
        if name:
            headers[self.name_header] = name
        if groups:
            headers[self.groups_header] = groups
        if role:
            headers[self.role_header] = role
        self._identity = {"email": email, "name": name, "groups": groups, "role": role}
        # SigninForm validates email+password before the trusted-header branch
        # ignores them; a random password can never match a stored hash.
        result = self.request(
            "POST",
            "/api/v1/auths/signin",
            json_body={"email": email, "password": str(uuid.uuid4())},
            headers=headers,
            timeout=SIGNIN_TIMEOUT_SEC,
            auth=False,
        )
        data = result["data"] if isinstance(result["data"], dict) else {}
        if result["ok"] and data.get("token"):
            self.token = str(data["token"])
            self.user = {key: data.get(key) for key in ("id", "email", "name", "role")}
        return result

    def refresh_session(self) -> dict[str, Any]:
        """Re-run the stored trusted sign-in to mint a fresh JWT (401 healing)."""
        if not self._identity:
            return _result(False, None, None, "no trusted identity on file to refresh")
        identity = dict(self._identity)
        email = identity.pop("email")
        return self.sign_in_trusted(email, **identity)

    def session_user(self) -> dict[str, Any]:
        """GET /api/v1/auths/ — verify the current token and fetch the identity."""
        return self.request("GET", "/api/v1/auths/")

    # -- machine key (sk-) lifecycle ------------------------------------------

    def mint_api_key(self) -> Optional[str]:
        """Mint a fresh CP ``sk-`` key over the current JWT session.

        Requires a signed-in admin identity and ``auth.enable_api_keys`` on
        (the reconciler orders the config write before minting). Stores the
        key on this client and hands it to ``on_remint`` for persistence.
        """
        if not self.token:
            return None
        previous = self.api_key
        self.api_key = None  # mint with the JWT, not the stale key
        try:
            result = self.call("apikey.create")
        finally:
            if self.api_key is None:
                self.api_key = previous
        data = result.get("data")
        key = str(data["api_key"]) if isinstance(data, dict) and data.get("api_key") else None
        if key:
            self.api_key = key
            if self.on_remint:
                try:
                    self.on_remint(key)
                except Exception as exc:  # noqa: BLE001 — persistence is best-effort
                    print(f"owui api_key persist: {exc}", flush=True)
        return key

    def _heal_auth(self) -> bool:
        """One recovery attempt for a 401: re-mint the sk- key, else re-sign-in."""
        if self.api_key:
            return bool(self._identity) and bool(self.mint_api_key())
        return bool(self._identity) and bool(self.refresh_session().get("ok"))

    # -- day-one methods (DESIGN.md table; all via the api map) ---------------

    def app_version(self) -> dict[str, Any]:
        """Unauthenticated app config; carries the installed OWUI version."""
        return self.call("app.version", auth=False)

    def config_export(self) -> dict[str, Any]:
        """Full flat key→value config map (portal tree + drift diff)."""
        return self.call("config.export")

    def config_import(self, config_map: dict[str, Any]) -> dict[str, Any]:
        """Upsert a dotted-key map. v0.11.0 requires the ``config`` wrapper."""
        return self.call("config.import", json_body={"config": config_map})

    def config_namespace(self, namespace: str) -> dict[str, Any]:
        return self.call("config.namespace", path_params={"namespace": namespace})

    def models_admin_list(self) -> dict[str, Any]:
        """Admin ground truth (``models/list`` is access-filtered — never it)."""
        return self.call("models.export")

    def model_create(self, model: dict[str, Any]) -> dict[str, Any]:
        return self.call("models.create", json_body=model)

    def model_update(self, update: dict[str, Any]) -> dict[str, Any]:
        return self.call("models.update", json_body=update)

    def model_delete(self, model_id: str) -> dict[str, Any]:
        return self.call("models.delete", json_body={"id": model_id})

    def models_sync(self, models: list[dict[str, Any]], *, full_management: bool) -> dict[str, Any]:
        """Full-replace sync (deletes anything absent from the payload).

        Guard-railed: only legal when the manifest declares the models kind
        ``full_management`` — under selective_management this client refuses
        to fire, whatever the caller believes.
        """
        if not full_management:
            return _result(
                False, None, None,
                "models.sync refused: kind is not full_management",
            )
        return self.call("models.sync", json_body=models)

    def users_all(self) -> dict[str, Any]:
        return self.call("users.all")

    def user_create(self, form: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/v1/auths/add`` (AddUserForm: email/name/role/password)."""
        return self.call("users.create", json_body=form)

    def user_update(self, user_id: str, form: dict[str, Any]) -> dict[str, Any]:
        return self.call("users.update", path_params={"user_id": user_id}, json_body=form)

    def user_delete(self, user_id: str) -> dict[str, Any]:
        """True wipe of the OWUI account. **The reconcile loop must never
        call this** — only the Phase-4 portal member-delete handler (DESIGN)."""
        return self.call("users.delete", path_params={"user_id": user_id})

    def api_key_create(self) -> dict[str, Any]:
        return self.call("apikey.create")

    def api_key_revoke(self) -> dict[str, Any]:
        return self.call("apikey.revoke")

    # -- health probes (unauthenticated in v0.11.0 main.py) -----------------

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/health", auth=False)

    def ready(self) -> dict[str, Any]:
        return self.request("GET", "/ready", auth=False)

    def health_db(self) -> dict[str, Any]:
        return self.request("GET", "/health/db", auth=False)

