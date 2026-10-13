"""PostgreSQL provisioning and CP state store (M6.0a)."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

POSTGRES_ENV = Path("/var/lib/lemonseed/data/secrets/postgres.env")
SECRETS_MODULES = Path("/var/lib/lemonseed/data/secrets/modules")
CP_DB_NAME = "lemonseed_cp"
CP_ROLE = "lemonseed_cp"
SCHEMA_VERSION = 4
# Shape created by ``_create_cp_tables`` (all `CREATE TABLE IF NOT EXISTS`).
# A database with no recorded version is assumed to be at this shape, because
# that is what the table creation immediately preceding the migration run leaves
# behind — on a fresh box *and* on a box upgraded from a pre-migration build.
BASELINE_SCHEMA_VERSION = 3

RESERVED_MODULES = (
    {
        "module_id": "control-plane",
        "db_name": CP_DB_NAME,
        "role_name": CP_ROLE,
    },
    {
        "module_id": "open-webui",
        "db_name": "open_webui",
        "role_name": "open_webui",
    },
)


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _module_secret_path(module_id: str) -> Path:
    safe = module_id.replace("/", "_")
    return SECRETS_MODULES / f"{safe}.env"


def _module_credentials(module_id: str) -> dict[str, str]:
    return _read_env_file(_module_secret_path(module_id))


def postgres_admin_dsn() -> Optional[str]:
    env = _read_env_file(POSTGRES_ENV)
    password = env.get("POSTGRES_PASSWORD")
    user = env.get("POSTGRES_USER", "postgres")
    if not password:
        return None
    host = os.environ.get("LEMONSEED_PG_HOST", "127.0.0.1")
    port = os.environ.get("LEMONSEED_PG_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/postgres"


def pg_isready() -> bool:
    try:
        result = subprocess.run(
            [
                "pg_isready",
                "-h",
                os.environ.get("LEMONSEED_PG_HOST", "127.0.0.1"),
                "-p",
                os.environ.get("LEMONSEED_PG_PORT", "5432"),
                "-U",
                "postgres",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _connect(database: str = "postgres"):
    env = _read_env_file(POSTGRES_ENV)
    password = env.get("POSTGRES_PASSWORD")
    user = env.get("POSTGRES_USER", "postgres")
    if not password:
        raise RuntimeError("postgres.env missing POSTGRES_PASSWORD")
    import psycopg2  # noqa: PLC0415

    conn = psycopg2.connect(
        host=os.environ.get("LEMONSEED_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEMONSEED_PG_PORT", "5432")),
        user=user,
        password=password,
        dbname=database,
    )
    conn.autocommit = True
    return conn


def _write_module_secret(module_id: str, payload: dict[str, str]) -> None:
    SECRETS_MODULES.mkdir(parents=True, exist_ok=True)
    tmp = _module_secret_path(module_id).with_suffix(".tmp")
    lines = [f"{k}={v}" for k, v in payload.items()]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    tmp.replace(_module_secret_path(module_id))


def _ensure_role_and_db(conn, db_name: str, role_name: str, module_id: str) -> str:
    existing = _module_credentials(module_id)
    password = existing.get("DB_PASSWORD") or secrets.token_urlsafe(24)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        role_exists = cur.fetchone() is not None
        if not role_exists:
            cur.execute(
                f'CREATE ROLE "{role_name}" LOGIN PASSWORD %s',
                (password,),
            )
        elif not existing.get("DB_PASSWORD"):
            cur.execute(
                f'ALTER ROLE "{role_name}" PASSWORD %s',
                (password,),
            )
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{db_name}" OWNER "{role_name}"')
        cur.execute(f'GRANT ALL PRIVILEGES ON DATABASE "{db_name}" TO "{role_name}"')
    return password


def _apply_cp_schema(conn) -> int:
    """Create the CP tables, then bring the schema up to ``SCHEMA_VERSION``.

    Returns the version the database is actually at, which is not necessarily
    ``SCHEMA_VERSION`` (see ``migrate_cp_schema`` on a rolled-back appliance).
    """
    _create_cp_tables(conn)
    with conn.cursor() as cur:
        return migrate_cp_schema(cur)


def _create_cp_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cp_meta (
              key TEXT PRIMARY KEY,
              value JSONB NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS module_db (
              module_id TEXT PRIMARY KEY,
              db_name TEXT NOT NULL,
              role_name TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # Edge-auth accounts (M6.2): standard users + groups, many:many.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              username TEXT PRIMARY KEY,
              display_name TEXT NOT NULL DEFAULT '',
              email TEXT NOT NULL DEFAULT '',
              password_hash TEXT NOT NULL,
              disabled BOOLEAN NOT NULL DEFAULT FALSE,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
              name TEXT PRIMARY KEY,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_groups (
              username TEXT NOT NULL REFERENCES users (username) ON DELETE CASCADE,
              name TEXT NOT NULL REFERENCES groups (name) ON DELETE CASCADE,
              PRIMARY KEY (username, name)
            )
            """
        )
        # M6.3b: last-known-good OWUI config snapshot (single slot). Deliberately
        # outside the OWUI database so it survives a broken/wiped OWUI DB.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS owui_config_lkg (
              id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
              policy_hash TEXT NOT NULL,
              owui_version TEXT NOT NULL,
              config_map JSONB NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


