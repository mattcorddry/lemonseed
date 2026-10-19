"""PostgreSQL provisioning and CP state store (M6.0a)."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

POSTGRES_ENV = Path("/var/lib/lemonseed/data/secrets/postgres.env")
SECRETS_MODULES = Path("/var/lib/lemonseed/data/secrets/modules")
CP_DB_NAME = "lemonseed_cp"
CP_ROLE = "lemonseed_cp"
SCHEMA_VERSION = 6
# Shape created by ``_create_cp_tables`` (all `CREATE TABLE IF NOT EXISTS`).
# A database with no recorded version is assumed to be at this shape, because
# that is what the table creation immediately preceding the migration run leaves
# behind on a fresh box.
#
# M7 P3 folded the shipped v4/v5 steps into the baseline and deleted them
# (USERS-AND-GROUPS.md D-29): there is no install base older than M7, so the
# cheaper move was to write the baseline honestly — including dropping the
# vestigial ``password_hash`` — rather than carry a column nothing reads. The
# cost is stated in D-21: a database stamped below this baseline cannot be
# upgraded, and ``migrate_cp_schema`` says so instead of silently skipping steps.
BASELINE_SCHEMA_VERSION = 5

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
        # ``srp_salt``/``srp_verifier`` are nullable rather than defaulted, which
        # is what lets one column set describe both states: a row with a
        # verifier authenticates with SRP, and a row without one cannot
        # authenticate at all (there is no plaintext fallback to fall back to).
        # There is no password column: bcrypt left with M7 P2, and the server
        # cannot store a value it is never allowed to see.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              username TEXT PRIMARY KEY,
              display_name TEXT NOT NULL DEFAULT '',
              email TEXT NOT NULL DEFAULT '',
              disabled BOOLEAN NOT NULL DEFAULT FALSE,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              token_generation INTEGER NOT NULL DEFAULT 0,
              last_login TIMESTAMPTZ,
              srp_salt TEXT,
              srp_verifier TEXT
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


def _migrate_v6(cur) -> None:
    """M7 P3/P4 — accounts a portal can manage, plus the audit trail.

    All additive, so a slot rolled back within M7 still reads the database.
    Two omissions are forced by ``test_only_additive_migration_sql``, which
    bans the words DELETE and UPDATE in migration SQL:

    * ``group_attributes`` gets **no** ``ON DELETE CASCADE``. Groups are only
      deletable through ``usermgmt``, which removes their attributes in the same
      transaction, so the cascade would be belt-and-braces anyway.
    * no ``INSERT`` seeds the two system groups (the test also requires
      ``IF NOT EXISTS`` on every non-ALTER statement). Seeding stays in the write
      layer, which has to be idempotent regardless.

    ``audit_log`` deliberately has **no** foreign keys: an audit trail that
    disappears when the account it describes is deleted is not an audit trail.
    ``owui_id`` is unique only where non-NULL — a partial index — because a row
    whose OWUI account has not been created yet legitimately has none, and two
    rows must never claim the same OWUI identity (USERS-AND-GROUPS.md D-16a).
    """
    cur.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_required"
        " BOOLEAN NOT NULL DEFAULT FALSE"
    )
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS owui_id TEXT")
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS users_owui_id_key"
        " ON users (owui_id) WHERE owui_id IS NOT NULL"
    )
    cur.execute(
        "ALTER TABLE groups ADD COLUMN IF NOT EXISTS system"
        " BOOLEAN NOT NULL DEFAULT FALSE"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS group_attributes (
          group_name TEXT NOT NULL REFERENCES groups (name),
          key TEXT NOT NULL,
          value JSONB NOT NULL,
          PRIMARY KEY (group_name, key)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
          id BIGSERIAL PRIMARY KEY,
          at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          actor TEXT NOT NULL,
          target TEXT NOT NULL DEFAULT '',
          action TEXT NOT NULL,
          detail JSONB
        )
        """
    )


# Append-only, ascending. Bumping SCHEMA_VERSION without registering the step is
# caught by test_migrations_cover_the_declared_version.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        6,
        "users: password_reset_required + owui_id; groups.system;"
        " group_attributes; audit_log",
        _migrate_v6,
    ),
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


# Columns that mean the database predates what this build can run. A pre-M7 box
# still has ``users.password_hash`` NOT NULL (it was the bcrypt credential), and
# the v6 step is additive — a slot rolled back inside M7 must still read the
# database — so no migration may ever drop it. Every ``usermgmt`` INSERT then
# dies on a not-null violation that names nothing an operator can act on.
UNSUPPORTED_COLUMNS = (("users", "password_hash"),)


def _reject_unsupported_columns(cur) -> None:
    """Refuse to serve a database whose shape this build cannot use.

    The version stamp is only a *proxy* for this hazard, which is why the check
    reads the catalog instead: a box that already ran the v6 step is stamped 6
    and is still broken, so raising the baseline would not catch it — and the
    baseline cannot be raised anyway, because a fresh database relies on the v6
    step for ``group_attributes`` and ``audit_log``.
    """
    for table, column in UNSUPPORTED_COLUMNS:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema()"
            " AND table_name = %s AND column_name = %s",
            (table, column),
        )
        if cur.fetchone() is not None:
            raise ValueError(
                f"CP schema still has {table}.{column}, which predates this"
                " build and no additive migration may drop it — reinstall the"
                " appliance, or on a test box run 'ALTER TABLE"
                f" {table} DROP COLUMN {column}'"
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

    A database *older than the baseline* is the opposite case and raises. The
    steps that would have upgraded it were deleted in D-29, so falling through
    would produce a database missing columns with no error anywhere — and
    because the v4/v5 steps were folded in rather than kept, no pre-M7 database
    can be upgraded at all. ``provision_data_plane`` turns this into a
    ``{"state": "error"}`` report, so the appliance says "reinstall" instead of
    booting into a half-shaped schema.

    Column *shape* is checked before any of that, because it is the real hazard
    and the stamp is only a proxy for it (see ``_reject_unsupported_columns``).
    """
    version = _read_schema_version(cur)
    _reject_unsupported_columns(cur)
    if version == 0:
        version = BASELINE_SCHEMA_VERSION
        _write_schema_version(cur, version)
    elif version < BASELINE_SCHEMA_VERSION:
        raise ValueError(
            f"CP schema version {version} predates the supported baseline "
            f"{BASELINE_SCHEMA_VERSION}; this build cannot upgrade it — reinstall "
            "the appliance"
        )
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
    """Connect to the CP state database (lemonseed_cp).

    The connection is **autocommit** (see :func:`_connect`). Right for reads and
    for single-statement writes; wrong for any change that must land as a unit —
    use :func:`cp_transaction` for those.
    """
    return _connect(CP_DB_NAME)


@contextmanager
def cp_transaction():
    """One explicit CP-database transaction: commit on a clean exit, else rollback.

    ``cp_connect`` hands back an autocommit connection, so a multi-statement
    write written against it is really a series of independent commits. That is
    not a style preference: ``usermgmt`` must be able to say "delete the
    straggler and enroll the new admin, or neither", and D-6's no-null-password
    guarantee depends on it. Restores autocommit before closing so a pooled or
    reused connection never silently inherits transaction mode.
    """
    conn = _connect(CP_DB_NAME)
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except BaseException:  # noqa: BLE001 — rollback then re-raise, whatever it was
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 — best effort after an already-failing session
            pass
        raise
    finally:
        try:
            conn.autocommit = True
        except Exception:  # noqa: BLE001 — cleanup on a connection that may be gone
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — a failed close must not mask the real error
            pass


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
