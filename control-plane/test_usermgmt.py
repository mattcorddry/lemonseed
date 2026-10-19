#!/usr/bin/env python3
"""Unit tests for usermgmt.py (mocked; no Postgres, no Open WebUI, no GPU).

The fake database is a dict plus a cursor that answers the handful of statements
this module actually issues. It is deliberately not a SQL engine: every question
it can answer is one this module really asks, so a query that changes shape fails
loudly here instead of quietly returning nothing on a real appliance.
"""

from __future__ import annotations

import copy
import io
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any, Optional
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import auth  # noqa: E402
import db  # noqa: E402
import modules  # noqa: E402
import srp  # noqa: E402
import usermgmt  # noqa: E402


def _enrollment(username: str = "alice", password: str = "longenough") -> tuple[str, str]:
    salt, verifier = srp.make_enrollment(srp.SERVER_GROUP, username, password)
    return salt.hex(), f"{verifier:x}"


class _FakeDb:
    """Just enough of the CP database for one test's scenario."""

    def __init__(
        self,
        users: Optional[dict[str, dict[str, Any]]] = None,
        groups: Optional[dict[str, bool]] = None,
        first_admin: str = "",
    ) -> None:
        self.users = users or {}
        self.groups = groups if groups is not None else {"admin": True, "users": True}
        self.first_admin = first_admin
        self.statements: list[tuple[str, tuple]] = []
        self.rowcount = 1
        # When set, every statement reports this rowcount: how a test forces the
        # "the guarded update matched nothing" branch without faking a database.
        self.force_rowcount: Optional[int] = None
        self.attributes: dict[tuple[str, str], Any] = {}

    def set_user(self, username: str, **fields: Any) -> None:
        self.users[username] = {
            "display_name": username.title(),
            "email": "",
            "disabled": False,
            "srp_salt": "salt",
            "srp_verifier": "verifier",
            "password_reset_required": False,
            "owui_id": "",
            "token_generation": 0,
            "groups": ["users"],
            **fields,
        }

    def members(self, group: str) -> list[str]:
        return [name for name, row in self.users.items() if group in row["groups"]]

    def enabled_admins(self) -> list[str]:
        return [
            name for name in self.members("admin") if not self.users[name]["disabled"]
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "users": copy.deepcopy(self.users),
            "groups": dict(self.groups),
            "attributes": dict(self.attributes),
        }

    def restore(self, state: dict[str, Any]) -> None:
        """Undo everything since :meth:`snapshot` — the fake's transaction abort."""
        self.users = state["users"]
        self.groups = state["groups"]
        self.attributes = state["attributes"]

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append((sql, params))
        head = " ".join(sql.split())
        self.rowcount = 1
        self._answer = None

        if "FOR UPDATE" in head:
            self._answer = (1,)
            return
        if head.startswith("SELECT COUNT(*) FROM user_groups"):
            self._answer = (len(self.enabled_admins()),)
            return
        if head.startswith("SELECT display_name, email, disabled"):
            row = self.users.get(params[0])
            if row is None:
                self._answer = None
                return
            self._answer = (
                row["display_name"], row["email"], row["disabled"], row["srp_salt"],
                row["srp_verifier"], row["password_reset_required"], row["owui_id"],
                row["token_generation"],
            )
            return
        if head.startswith("SELECT name FROM user_groups WHERE username"):
            self._answer = [(g,) for g in self.users.get(params[0], {}).get("groups", [])]
            return
        if head.startswith("SELECT 1 FROM user_groups"):
            username, group = params
            joined = group in self.users.get(username, {}).get("groups", [])
            self._answer = (1,) if joined else None
            return
        if head.startswith("SELECT system FROM groups"):
            name = params[0]
            self._answer = (self.groups.get(name),) if name in self.groups else None
            return
        if head.startswith("SELECT 1 FROM groups"):
            self._answer = (1,) if params[0] in self.groups else None
            return
        if head.startswith("INSERT INTO groups"):
            if params[0] in self.groups:
                self.rowcount = 0
            else:
                self.groups[params[0]] = False
            return
        if head.startswith("INSERT INTO users"):
            username = params[0]
            if username in self.users:
                raise RuntimeError("duplicate key value violates unique constraint")
            self.set_user(
                username,
                display_name=params[1],
                email=params[2],
                srp_salt=params[3],
                srp_verifier=params[4],
                # Written as a SQL literal, not a parameter, so the fake reads it
                # out of the statement text the way the server does.
                password_reset_required=head.endswith(", TRUE)"),
                groups=[],
            )
            return
        if head.startswith("INSERT INTO user_groups"):
            username, group = params
            groups = self.users.setdefault(username, self._stub_row())["groups"]
            if group not in groups:
                groups.append(group)
            return
        if head.startswith("UPDATE users SET owui_id"):
            username = params[1]
            if username in self.users and not self.users[username]["owui_id"]:
                self.users[username]["owui_id"] = params[0]
            else:
                self.rowcount = 0
            return
        if head.startswith("UPDATE users SET display_name"):
            self.users[params[1]]["display_name"] = params[0]
            return
        if head.startswith("UPDATE users SET email"):
            self.users[params[1]]["email"] = params[0]
            return
        if head.startswith("UPDATE users SET disabled"):
            self.users[params[1]]["disabled"] = bool(params[0])
            return
        if head.startswith("UPDATE users SET token_generation"):
            self.users[params[0]]["token_generation"] += 1
            return
        if head.startswith("UPDATE users"):
            # rotate_password (4 params: the guarded write) or admin_reset (3).
            if len(params) == 4:
                username, old = params[2], params[3]
            else:
                username, old = params[2], None
            current = self.users.get(username)
            if current is None:
                self.rowcount = 0
                return
            if old is not None and current["srp_verifier"] != old:
                self.rowcount = 0  # the verifier moved under us
                return
            current["srp_salt"] = params[0]
            current["srp_verifier"] = params[1]
            current["password_reset_required"] = "password_reset_required = TRUE" in head
            current["token_generation"] += 1
            return
        if head.startswith("DELETE FROM users"):
            self.rowcount = 1 if self.users.pop(params[0], None) is not None else 0
            return
        if head.startswith("DELETE FROM user_groups"):
            if len(params) == 2:
                username, group = params
                groups = self.users.get(username, {}).get("groups", [])
                if group in groups:
                    groups.remove(group)
            else:
                group = params[0]
                for row in self.users.values():
                    if group in row["groups"]:
                        row["groups"].remove(group)
            return
        if head.startswith("DELETE FROM group_attributes"):
            # delete_group passes one param (all of them), delete_group_attribute
            # two (one key). Both are real; the fake must not guess.
            if len(params) == 2:
                self.attributes.pop((params[0], params[1]), None)
            else:
                for key in [k for k in self.attributes if k[0] == params[0]]:
                    self.attributes.pop(key, None)
            return
        if head.startswith("DELETE FROM groups"):
            self.rowcount = 1 if self.groups.pop(params[0], None) is not None else 0
            return
        if head.startswith("INSERT INTO group_attributes"):
            self.attributes[(params[0], params[1])] = params[2]
            return
        if head.startswith("INSERT INTO audit_log"):
            return
        raise AssertionError(f"usermgmt issued an unexpected statement: {head[:90]}")

    @staticmethod
    def _stub_row() -> dict[str, Any]:
        return {
            "display_name": "", "email": "", "disabled": False, "srp_salt": "",
            "srp_verifier": "", "password_reset_required": False, "owui_id": "",
            "token_generation": 0, "groups": [],
        }

    def fetchone(self) -> Any:
        return getattr(self, "_answer", None)

    def fetchall(self) -> list[Any]:
        answer = getattr(self, "_answer", None)
        return list(answer) if isinstance(answer, list) else (
            [answer] if answer is not None else []
        )