@dataclass(frozen=True)
class Migration:
    """One step forward in the CP schema.

    Migrations are **additive only** — `CREATE ... IF NOT EXISTS`, `ADD COLUMN
    IF NOT EXISTS`, never a drop, rename, or type change. Two reasons, both about
    A/B updates: a slot rolled back to an older control plane must still be able
    to open a database written by a newer one, and every statement is re-run
    safely if the version stamp and the schema ever disagree. A destructive
    migration needs a two-release sequence (stop writing the old shape first);
    `test_only_additive_migration_sql` is the guard for that rule.
    """

    version: int
    description: str
    apply: Callable[[Any], None]


def _migrate_v4(cur) -> None:
    """M6.6 D1/D3 — per-user session revocation and last-seen login.

    ``token_generation`` is embedded in the auth token and compared on every
    verify, so bumping it revokes exactly one account (disable, demote, delete,
    logout-everywhere) on its next request. ``last_login`` backs the My Profile
    view. Both have defaults, so existing rows and older control planes are
    unaffected.
    """
    cur.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_generation"
        " INTEGER NOT NULL DEFAULT 0"
    )
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ")


# Append-only, ascending. Bumping SCHEMA_VERSION without registering the step is
# caught by test_migrations_cover_the_declared_version.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(4, "users: token_generation + last_login", _migrate_v4),
)


