"""CPU-only installation safety tests; all runtime files live in temporary dirs."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class EdgeInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.edge = load(ROOT / "integrations/edge-disk-cache/patch_simple_kv_disk.py", "edge_patch")
        self.manager = self.root / "vllm/v1/simple_kv_offload/manager.py"
        self.worker = self.manager.with_name("worker.py")
        self.manager.parent.mkdir(parents=True)
        for p in (self.manager, self.worker):
            p.write_text("value = 'before'\n")
            p.chmod(0o640)

    def run_patch(self, worker):
        def transform(path):
            path.write_text("value = 'after'\n")
        with mock.patch.object(self.edge, "patch_manager", transform), mock.patch.object(self.edge, "patch_worker", worker), mock.patch.object(self.edge, "verify", lambda path, markers: ast.parse(path.read_text())), mock.patch.object(self.edge.sys, "argv", ["patch", str(self.root)]):
            self.edge.main()

    def test_worker_anchor_failure_leaves_both_originals_intact(self):
        def fail(path):
            raise RuntimeError("unsupported worker anchor")
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            self.run_patch(fail)
        for p in (self.manager, self.worker):
            self.assertEqual(p.read_text(), "value = 'before'\n")

    def test_success_updates_both_files_and_preserves_permissions(self):
        self.run_patch(lambda p: p.write_text("value = 'after'\n"))
        for p in (self.manager, self.worker):
            self.assertEqual(p.read_text(), "value = 'after'\n")
            self.assertEqual(p.stat().st_mode & 0o777, 0o640)

    def test_second_file_write_failure_rolls_back_first(self):
        original = self.edge.atomic_write
        def write(path, data, mode):
            if path == self.worker and b"after" in data:
                raise OSError("injected worker replace failure")
            return original(path, data, mode)
        with mock.patch.object(self.edge, "atomic_write", write):
            with self.assertRaisesRegex(OSError, "injected"):
                self.run_patch(lambda p: p.write_text("value = 'after'\n"))
        for p in (self.manager, self.worker):
            self.assertEqual(p.read_text(), "value = 'before'\n")
            self.assertEqual(p.stat().st_mode & 0o777, 0o640)

    def test_invalid_staged_python_leaves_originals_intact(self):
        with self.assertRaises(SyntaxError):
            self.run_patch(lambda p: p.write_text("invalid ! python"))
        for p in (self.manager, self.worker):
            self.assertEqual(p.read_text(), "value = 'before'\n")

class LMCacheInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        script = self.root / "scripts/patch-lmcache-operation-lifecycle.py"
        script.parent.mkdir()
        shutil.copyfile(ROOT / "scripts/patch-lmcache-operation-lifecycle.py", script)
        self.installer = load(script, "lmcache_installer")
        bundle = script.parent / "patches/lmcache-operation-lifecycle-v1"
        bundle.mkdir(parents=True)
        self.site = self.root / "site"
        (self.site / "lmcache").mkdir(parents=True)
        self.backup = self.root / "backup"
        files = []
        for name in ("a.py", "b.py"):
            (self.site / "lmcache" / name).write_bytes(b"value = 1\n")
            payload = name + ".patch"
            (bundle / payload).write_text("--- old\n+++ new\n@@ -1 +1 @@\n-value = 1\n+value = 2\n")
            files.append(dict(path="lmcache/" + name, payload=payload,
                before_sha256=hashlib.sha256(b"value = 1\n").hexdigest(),
                after_sha256=hashlib.sha256(b"value = 2\n").hexdigest()))
        (bundle / "manifest.json").write_text(json.dumps(dict(id="test", files=files)))

    def run_install(self, mode="apply"):
        self.installer.run(SimpleNamespace(site_packages=self.site, backup=self.backup, mode=mode))

    def test_fresh_apply_reapply_and_rollback(self):
        self.run_install()
        self.run_install()
        self.run_install("rollback")
        for name in ("a.py", "b.py"):
            self.assertEqual((self.site / "lmcache" / name).read_bytes(), b"value = 1\n")

    def test_mixed_runtime_without_original_backup_is_rejected_before_mutation(self):
        (self.site / "lmcache/a.py").write_bytes(b"value = 2\n")
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual((self.site / "lmcache/b.py").read_bytes(), b"value = 1\n")

    def test_mixed_runtime_with_valid_original_backup_can_complete_and_rollback(self):
        (self.site / "lmcache/a.py").write_bytes(b"value = 2\n")
        (self.backup / "lmcache").mkdir(parents=True)
        (self.backup / "lmcache/a.py").write_bytes(b"value = 1\n")
        self.run_install()
        self.run_install("rollback")
        self.assertEqual((self.site / "lmcache/a.py").read_bytes(), b"value = 1\n")

    def test_fully_patched_runtime_cannot_claim_fresh_rollback_backup(self):
        self.run_install()
        self.backup = self.root / "different-backup"
        with self.assertRaises(ValueError):
            self.run_install()

    def test_corrupt_original_backup_is_rejected_before_other_file_changes(self):
        (self.site / "lmcache/a.py").write_bytes(b"value = 2\n")
        (self.backup / "lmcache").mkdir(parents=True)
        (self.backup / "lmcache/a.py").write_bytes(b"corrupt\n")
        with self.assertRaisesRegex(ValueError, "invalid original backup"):
            self.run_install()
        self.assertEqual((self.site / "lmcache/b.py").read_bytes(), b"value = 1\n")

    def test_check_does_not_require_backup_for_installed_runtime(self):
        self.run_install()
        self.backup = None
        self.run_install("check")

if __name__ == "__main__":
    unittest.main()
