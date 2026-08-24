#!/usr/bin/env python3
"""Unit tests for db.py and data_services.py (mocked; no Postgres required)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import data_services  # noqa: E402
import db  # noqa: E402


class DbProvisioningTests(unittest.TestCase):
    def test_read_env_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write("POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=secret\n")
            path = Path(tmp.name)
        try:
            env = db._read_env_file(path)
            self.assertEqual(env["POSTGRES_USER"], "postgres")
            self.assertEqual(env["POSTGRES_PASSWORD"], "secret")
        finally:
            path.unlink()

    @mock.patch.object(db, "pg_isready", return_value=False)
    @mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://u:p@127.0.0.1/postgres")
    def test_provision_waiting_for_postgres(self, _dsn: mock.Mock, _ready: mock.Mock) -> None:
        result = db.provision_data_plane()
        self.assertEqual(result["state"], "starting")

    @mock.patch.object(db, "_write_module_secret")
    @mock.patch.object(db, "_connect")
    @mock.patch.object(db, "pg_isready", return_value=True)
    @mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://u:p@127.0.0.1/postgres")
    def test_provision_ready(
        self,
        _dsn: mock.Mock,
        _ready: mock.Mock,
        connect: mock.Mock,
        _write: mock.Mock,
    ) -> None:
        admin = mock.MagicMock()
        cp = mock.MagicMock()
        connect.side_effect = [admin, cp]

        def cursor_ctx(*_a, **_k):
            ctx = mock.MagicMock()
            ctx.__enter__ = mock.Mock(return_value=ctx)
            ctx.__exit__ = mock.Mock(return_value=False)
            ctx.fetchone.return_value = None
            return ctx

        admin.cursor.side_effect = cursor_ctx
        cp.cursor.side_effect = cursor_ctx

        with mock.patch.object(db, "POSTGRES_ENV", Path("/nonexistent")):
            with mock.patch.object(db, "_module_credentials", return_value={}):
                result = db.provision_data_plane()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["cp_database"], "lemonseed_cp")
        self.assertEqual(len(result["modules"]), 2)


class DataServicesTests(unittest.TestCase):
    @mock.patch.object(data_services, "valkey_ready", return_value=True)
    @mock.patch.object(data_services, "postgres_snapshot", return_value={"state": "ready"})
    def test_data_snapshot(self, _pg: mock.Mock, _vk: mock.Mock) -> None:
        snap = data_services.data_snapshot()
        self.assertEqual(snap["postgres"]["state"], "ready")
        self.assertEqual(snap["valkey"]["state"], "ready")

    @mock.patch.object(data_services, "_sync_json_state_to_db")
    @mock.patch.object(data_services, "provision_data_plane", return_value={"state": "ready"})
    @mock.patch.object(data_services, "pg_isready", return_value=True)
    def test_ensure_provisioned_syncs_state(
        self,
        _ready: mock.Mock,
        _prov: mock.Mock,
        sync: mock.Mock,
    ) -> None:
        data_services._provision_state = None
        result = data_services.ensure_provisioned()
        self.assertEqual(result["state"], "ready")
        sync.assert_called_once()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