class _Base(unittest.TestCase):
    """Wires a fake database, a fake Open WebUI, and an observing spy network."""

    def setUp(self) -> None:
        self.db = _FakeDb()
        self.owui = mock.MagicMock()
        self.owui.users_all.return_value = {"ok": True, "data": {"users": [], "total": 0}}
        self.owui.user_create.return_value = {"ok": True, "data": {"id": "owui-new"}}
        self.owui.user_delete.return_value = {"ok": True, "data": True}
        self.owui.user_update.return_value = {"ok": True, "data": {"id": "x"}}
        self.reconcile = mock.MagicMock()
        self.audit = mock.MagicMock()
        self.events: list[tuple[str, dict]] = []
        self.first_admin = ""

        @contextmanager
        def _txn():
            # A real transaction aborts on error; the guards are only tested
            # honestly if a raise restores the rows it touched first.
            snapshot = self.db.snapshot()
            try:
                yield self.conn
            except BaseException:
                self.db.restore(snapshot)
                raise

        self.conn = mock.MagicMock()
        self.cursor = mock.MagicMock()
        self.cursor.__enter__ = mock.Mock(return_value=self.cursor)
        self.cursor.__exit__ = mock.Mock(return_value=False)
        self.cursor.execute.side_effect = self._execute
        self.cursor.fetchone.side_effect = self.db.fetchone
        self.cursor.fetchall.side_effect = self.db.fetchall
        self.conn.cursor.return_value = self.cursor

        patches = [
            mock.patch.object(db, "cp_transaction", _txn),
            # Reads and the small single-statement writes (owui_id capture,
            # last_login) use cp_connect, so they need the same fake.
            mock.patch.object(db, "cp_connect", return_value=self.conn),
            mock.patch.object(modules, "owui_admin_client", return_value=self.owui),
            mock.patch.object(modules, "request_policy_reconcile", self.reconcile),
            mock.patch.object(usermgmt.audit, "record", self.audit),
            mock.patch.object(db, "cp_get_meta", self._get_meta),
            mock.patch.object(auth, "pg_ready", return_value=True),
            mock.patch.object(auth, "_pg_ready", return_value=True),
            mock.patch.object(auth, "load_users", self._load_users),
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(mock.patch.stopall)
        usermgmt.register_observer(self._observe)

    def _execute(self, sql: str, params: tuple = ()) -> None:
        """Run the fake, then publish rowcount the way a real cursor does."""
        self.db.execute(sql, params)
        if self.db.force_rowcount is not None:
            self.db.rowcount = self.db.force_rowcount
        self.cursor.rowcount = self.db.rowcount

    def _observe(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    def _get_meta(self, key: str) -> Any:
        if key == usermgmt.FIRST_ADMIN_KEY:
            return {"username": self.first_admin} if self.first_admin else None
        return None

    def _load_users(self) -> list[dict[str, Any]]:
        return [dict(row, username=name) for name, row in self.db.users.items()]

    def statements(self) -> list[str]:
        return [" ".join(sql.split()) for sql, _ in self.db.statements]

    def audit_actions(self) -> list[tuple]:
        return [call.args[:3] for call in self.audit.call_args_list]

    def detail_for(self, action: str) -> dict:
        for call in self.audit.call_args_list:
            if call.args[2] == action:
                return call.args[3] if len(call.args) > 3 else {}
        raise AssertionError(f"no audit row with action {action!r}")


class CreateTests(_Base):
    def test_happy_path(self) -> None:
        salt, verifier = _enrollment()
        result = usermgmt.create(
            "alice", "Alice A", "alice@example.com", salt, verifier, actor="admin"
        )
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["owui"]["ok"])
        self.assertEqual(result["user"]["display_name"], "Alice A")
        # The mirror call carries the derived identity, never the household email.
        body = self.owui.user_create.call_args.args[0]
        self.assertEqual(body["email"], "alice@localhost")
        self.assertNotIn("alice@example.com", str(body))
        self.assertEqual(body["role"], "user")
        self.assertIn("user.create", [a[2] for a in self.audit_actions()])
        self.assertEqual(self.events[0][0], usermgmt.EVENT_CREATED)
        # The captured id lands on the row (D-31: capture at create, never adopt).
        self.assertEqual(self.db.users["alice"]["owui_id"], "owui-new")
        self.assertIn("users", self.db.users["alice"]["groups"])
        self.reconcile.assert_called_once()

    def test_admin_flag_grants_the_admin_group(self) -> None:
        salt, verifier = _enrollment("root-admin")
        usermgmt.create("boss", "The Boss", "", salt, verifier, is_admin=True, actor="a")
        self.assertIn("admin", self.db.users["boss"]["groups"])
        body = self.owui.user_create.call_args.args[0]
        self.assertEqual(body["role"], "admin")

    def test_a_new_account_must_choose_its_own_password_first(self) -> None:
        """D-11/D-13: the admin handed this credential out, so it is not a secret yet.

        The People panel tells the admin "they will be asked to choose their own
        password the first time they sign in". That promise is this column; if it
        is not set, the admin can sign in as the new member and read their chat
        history, and the screen is lying.
        """
        salt, verifier = _enrollment()
        usermgmt.create("alice", "Alice A", "", salt, verifier, actor="admin")
        self.assertTrue(self.db.users["alice"]["password_reset_required"])
        self.assertTrue(usermgmt.get("alice")["password_reset_required"])

    def test_duplicate_username_refused_before_open_webui_is_touched(self) -> None:
        """The ordering is the guard, not the error message.

        Provisioning Open WebUI first would delete the existing account's OWUI row
        on the way to failing on the duplicate username — turning a typo into the
        identity reuse D-16a exists to prevent.
        """
        self.db.set_user("alice")
        salt, verifier = _enrollment()
        with self.assertRaises(ValueError) as ctx:
            usermgmt.create("alice", "Someone Else", "", salt, verifier, actor="a")
        self.assertIn("already exists", str(ctx.exception))
        self.owui.user_create.assert_not_called()
        self.owui.user_delete.assert_not_called()
        self.assertIn("alice", self.db.users)
        self.assertIn("verifier", self.db.users["alice"]["srp_verifier"])

    def test_invalid_fields_are_refused(self) -> None:
        salt, verifier = _enrollment()
        cases = {
            "username": ("1bad",),
            "reserved": ("root",),
            "display name": ("",),
            "email": ("not an address",),
        }
        for label, args in cases.items():
            with self.subTest(field=label):
                if label == "display name":
                    username, name, email = "carol", args[0], ""
                elif label == "email":
                    username, name, email = "carol", "Carol", args[0]
                else:
                    username, name, email = args[0], "Carol", ""
                with self.assertRaises(ValueError, msg=label):
                    usermgmt.create(username, name, email, salt, verifier, actor="a")
        self.owui.user_create.assert_not_called()

    def test_mixed_case_username_is_normalised(self) -> None:
        """D-33: ``Carol`` and ``carol`` are one account, stored lower-cased."""
        salt, verifier = _enrollment("carol")
        usermgmt.create("  Carol  ", "Carol", "", salt, verifier, actor="a")
        self.assertIn("carol", self.db.users)
        self.assertNotIn("Carol", self.db.users)

    def test_bad_srp_pair_is_refused(self) -> None:
        salt, _verifier = _enrollment()
        with self.assertRaises(ValueError):
            usermgmt.create("dave", "Dave", "", salt, "0", actor="a")
        self.owui.user_create.assert_not_called()

    def test_open_webui_absent_still_creates_the_account(self) -> None:
        """A member must not be un-creatable because a module is down (D-18)."""
        salt, verifier = _enrollment()
        with mock.patch.object(modules, "owui_admin_client", return_value=None):
            result = usermgmt.create("erin", "Erin", "", salt, verifier, actor="a")
        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["owui"]["ok"])
        self.assertIn("not running", result["owui"]["reason"])
        self.assertIsNone(self.db.users["erin"]["owui_id"] or None)

    def test_open_webui_failure_is_reported_not_raised(self) -> None:
        salt, verifier = _enrollment()
        self.owui.user_create.return_value = {"ok": False, "error": "EMAIL_TAKEN"}
        result = usermgmt.create("frank", "Frank", "", salt, verifier, actor="a")
        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["owui"]["ok"])
        self.assertIn("EMAIL_TAKEN", result["owui"]["reason"])

    def test_a_create_without_an_id_is_not_success(self) -> None:
        """No id means no future update or delete, so it cannot be 'fine'."""
        salt, verifier = _enrollment()
        self.owui.user_create.return_value = {"ok": True, "data": {}}
        result = usermgmt.create("gina", "Gina", "", salt, verifier, actor="a")
        self.assertFalse(result["owui"]["ok"])
        self.assertIn("did not report its id", result["owui"]["reason"])

    def test_an_existing_open_webui_account_is_deleted_not_adopted(self) -> None:
        """D-16a: adopting would hand the new person the old person's chats."""
        salt, verifier = _enrollment()
        self.owui.users_all.return_value = {
            "ok": True,
            "data": {"users": [{"id": "old-1", "email": "heidi@localhost", "name": "Old"}]},
        }
        result = usermgmt.create("heidi", "Heidi", "", salt, verifier, actor="a")
        self.owui.user_delete.assert_called_once_with("old-1")
        self.assertTrue(result["owui"]["ok"])

    def test_refusal_to_delete_the_orphan_stops_the_create(self) -> None:
        salt, verifier = _enrollment()
        self.owui.users_all.return_value = {
            "ok": True,
            "data": {"users": [{"id": "old-1", "email": "ivan@localhost"}]},
        }
        self.owui.user_delete.return_value = {"ok": False, "error": "cannot delete first user"}
        result = usermgmt.create("ivan", "Ivan", "", salt, verifier, actor="a")
        self.assertFalse(result["owui"]["ok"])
        self.assertIn("refused", result["owui"]["reason"])
        self.owui.user_create.assert_not_called()

    def test_a_failed_list_is_not_read_as_no_orphan(self) -> None:
        """'No such account' and 'the call failed' must not look the same."""
        salt, verifier = _enrollment()
        self.owui.users_all.return_value = {"ok": False, "error": "503"}
        result = usermgmt.create("june", "June", "", salt, verifier, actor="a")
        self.assertFalse(result["owui"]["ok"])
        self.assertIn("could not list", result["owui"]["reason"].lower())
        self.owui.user_create.assert_not_called()


class GuardTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db.set_user("admin", groups=["admin", "users"])
        self.db.set_user("member", groups=["users"])
        self.first_admin = "admin"

    def test_last_admin_cannot_be_demoted(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            usermgmt.set_groups("admin", ["users"], actor="admin")
        self.assertIn("last enabled admin", str(ctx.exception))
        self.assertIn("admin", self.db.users["admin"]["groups"])

    def test_second_admin_may_be_demoted(self) -> None:
        self.db.set_user("admin2", groups=["admin", "users"])
        usermgmt.set_groups("admin2", ["users"], actor="admin")
        self.assertNotIn("admin", self.db.users["admin2"]["groups"])

    def test_demoting_each_other_cannot_empty_the_group(self) -> None:
        """The admin group is row-locked so the two changes serialise (D-2.2)."""
        self.db.set_user("admin2", groups=["admin", "users"])
        usermgmt.set_groups("admin", ["users"], actor="admin2")
        locked = [s for s in self.statements() if "FOR UPDATE" in s]
        self.assertTrue(locked, "admin-group changes must take a row lock")

    def test_last_admin_cannot_be_disabled(self) -> None:
        with self.assertRaises(ValueError):
            usermgmt.set_disabled("admin", True, actor="admin2")
        self.assertFalse(self.db.users["admin"]["disabled"])

    def test_admin_cannot_disable_themself(self) -> None:
        """Kills the two-tab race where one admin demotes the other mid-demote."""
        self.db.set_user("admin2", groups=["admin", "users"])
        with self.assertRaises(ValueError) as ctx:
            usermgmt.set_disabled("admin", True, actor="admin")
        self.assertIn("itself", str(ctx.exception))

    def test_disabled_admin_does_not_count_as_an_admin(self) -> None:
        """The guard counts *enabled* admins, or one disabled one would suffice."""
        self.db.set_user("admin2", groups=["admin", "users"], disabled=True)
        with self.assertRaises(ValueError):
            usermgmt.set_groups("admin", ["users"], actor="admin2")

    def test_unknown_group_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            usermgmt.set_groups("member", ["nope"], actor="admin")

    def test_base_group_is_always_kept(self) -> None:
        """D-3: membership is a stored row, so the write layer guarantees it."""
        usermgmt.set_groups("member", [], actor="admin")
        self.assertIn("users", self.db.users["member"]["groups"])

    def test_disable_bumps_the_generation_and_enable_does_not(self) -> None:
        usermgmt.set_disabled("member", True, actor="admin")
        self.assertEqual(self.db.users["member"]["token_generation"], 1)
        usermgmt.set_disabled("member", False, actor="admin")
        self.assertEqual(self.db.users["member"]["token_generation"], 1)

    def test_no_op_change_reports_unchanged(self) -> None:
        result = usermgmt.set_disabled("member", False, actor="admin")
        self.assertFalse(result["changed"])
        self.assertEqual(self.audit_actions(), [])

    def test_reserved_identity_cannot_be_disabled(self) -> None:
        self.db.set_user("lemonseed-cp", groups=["users"])
        with self.assertRaises(ValueError):
            usermgmt.set_disabled("lemonseed-cp", True, actor="admin")

    def test_missing_account_is_refused_not_silently_ignored(self) -> None:
        for call in (
            lambda: usermgmt.set_display_name("ghost", "Boo", actor="admin"),
            lambda: usermgmt.set_email("ghost", "b@example.com", actor="admin"),
            lambda: usermgmt.set_disabled("ghost", True, actor="admin"),
            lambda: usermgmt.set_groups("ghost", ["users"], actor="admin"),
            lambda: usermgmt.admin_reset_password(
                "ghost", *_enrollment(), actor="admin"
            ),
            lambda: usermgmt.delete("ghost", actor="admin"),
        ):
            with self.assertRaises(ValueError):
                call()


class PasswordTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        salt, verifier = _enrollment("alice", "oldpassword")
        self.old_verifier = verifier
        self.db.set_user(
            "alice", srp_salt=salt, srp_verifier=verifier, groups=["users"], owui_id="o-1"
        )

    def test_rotation_writes_the_new_pair_and_bumps_generation(self) -> None:
        new_salt, new_verifier = _enrollment("alice", "newpassword")
        usermgmt.rotate_password("alice", self.old_verifier, new_salt, new_verifier)
        row = self.db.users["alice"]
        self.assertEqual(row["srp_verifier"], new_verifier)
        self.assertEqual(row["srp_salt"], new_salt)
        self.assertFalse(row["password_reset_required"])
        self.assertEqual(row["token_generation"], 1)  # D-7: other sessions die
        self.assertEqual(self.events[0][0], usermgmt.EVENT_PASSWORD_ROTATED)

    def test_wrong_current_password_leaves_the_pair_byte_identical(self) -> None:
        """D-6: rollback is structural. There is no window with no password."""
        new_salt, new_verifier = _enrollment("alice", "newpassword")
        with self.assertRaises(ValueError):
            usermgmt.rotate_password("alice", "not-the-stored-verifier", new_salt, new_verifier)
        row = self.db.users["alice"]
        self.assertEqual(row["srp_verifier"], self.old_verifier)
        self.assertEqual(row["srp_salt"], self.db.users["alice"]["srp_salt"])
        self.assertEqual(row["token_generation"], 0)
        # Nothing was written at all, not "written then reverted".
        self.assertEqual([s for s in self.statements() if s.startswith("UPDATE users")], [])

    def test_concurrent_change_fails_the_guarded_update(self) -> None:
        """The ``AND srp_verifier = %s`` clause is the whole second-tab story."""
        new_salt, new_verifier = _enrollment("alice", "newpassword")
        self.db.users["alice"]["srp_verifier"] = "changed-elsewhere"
        with self.assertRaises(ValueError):
            usermgmt.rotate_password("alice", self.old_verifier, new_salt, new_verifier)
        self.assertEqual(self.db.users["alice"]["srp_verifier"], "changed-elsewhere")

    def test_zero_rowcount_is_an_error_not_success(self) -> None:
        new_salt, new_verifier = _enrollment("alice", "newpassword")
        self.db.force_rowcount = 0
        with self.assertRaises(ValueError):
            usermgmt.rotate_password("alice", self.old_verifier, new_salt, new_verifier)

    def test_disabled_account_cannot_rotate(self) -> None:
        self.db.users["alice"]["disabled"] = True
        new_salt, new_verifier = _enrollment("alice", "newpassword")
        with self.assertRaises(ValueError):
            usermgmt.rotate_password("alice", self.old_verifier, new_salt, new_verifier)

    def test_admin_reset_sets_the_flag_and_dies_no_sessions_quietly(self) -> None:
        salt, verifier = _enrollment("alice", "onetimepw")
        result = usermgmt.admin_reset_password("alice", salt, verifier, actor="boss")
        self.assertTrue(result["rotate_required"])
        row = self.db.users["alice"]
        self.assertEqual(row["srp_verifier"], verifier)
        self.assertTrue(row["password_reset_required"])  # D-11: no session until rotated
        self.assertEqual(row["token_generation"], 1)  # existing sessions die now

    def test_admin_reset_needs_an_actor(self) -> None:
        """An unattributed privilege escalation is not an audit trail."""
        salt, verifier = _enrollment("alice", "onetimepw")
        with self.assertRaises(ValueError):
            usermgmt.admin_reset_password("alice", salt, verifier)

    def test_admin_reset_does_not_reset_the_flag_it_just_set(self) -> None:
        detail = {"rotate_required": True}
        salt, verifier = _enrollment("alice", "onetimepw")
        usermgmt.admin_reset_password("alice", salt, verifier, actor="boss")
        self.assertEqual(self.detail_for("user.password_reset"), detail)

    def test_admin_reset_refused_for_a_disabled_account(self) -> None:
        self.db.users["alice"]["disabled"] = True
        salt, verifier = _enrollment("alice", "onetimepw")
        with self.assertRaises(ValueError):
            usermgmt.admin_reset_password("alice", salt, verifier, actor="boss")

    def test_the_one_time_password_never_reaches_the_row_or_the_log(self) -> None:
        """What arrives is a verifier. The OTP itself is not in this process.

        Not a fake KAT: the assertion is that no statement and no audit detail
        contains the value the admin generated, because nothing here is given it.
        """
        otp = "abcdefghjkmn"
        salt, verifier = _enrollment("alice", otp)
        usermgmt.admin_reset_password("alice", salt, verifier, actor="boss")
        for sql, params in self.db.statements:
            self.assertNotIn(otp, str(params), sql)
        for call in self.audit.call_args_list:
            self.assertNotIn(otp, str(call.args), call.args)
        # The verifier is a one-way function of it, so it cannot be recovered
        # from what was stored — the point of the whole milestone.
        self.assertNotIn(otp, self.db.users["alice"]["srp_verifier"])


class DeleteTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db.set_user("admin", groups=["admin", "users"], owui_id="o-admin")
        self.db.set_user("member", groups=["users"], owui_id="o-member")
        self.first_admin = "admin"

    def test_deletes_the_open_webui_account_and_the_row(self) -> None:
        result = usermgmt.delete("member", actor="admin")
        self.assertEqual(result["owui"], "deleted")
        self.owui.user_delete.assert_called_once_with("o-member")
        self.assertNotIn("member", self.db.users)
        self.assertEqual(self.events[0][0], usermgmt.EVENT_DELETED)

    def test_open_webui_first_refusal_leaves_the_row_alone(self) -> None:
        """A person with live chats and no portal account is the worst outcome."""
        self.owui.user_delete.return_value = {"ok": False, "error": "first user protected"}
        with self.assertRaises(ValueError) as ctx:
            usermgmt.delete("member", actor="admin")
        self.assertIn("refused", str(ctx.exception))
        self.assertIn("member", self.db.users)
        self.assertEqual([s for s in self.statements() if s.startswith("DELETE FROM users")], [])

    def test_unreachable_open_webui_refuses_the_whole_delete(self) -> None:
        with mock.patch.object(modules, "owui_admin_client", return_value=None):
            with self.assertRaises(ValueError):
                usermgmt.delete("member", actor="admin")
        self.assertIn("member", self.db.users)

    def test_first_admin_row_cannot_be_deleted(self) -> None:
        """Identified by row, not name: the username is whatever setup typed."""
        with self.assertRaises(ValueError) as ctx:
            usermgmt.delete("admin", actor="other")
        self.assertIn("first admin", str(ctx.exception))
        self.assertIn("admin", self.db.users)

    def test_first_admin_is_identified_whatever_it_is_called(self) -> None:
        self.db.set_user("matt", groups=["admin", "users"])
        self.db.set_user("admin", groups=["admin", "users"])
        self.first_admin = "matt"
        with self.assertRaises(ValueError):
            usermgmt.delete("matt", actor="admin")
        usermgmt.delete("admin", actor="matt")
        self.assertNotIn("admin", self.db.users)

    def test_no_admin_can_delete_themself(self) -> None:
        with self.assertRaises(ValueError):
            usermgmt.delete("member", actor="member")

    def test_the_second_admin_is_the_last_one_that_can_go(self) -> None:
        self.first_admin = "someone-gone"
        self.db.set_user("admin2", groups=["admin", "users"])
        usermgmt.delete("admin2", actor="admin")
        with self.assertRaises(ValueError):
            usermgmt.delete("admin", actor="admin2")

    def test_reserved_identity_cannot_be_deleted(self) -> None:
        self.db.set_user("lemonseed-cp", groups=["users"])
        with self.assertRaises(ValueError):
            usermgmt.delete("lemonseed-cp", actor="admin")
        self.owui.user_delete.assert_not_called()


class EmailAndNameTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db.set_user("member", groups=["users"], owui_id="o-member")

    def test_rename_pushes_the_name_and_reports_drift(self) -> None:
        result = usermgmt.set_display_name("member", "Renamed", actor="admin")
        self.assertEqual(self.db.users["member"]["display_name"], "Renamed")
        self.owui.user_update.assert_called_once_with("o-member", {"name": "Renamed"})
        self.assertIsNone(result["owui_error"])

    def test_a_failed_rename_is_drift_not_failure(self) -> None:
        """Nothing selects a user by name, and the name header is read at
        creation only, so a stale copy in the module is cosmetic (D-17a)."""
        self.owui.user_update.return_value = {"ok": False, "error": "500"}
        result = usermgmt.set_display_name("member", "Renamed", actor="admin")
        self.assertEqual(result["state"], "ready")
        self.assertIn("kept the old name", result["owui_error"])

    def test_email_change_touches_no_module(self) -> None:
        """D-16': the module identity is derived and must never be mirrored."""
        usermgmt.set_email("member", "new@example.com", actor="admin")
        self.owui.user_update.assert_not_called()
        self.owui.user_create.assert_not_called()
        self.assertEqual(self.db.users["member"]["email"], "new@example.com")

    def test_email_audit_row_names_the_field_not_the_address(self) -> None:
        usermgmt.set_email("member", "private@example.com", actor="admin")
        self.assertEqual(self.detail_for("user.email_changed"), {"fields": ["email"]})

    def test_email_is_lower_cased_and_optional(self) -> None:
        usermgmt.set_email("member", "  Alex@Example.COM ", actor="admin")
        self.assertEqual(self.db.users["member"]["email"], "alex@example.com")
        usermgmt.set_email("member", "", actor="admin")
        self.assertEqual(self.db.users["member"]["email"], "")

    def test_the_module_identity_domain_is_never_acceptable_as_an_email(self) -> None:
        """A household address must not collide with the derived identity."""
        usermgmt.set_email("member", "member@localhost", actor="admin")
        self.assertEqual(self.db.users["member"]["email"], "member@localhost")


class ObserverTests(_Base):
    def test_a_raising_observer_does_not_break_the_operation(self) -> None:
        def _explode(event: str, payload: dict) -> None:
            raise RuntimeError("my sync is broken")

        usermgmt.register_observer(_explode)
        self.addCleanup(usermgmt._observers.remove, _explode)
        self.db.set_user("member", groups=["users"], owui_id="o")
        out = io.StringIO()
        with redirect_stdout(out):
            result = usermgmt.set_email("member", "a@b.com", actor="admin")
        self.assertEqual(result["state"], "ready")
        self.assertIn("my sync is broken", out.getvalue())

    def test_registering_twice_fires_once(self) -> None:
        self.db.set_user("member", groups=["users"])
        usermgmt.register_observer(self._observe)
        before = len(self.events)
        usermgmt.set_disabled("member", True, actor="admin")
        self.assertEqual(len(self.events) - before, 1)

    def test_each_observer_gets_its_own_payload(self) -> None:
        """One handler scribbling on its copy must not reach the next one.

        The copy is also taken after commit, so no observer can ever see an
        account that was rolled back.
        """
        seen: list[dict] = []

        def _scribble(event: str, payload: dict) -> None:
            payload["mutated"] = True

        usermgmt.register_observer(_scribble)
        self.addCleanup(usermgmt._observers.remove, _scribble)

        def _peek(event: str, payload: dict) -> None:
            seen.append(payload)

        usermgmt.register_observer(_peek)
        self.addCleanup(usermgmt._observers.remove, _peek)

        self.db.set_user("member", groups=["users"])
        usermgmt.set_disabled("member", True, actor="admin")
        self.assertNotIn("mutated", seen[-1])
        self.assertEqual(seen[-1]["actor"], "admin")


class GroupTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db.set_user("member", groups=["users"])

    def test_system_group_deletion_is_refused(self) -> None:
        for name in ("admin", "users"):
            with self.subTest(group=name), self.assertRaises(ValueError):
                usermgmt.delete_group(name, actor="admin")

    def test_custom_group_lifecycle(self) -> None:
        usermgmt.create_group("Kids", actor="admin")
        self.assertIn("kids", self.db.groups)
        self.assertFalse(self.db.groups["kids"])
        usermgmt.add_member("kids", "member", actor="admin")
        self.assertIn("kids", self.db.users["member"]["groups"])
        usermgmt.remove_member("kids", "member", actor="admin")
        self.assertNotIn("kids", self.db.users["member"]["groups"])
        usermgmt.delete_group("kids", actor="admin")
        self.assertNotIn("kids", self.db.groups)

    def test_system_groups_are_not_recreatable(self) -> None:
        for name in ("admin", "users"):
            with self.subTest(group=name), self.assertRaises(ValueError):
                usermgmt.create_group(name, actor="admin")

    def test_group_names_are_validated(self) -> None:
        for bad in ("", "1kids", "a", "kids.", "-kids", "a" * 33, "has space"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                usermgmt.create_group(bad, actor="admin")

    def test_admin_membership_is_not_a_group_call(self) -> None:
        """One place enforces the last-admin guard, not two that can forget."""
        for call in (
            lambda: usermgmt.add_member("admin", "member", actor="admin"),
            lambda: usermgmt.remove_member("admin", "member", actor="admin"),
        ):
            with self.assertRaises(ValueError):
                call()

    def test_membership_in_an_unknown_group_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            usermgmt.add_member("nope", "member", actor="admin")

    def test_deleting_a_group_clears_its_attributes_and_members(self) -> None:
        usermgmt.create_group("kids", actor="admin")
        usermgmt.add_member("kids", "member", actor="admin")
        usermgmt.set_group_attribute("kids", "content_filter", True, actor="admin")
        usermgmt.delete_group("kids", actor="admin")
        self.assertNotIn(("kids", "content_filter"), self.db.attributes)
        self.assertNotIn("kids", self.db.users["member"]["groups"])


class AttributeTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db.groups["kids"] = False

    def test_unknown_key_is_refused(self) -> None:
        """JSONB stores anything, so the meaning of a key lives in code (D-3)."""
        with self.assertRaises(ValueError) as ctx:
            usermgmt.set_group_attribute("kids", "mystery", True, actor="admin")
        self.assertIn("not known", str(ctx.exception))

    def test_wrong_type_is_refused(self) -> None:
        for value in ("true", 1, None, ["yes"]):
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                usermgmt.set_group_attribute("kids", "content_filter", value, actor="admin")

    def test_a_real_boolean_is_stored_as_json(self) -> None:
        usermgmt.set_group_attribute("kids", "content_filter", True, actor="admin")
        self.assertEqual(self.db.attributes[("kids", "content_filter")], "true")
        # Stored as JSON, not as the string "True": D-30's whole argument is that
        # a boolean check must not depend on which spelling a value arrived in.
        self.assertTrue(any("::jsonb" in s for s in self.statements()))

    def test_attribute_removal(self) -> None:
        usermgmt.set_group_attribute("kids", "content_filter", False, actor="admin")
        usermgmt.delete_group_attribute("kids", "content_filter", actor="admin")
        self.assertNotIn(("kids", "content_filter"), self.db.attributes)


class CleanupTests(_Base):
    def test_empty_input_touches_nothing(self) -> None:
        with mock.patch.object(modules, "owui_admin_client") as client:
            self.assertEqual(usermgmt.cleanup_owui_accounts([]), [])
        client.assert_not_called()

    def test_refusals_are_returned_not_swallowed(self) -> None:
        self.owui.user_delete.side_effect = [
            {"ok": True, "data": True},
            {"ok": False, "error": "first user"},
        ]
        problems = usermgmt.cleanup_owui_accounts(
            [{"username": "a", "owui_id": "o-1"}, {"username": "b", "owui_id": "o-2"}],
            actor="system",
        )
        self.assertEqual([p["username"] for p in problems], ["b"])
        self.assertIn("first user", problems[0]["error"])

    def test_id_is_looked_up_from_email_when_unknown(self) -> None:
        self.owui.users_all.return_value = {
            "ok": True,
            "data": {"users": [{"id": "found-1", "email": "c@localhost"}]},
        }
        problems = usermgmt.cleanup_owui_accounts(
            [{"username": "c", "owui_id": "", "email": "c@localhost"}], actor="system"
        )
        self.assertEqual(problems, [])
        self.owui.user_delete.assert_called_once_with("found-1")

    def test_unreachable_module_reports_every_identity(self) -> None:
        """D-20a's CP rows are already gone; the operator must still be told."""
        with mock.patch.object(modules, "owui_admin_client", return_value=None):
            problems = usermgmt.cleanup_owui_accounts(
                [{"username": "a", "owui_id": "o-1"}, {"username": "b", "owui_id": ""}]
            )
        self.assertEqual([p["username"] for p in problems], ["a", "b"])


class LoginStampTests(_Base):
    def test_last_login_failure_never_fails_a_sign_in(self) -> None:
        """An informational column is not a reason to refuse someone access."""
        out = io.StringIO()
        with mock.patch.object(db, "cp_connect", side_effect=RuntimeError("down")):
            with redirect_stdout(out):
                usermgmt.record_login("alice")
        self.assertIn("last_login alice", out.getvalue())


if __name__ == "__main__":
    unittest.main()