def _read_schema_version(cur) -> int:
    cur.execute("SELECT value FROM cp_meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None or row[0] is None:
        return 0
    raw = row[0]
    if isinstance(raw, str):
        # jsonb normally comes back as an int; a TEXT-era box returns a string.
        try:
            raw = json.loads(raw)
        except ValueError:
            return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _write_schema_version(cur, version: int) -> None:
    cur.execute(
        """
        INSERT INTO cp_meta (key, value)
        VALUES ('schema_version', %s::jsonb)
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (json.dumps(version),),
    )


def migrate_cp_schema(cur) -> int:
    """Apply pending migrations on an open CP cursor; return the resulting version.

    The stamp is written *after* each step, so a step that raises leaves the
    version behind and gets retried on the next provisioning tick — which is why
    every statement above is idempotent.

    A database *newer* than this control plane is tolerated rather than raised
    on: additive-only migrations keep it readable, and refusing to start would
    turn an A/B rollback into a bricked appliance. Nothing is applied in that
    case, and the recorded version is reported as-is.
    """
    version = _read_schema_version(cur)
    if version == 0:
        version = BASELINE_SCHEMA_VERSION
        _write_schema_version(cur, version)
    if version > SCHEMA_VERSION:
        return version
    for migration in MIGRATIONS:
        if migration.version <= version:
            continue
        migration.apply(cur)
        version = migration.version
        _write_schema_version(cur, version)
    return version


def provision_data_plane() -> dict[str, Any]:
    """Ensure Postgres roles/databases and CP schema exist."""
    if not postgres_admin_dsn():
        return {"state": "not-configured", "reason": "postgres.env missing"}
    if not pg_isready():
        return {"state": "starting", "reason": "PostgreSQL not ready"}

    try:
        admin = _connect("postgres")
        modules: list[dict[str, Any]] = []
        host = os.environ.get("LEMONSEED_PG_HOST", "127.0.0.1")
        port = os.environ.get("LEMONSEED_PG_PORT", "5432")
        for spec in RESERVED_MODULES:
            password = _ensure_role_and_db(
                admin,
                spec["db_name"],
                spec["role_name"],
                spec["module_id"],
            )
            _write_module_secret(
                spec["module_id"],
                {
                    "DATABASE_URL": (
                        f"postgresql://{spec['role_name']}:{password}"
                        f"@{host}:{port}/{spec['db_name']}"
                    ),
                    "DB_NAME": spec["db_name"],
                    "DB_USER": spec["role_name"],
                    "DB_PASSWORD": password,
                },
            )
            modules.append(
                {
                    "module_id": spec["module_id"],
                    "db_name": spec["db_name"],
                    "role_name": spec["role_name"],
                }
            )
        admin.close()

        cp_conn = _connect(CP_DB_NAME)
        schema_version = _apply_cp_schema(cp_conn)
        with cp_conn.cursor() as cur:
            for spec in RESERVED_MODULES:
                cur.execute(
                    """
                    INSERT INTO module_db (module_id, db_name, role_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (module_id) DO UPDATE
                      SET db_name = EXCLUDED.db_name,
                          role_name = EXCLUDED.role_name
                    """,
                    (spec["module_id"], spec["db_name"], spec["role_name"]),
                )
        cp_conn.close()
        return {
            "state": "ready",
            "cp_database": CP_DB_NAME,
            "schema_version": schema_version,
            "modules": modules,
        }
    except Exception as exc:  # noqa: BLE001
        return {"state": "error", "error": str(exc)}


def cp_connect():
    """Connect to the CP state database (lemonseed_cp)."""
    return _connect(CP_DB_NAME)


def cp_get_meta(key: str) -> Any:
    if not postgres_admin_dsn() or not pg_isready():
        return None
    try:
        conn = _connect(CP_DB_NAME)
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM cp_meta WHERE key = %s", (key,))
            row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def cp_set_meta(key: str, value: Any) -> bool:
    if not postgres_admin_dsn() or not pg_isready():
        return False
    try:
        conn = _connect(CP_DB_NAME)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cp_meta (key, value)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (key) DO UPDATE
                  SET value = EXCLUDED.value, updated_at = NOW()
                """,
                (key, json.dumps(value)),
            )
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


# Open WebUI's own database name (see RESERVED_MODULES): the reset window in
# modules.py needs it, and spelling it out here keeps the connection target and
# the statement in one testable place.
OWUI_DB_NAME = "open_webui"


def truncate_owui_config() -> dict:
    """Drop Open WebUI's config table (M6.3b Phase 4B admin reset).

    The narrowest thing that satisfies "forget every setting": ``config`` holds
    OWUI's settings map and nothing else — chats, models, files and accounts all
    live in other tables, so a reset cannot lose user data.

    Uses the postgres superuser (``_connect`` reads admin creds from
    ``postgres.env``) rather than the module role, because TRUNCATE on a
    customer-owned table should not be a right we hand to the app user.
    Never raises: the caller is an admin-triggered flow that must report
    failure, not die. Returns ``{"state": ..., "error": ...}``.
    """
    if not postgres_admin_dsn() or not pg_isready():
        return {"state": "error", "error": "postgres is not available"}
    try:
        conn = _connect(OWUI_DB_NAME)
        try:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE config")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — service boundary; caller reports
        return {"state": "error", "error": str(exc)[:300]}
    return {"state": "ready", "error": None}
