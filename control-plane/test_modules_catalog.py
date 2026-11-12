#!/usr/bin/env python3
"""Unit tests for modules_catalog.py (M9 P2): spec v1 validation, the build
merge step, digest verification at load, and the compat filter.

Validation runs against plain dicts, so these tests need no YAML; the tests
that exercise the YAML authoring path (parsing a module.yaml and merging the
real tree) skip with a stated reason when PyYAML is absent. The Nix build
always has it, so the shipped catalog is still validated end to end.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CP = Path(__file__).resolve().parent
REPO = CP.parent
sys.path.insert(0, str(CP))

import modules_catalog as mc  # noqa: E402

try:
    import yaml  # noqa: F401 — authoring format only; the runtime never parses it

    HAVE_YAML = True
except ImportError:  # pragma: no cover — exercised only on PyYAML-less hosts
    HAVE_YAML = False

PINS = json.loads((CP / "container-images.json").read_text(encoding="utf-8"))


def valid_spec(**overrides: object) -> dict:
    spec = {
        "spec_version": 1,
        "id": "demo",
        "name": "Demo",
        "type": "plugin",
        "version": "1.0.0",
        "maturity": "beta",
        "license": "Apache-2.0",
        "owners": ["wago"],
        "docker": {
            "image": "hello_world",
            "container": "lemonseed-demo",
        },
    }
    spec.update(overrides)
    if "docker" not in overrides:
        # Keep the container name in lockstep with the id unless the test
        # overrides the whole docker block: the catalog validates the pair.
        spec["docker"] = {
            "image": "hello_world",
            "container": f"lemonseed-{spec['id']}",
        }
    return spec


class ValidationTests(unittest.TestCase):
    """Unknown keys and wrong types are rejected, not ignored (§3)."""

    def test_minimal_spec_is_valid(self) -> None:
        self.assertEqual(mc.validate_spec(valid_spec())["id"], "demo")

    def test_unknown_top_level_key_rejected(self) -> None:
        with self.assertRaisesRegex(mc.CatalogError, "unknown top-level"):
            mc.validate_spec(valid_spec(surprise="x"))

    def test_unknown_docker_key_rejected(self) -> None:
        spec = valid_spec()
        spec["docker"]["surprise"] = "x"
        with self.assertRaisesRegex(mc.CatalogError, "unknown docker keys"):
            mc.validate_spec(spec)

    def test_id_shape_and_directory_equality(self) -> None:
        with self.assertRaisesRegex(mc.CatalogError, "id must match"):
            mc.validate_spec(valid_spec(id="Demo"))
        with self.assertRaisesRegex(mc.CatalogError, "must equal its directory"):
            mc.validate_spec(valid_spec(id="demo"), module_id="other")

    def test_wrong_type_rejected(self) -> None:
        with self.assertRaisesRegex(mc.CatalogError, "display name is required"):
            mc.validate_spec(valid_spec(name=None))
        with self.assertRaisesRegex(mc.CatalogError, "version is required"):
            mc.validate_spec(valid_spec(version=1.0))
        with self.assertRaisesRegex(mc.CatalogError, "owners must be a non-empty"):
            mc.validate_spec(valid_spec(owners=[]))

    def test_spec_version_must_be_one(self) -> None:
        with self.assertRaisesRegex(mc.CatalogError, "spec_version must be 1"):
            mc.validate_spec(valid_spec(spec_version=2))

    def test_type_and_maturity_enumerations(self) -> None:
        with self.assertRaisesRegex(mc.CatalogError, "type must be one of"):
            mc.validate_spec(valid_spec(type="widget"))
        with self.assertRaisesRegex(mc.CatalogError, "maturity must be one of"):
            mc.validate_spec(valid_spec(maturity="ga"))

    def test_application_may_not_declare_a_platform_block(self) -> None:
        spec = valid_spec(type="application")
        spec["platform"] = {"litellm": {"mcp_server": {"name": "demo", "url": "http://x"}}}
        with self.assertRaisesRegex(mc.CatalogError, "only a plugin"):
            mc.validate_spec(spec)

    def test_platform_block_shape(self) -> None:
        spec = valid_spec()
        spec["platform"] = {"litellm": {"mcp_server": {"name": "de-mo", "url": "http://x"}}}
        # LiteLLM rejects mcp_servers keys with a hyphen at startup, so the
        # catalog refuses one before it can break the chat path.
        with self.assertRaisesRegex(mc.CatalogError, "mcp_server.name must match"):
            mc.validate_spec(spec)
        spec["platform"]["litellm"]["mcp_server"]["name"] = "de_mo"
        spec["platform"]["litellm"]["mcp_server"]["url"] = "https://insecure.example"
        with self.assertRaisesRegex(mc.CatalogError, "must be an http:// URL"):
            mc.validate_spec(spec)

    def test_network_is_fixed_to_lemon(self) -> None:
        spec = valid_spec()
        spec["docker"]["network"] = "host"
        with self.assertRaisesRegex(mc.CatalogError, "fixed to lemon"):
            mc.validate_spec(spec)

    def test_ports_must_publish_to_localhost_only(self) -> None:
        spec = valid_spec()
        spec["docker"]["ports"] = ["0.0.0.0:8080:80"]
        with self.assertRaisesRegex(mc.CatalogError, "127.0.0.1 only"):
            mc.validate_spec(spec)
        spec["docker"]["ports"] = ["127.0.0.1:notaport:80"]
        with self.assertRaisesRegex(mc.CatalogError, "invalid port"):
            mc.validate_spec(spec)

    def test_volumes_must_stay_under_the_modules_root(self) -> None:
        spec = valid_spec()
        spec["docker"]["volumes"] = ["/etc:/etc:ro"]
        with self.assertRaisesRegex(mc.CatalogError, "must mount under"):
            mc.validate_spec(spec)

    def test_volumes_may_not_reach_the_docker_daemon(self) -> None:
        spec = valid_spec()
        spec["docker"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]
        with self.assertRaisesRegex(mc.CatalogError, "Docker daemon"):
            mc.validate_spec(spec)

    def test_run_args_audit(self) -> None:
        cases: list[tuple[list[str], str]] = [
            (["--privileged"], "privileged"),
            (["--pid=host"], "pid=host"),
            (["--pid", "host"], "pid=host"),
            (["--network", "host"], "network other than lemon"),
            (["--net=bridge"], "network other than lemon"),
            (["-v", "/etc:/etc"], "mount volumes"),
            (["--mount", "type=tmpfs"], "mount volumes"),
            (["-p", "0.0.0.0:80:80"], "only publish to 127.0.0.1"),
            (["--restart", "always"], "restart policy"),
            (["-v", "/var/run/docker.sock:/var/run/docker.sock"], "Docker daemon"),
        ]
        for tokens, phrase in cases:
            with self.subTest(tokens=tokens):
                with self.assertRaisesRegex(mc.CatalogError, phrase):
                    mc.audit_run_args(tokens)

    def test_run_args_localhost_publish_and_lemon_network_pass(self) -> None:
        mc.audit_run_args(["-p", "127.0.0.1:9:9", "--network", "lemon", "--read-only"])

    def test_probe_port_must_be_a_published_host_port(self) -> None:
        spec = valid_spec()
        spec["docker"]["probe"] = {"port": 9999}
        with self.assertRaisesRegex(mc.CatalogError, "published host ports"):
            mc.validate_spec(spec)

    def test_hooks_timeout_range(self) -> None:
        spec = valid_spec()
        spec["hooks"] = {"script_timeout": 3}
        with self.assertRaisesRegex(mc.CatalogError, "5-600"):
            mc.validate_spec(spec)
        spec["hooks"] = {"script_timeout": 700}
        with self.assertRaisesRegex(mc.CatalogError, "5-600"):
            mc.validate_spec(spec)
        spec["hooks"] = {"script_timeout": 600}
        self.assertEqual(mc.validate_spec(spec)["hooks"]["script_timeout"], 600)

    def test_data_shapes(self) -> None:
        spec = valid_spec()
        spec["data"] = {"postgres": {"db_name": "Bad Name"}}
        with self.assertRaisesRegex(mc.CatalogError, "db_name must match"):
            mc.validate_spec(spec)
        spec["data"] = {"valkey": {"db": 0}}
        with self.assertRaisesRegex(mc.CatalogError, "must be an int 1-15"):
            mc.validate_spec(spec)
        spec["data"] = {"valkey": {"db": 15}}
        self.assertEqual(mc.validate_spec(spec)["data"]["valkey"]["db"], 15)

    def test_reserved_database_names_are_not_a_spec_matter(self) -> None:
        # A module may declare any db_name; the reserved-name refusal is the
        # provisioner's job (db.provision_module_db), not the reader's.
        spec = valid_spec()
        spec["data"] = {"postgres": {"db_name": "open_webui"}}
        self.assertEqual(
            mc.validate_spec(spec)["data"]["postgres"]["db_name"], "open_webui"
        )

    def test_settings_entry_shape(self) -> None:
        spec = valid_spec()
        spec["settings"] = [{"name": "lower", "type": "string"}]
        with self.assertRaisesRegex(mc.CatalogError, "settings name must match"):
            mc.validate_spec(spec)
        spec["settings"] = [{"name": "GOOD", "type": "enum", "default": "x"}]
        with self.assertRaisesRegex(mc.CatalogError, "needs a non-empty choices"):
            mc.validate_spec(spec)
        spec["settings"] = [
            {"name": "GOOD", "type": "enum", "default": "x", "choices": ["y"]}
        ]
        with self.assertRaisesRegex(mc.CatalogError, "must be one of its choices"):
            mc.validate_spec(spec)
        spec["settings"] = [
            {"name": "GOOD", "type": "enum", "default": "y", "choices": ["y"]}
        ]
        self.assertEqual(len(mc.validate_spec(spec)["settings"]), 1)

    def test_container_name_must_be_derivable(self) -> None:
        # The control plane derives the container name from the module id, so
        # teardown never needs a spec that may no longer exist.
        spec = valid_spec()
        spec["docker"]["container"] = "lemonseed-other"
        with self.assertRaisesRegex(mc.CatalogError, "lemonseed-<module id>"):
            mc.validate_spec(spec)

    def test_outbound_declaration_shape(self) -> None:
        spec = valid_spec()
        spec["network"] = {"outbound": [{"host": "api.example.com", "port": 443}]}
        with self.assertRaisesRegex(mc.CatalogError, "exactly host, port, reason"):
            mc.validate_spec(spec)
        spec["network"]["outbound"][0]["reason"] = "update checks"
        self.assertEqual(len(mc.validate_spec(spec)["network"]["outbound"]), 1)


class CompatTests(unittest.TestCase):
    def test_no_floor_is_always_compatible(self) -> None:
        self.assertIsNone(mc.compat_reason(valid_spec(), "unknown"))

    def test_floor_comparison(self) -> None:
        spec = valid_spec(compat={"min_lemonseed": "20261112.01"})
        self.assertIsNone(mc.compat_reason(spec, "20261112.02"))
        reason = mc.compat_reason(spec, "20261112.00")
        self.assertIsNotNone(reason)
        self.assertIn("20261112.01", reason or "")

    def test_unknown_running_version_does_not_filter(self) -> None:
        # A version file that says "unknown" must not hide every module from
        # the admin who most needs to see what is installed.
        spec = valid_spec(compat={"min_lemonseed": "20261112.01"})
        self.assertIsNone(mc.compat_reason(spec, "unknown"))

    def test_malformed_floor(self) -> None:
        spec = valid_spec(compat={"min_lemonseed": "not-a-version"})
        self.assertIn("unreadable", mc.compat_reason(spec, "20261112.01") or "")


@unittest.skipUnless(HAVE_YAML, "PyYAML is required to write module.yaml fixtures")
class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = Path(self._tmp.name) / "modules"
        self.tree.mkdir()

    def _write(self, module_id: str, spec: dict) -> None:
        (self.tree / module_id).mkdir(parents=True, exist_ok=True)
        import yaml  # the authoring format only; guaranteed by skipUnless above

        (self.tree / module_id / "module.yaml").write_text(
            yaml.dump(spec, sort_keys=False), encoding="utf-8"
        )

    def test_real_tree_builds_and_digs_match(self) -> None:
        catalog = mc.build_catalog(REPO / "modules", pins=PINS)
        self.assertIn("hello-world", catalog["modules"])
        entry = catalog["modules"]["hello-world"]
        self.assertEqual(entry["spec"]["id"], "hello-world")
        actual = hashlib.sha256(
            (REPO / "modules" / "hello-world" / "module.yaml").read_bytes()
        ).hexdigest()
        self.assertEqual(entry["sha256"], actual)
        self.assertEqual(catalog["catalog_version"], mc.CATALOG_VERSION)

    def test_directory_without_module_yaml_rejects(self) -> None:
        (self.tree / "stray").mkdir()
        with self.assertRaisesRegex(mc.CatalogError, "has no module.yaml"):
            mc.build_catalog(self.tree, pins=PINS)

    def test_directory_name_must_equal_id(self) -> None:
        self._write("other-dir", valid_spec())
        with self.assertRaisesRegex(mc.CatalogError, "must equal its directory"):
            mc.build_catalog(self.tree, pins=PINS)

    def test_missing_pin_key_rejects(self) -> None:
        self._write("demo", valid_spec(docker={"image": "nonexistent", "container": "lemonseed-demo"}))
        with self.assertRaisesRegex(mc.CatalogError, "has no pin"):
            mc.build_catalog(self.tree, pins=PINS)

    def test_missing_dependency_rejects(self) -> None:
        self._write("demo", valid_spec(dependencies={"modules": ["ghost"]}))
        with self.assertRaisesRegex(mc.CatalogError, "depends on missing module"):
            mc.build_catalog(self.tree, pins=PINS)

    def test_dependency_cycle_rejects(self) -> None:
        self._write(
            "aa", valid_spec(id="aa", dependencies={"modules": ["bb"]})
        )
        self._write(
            "bb", valid_spec(id="bb", dependencies={"modules": ["aa"]})
        )
        with self.assertRaisesRegex(mc.CatalogError, "dependency cycle"):
            mc.build_catalog(self.tree, pins=PINS)

    def test_topological_order(self) -> None:
        self._write("aa", valid_spec(id="aa"))
        self._write("bb", valid_spec(id="bb", dependencies={"modules": ["aa"]}))
        self._write("cc", valid_spec(id="cc", dependencies={"modules": ["bb"]}))
        order = mc.dependency_order(mc.build_catalog(self.tree, pins=PINS)["modules"])
        self.assertEqual(order, ["aa", "bb", "cc"])

    def test_unique_mcp_server_names(self) -> None:
        platform = {
            "litellm": {
                "mcp_server": {"name": "same", "url": "http://lemonseed-demo:80/mcp"}
            }
        }
        self._write("aa", valid_spec(id="aa", platform=platform))
        self._write("bb", valid_spec(id="bb", platform=platform))
        with self.assertRaisesRegex(mc.CatalogError, "mcp server name 'same'"):
            mc.build_catalog(self.tree, pins=PINS)

    def test_unique_host_ports(self) -> None:
        self._write("aa", valid_spec(id="aa", docker={"image": "hello_world", "container": "lemonseed-aa", "ports": ["127.0.0.1:9:9"]}))
        self._write("bb", valid_spec(id="bb", docker={"image": "hello_world", "container": "lemonseed-bb", "ports": ["127.0.0.1:9:80"]}))
        with self.assertRaisesRegex(mc.CatalogError, "host port 9 is already"):
            mc.build_catalog(self.tree, pins=PINS)

    def test_unique_valkey_db(self) -> None:
        self._write("aa", valid_spec(id="aa", data={"valkey": {"db": 3}}))
        self._write("bb", valid_spec(id="bb", data={"valkey": {"db": 3}}))
        with self.assertRaisesRegex(mc.CatalogError, "valkey db 3 is already"):
            mc.build_catalog(self.tree, pins=PINS)

    def test_catalog_output_is_sorted_and_shape_stable(self) -> None:
        self._write("bb", valid_spec(id="bb"))
        self._write("aa", valid_spec(id="aa"))
        catalog = mc.build_catalog(self.tree, pins=PINS)
        self.assertEqual(list(catalog["modules"]), ["aa", "bb"])
        self.assertEqual(catalog["generated_from"], "modules/")
        for entry in catalog["modules"].values():
            self.assertEqual(set(entry), {"spec", "sha256"})


class LoaderTests(unittest.TestCase):
    """The runtime loader: digest verification and spec re-validation (§4)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tree = self.root / "modules"
        self.tree.mkdir()
        self.shipped = self.root / "modules-catalog.json"
        self.durable = self.root / "var" / "catalog" / "catalog.json"

    def _build(self, spec: dict) -> dict:
        (self.tree / spec["id"]).mkdir(parents=True, exist_ok=True)
        (self.tree / spec["id"] / "module.yaml").write_text(
            json.dumps(spec), encoding="utf-8"  # any bytes work: digests, not YAML
        )
        catalog = mc.build_catalog(self.tree, pins=PINS)
        self.shipped.write_text(json.dumps(catalog), encoding="utf-8")
        return catalog

    def _load(self) -> dict:
        return mc.load_catalog(
            shipped=self.shipped, durable=self.durable, root=self.tree
        )

    def test_valid_module_loads(self) -> None:
        self._build(valid_spec())
        loaded = self._load()
        self.assertEqual(list(loaded["modules"]), ["demo"])
        self.assertEqual(loaded["problems"], [])

    def test_tampered_spec_refused(self) -> None:
        catalog = self._build(valid_spec())
        # Someone edited the writable catalog copy, not the module.yaml.
        catalog["modules"]["demo"]["spec"]["docker"]["run_args"] = ["--privileged"]
        self.shipped.write_text(json.dumps(catalog), encoding="utf-8")
        loaded = self._load()
        self.assertEqual(loaded["modules"], {})
        self.assertTrue(any("run_args" in p for p in loaded["problems"]))

    def test_tampered_module_yaml_refused(self) -> None:
        spec = valid_spec()
        self._build(spec)
        (self.tree / "demo" / "module.yaml").write_text(
            json.dumps(spec) + "\n# hand edit\n", encoding="utf-8"
        )
        loaded = self._load()
        self.assertEqual(loaded["modules"], {})
        self.assertTrue(any("does not match" in p for p in loaded["problems"]))

    def test_missing_module_yaml_refused(self) -> None:
        self._build(valid_spec())
        (self.tree / "demo" / "module.yaml").unlink()
        loaded = self._load()
        self.assertEqual(loaded["modules"], {})
        self.assertTrue(any("missing from the image path" in p for p in loaded["problems"]))

    def test_missing_pin_refused(self) -> None:
        # A pin that vanished from an appliance update: build_catalog refuses
        # this spec at build time, so the loader test must craft the entry
        # directly — the loader's own pin check is what catches it.
        spec = valid_spec(docker={"image": "ghost", "container": "lemonseed-demo"})
        digest = hashlib.sha256(json.dumps(spec).encode("utf-8")).hexdigest()
        self.shipped.write_text(
            json.dumps({"modules": {"demo": {"spec": spec, "sha256": digest}}}),
            encoding="utf-8",
        )
        (self.tree / "demo").mkdir()
        (self.tree / "demo" / "module.yaml").write_text(json.dumps(spec), encoding="utf-8")
        loaded = self._load()
        self.assertEqual(loaded["modules"], {})
        self.assertTrue(any("ghost" in p for p in loaded["problems"]))

    def test_unreadable_catalog_is_a_problem_not_a_crash(self) -> None:
        self.shipped.write_text("{ not json", encoding="utf-8")
        loaded = self._load()
        self.assertEqual(loaded["modules"], {})
        self.assertTrue(loaded["problems"])


class SeederTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.shipped = self.root / "shipped.json"
        self.durable = self.root / "var" / "catalog" / "catalog.json"

    def _write_shipped(self, catalog_version: int, marker: str = "a") -> None:
        self.shipped.parent.mkdir(parents=True, exist_ok=True)
        self.shipped.write_text(
            json.dumps({"catalog_version": catalog_version, "marker": marker}),
            encoding="utf-8",
        )

    def test_copies_when_missing(self) -> None:
        self._write_shipped(1)
        result = mc.ensure_seeded_catalog(self.shipped, self.durable)
        self.assertTrue(result["seeded"])
        self.assertEqual(json.loads(self.durable.read_text())["marker"], "a")

    def test_refreshes_same_version_when_content_changes(self) -> None:
        # An appliance update ships a new catalog at the same version: the
        # writable copy must follow it, or the box serves last release's
        # modules forever.
        self._write_shipped(1, marker="a")
        mc.ensure_seeded_catalog(self.shipped, self.durable)
        self._write_shipped(1, marker="b")
        result = mc.ensure_seeded_catalog(self.shipped, self.durable)
        self.assertTrue(result["seeded"])
        self.assertEqual(json.loads(self.durable.read_text())["marker"], "b")

    def test_keeps_a_newer_durable_copy(self) -> None:
        # The future hosted-catalog refresh writes a higher version; a re-seed
        # after an appliance update must not clobber it.
        self._write_shipped(1, marker="a")
        mc.ensure_seeded_catalog(self.shipped, self.durable)
        self.durable.write_text(
            json.dumps({"catalog_version": 2, "marker": "hosted"}), encoding="utf-8"
        )
        result = mc.ensure_seeded_catalog(self.shipped, self.durable)
        self.assertFalse(result["seeded"])
        self.assertEqual(json.loads(self.durable.read_text())["marker"], "hosted")


class HarnessTests(unittest.TestCase):
    """Every module's own no-external-dependency harness runs in make test
    (MODULES-FRAMEWORK.md requirement 38): a bad module fails CI before it
    fails a release."""

    def test_every_module_harness_passes(self) -> None:
        harnesses = sorted((REPO / "modules").glob("*/test/test.sh"))
        self.assertTrue(harnesses, "no module harnesses found")
        for harness in harnesses:
            with self.subTest(harness=str(harness.relative_to(REPO))):
                done = subprocess.run(
                    ["bash", str(harness)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
