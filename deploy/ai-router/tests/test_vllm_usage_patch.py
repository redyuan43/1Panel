import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("usage_patch", Path(__file__).resolve().parents[1] / "scripts/patch-vllm-cache-usage.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ResponsePatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "_version.py").write_text("__version__ = version = '1.5.0'\n")
        self.target = self.root / "entrypoints/openai/chat_completion/serving.py"
        self.target.parent.mkdir(parents=True)
        self.source = b"def stream(self, num_cached_tokens):\n    if self.enable_prompt_tokens_details and num_cached_tokens:\n        return {'cached_tokens': num_cached_tokens}\ndef regular(self, final_res):\n    if self.enable_prompt_tokens_details and final_res.num_cached_tokens:\n        return {'cached_tokens': final_res.num_cached_tokens}\n"
        self.target.write_bytes(self.source)
        self.guard = patch.object(module, "ORIGINAL_SHA256", hashlib.sha256(self.source).hexdigest())
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_zero_positive_unknown_and_disabled(self):
        from types import SimpleNamespace
        module.run(self.root, "apply")
        namespace = {}
        exec(self.target.read_bytes(), namespace)
        for enabled in (False, True):
            for count in (None, 0, 512):
                expected = {"cached_tokens": count} if enabled and count is not None else None
                self.assertEqual(namespace["stream"](SimpleNamespace(enable_prompt_tokens_details=enabled), count), expected)
                self.assertEqual(namespace["regular"](SimpleNamespace(enable_prompt_tokens_details=enabled), SimpleNamespace(num_cached_tokens=count)), expected)

    def test_idempotent_and_exact_rollback(self):
        module.run(self.root, "apply")
        data = self.target.read_bytes()
        module.run(self.root, "apply")
        module.run(self.root, "check")
        self.assertEqual(data, self.target.read_bytes())
        module.run(self.root, "rollback")
        self.assertEqual(self.source, self.target.read_bytes())
        with self.assertRaises(ValueError):
            module.run(self.root, "check")

    def test_refuses_version_or_independent_source_changes(self):
        module.run(self.root, "apply")
        modified = self.target.read_bytes() + b"# independent edit\n"
        self.target.write_bytes(modified)
        for action in ("apply", "rollback", "check"):
            with self.assertRaises(ValueError):
                module.run(self.root, action)
        self.assertEqual(self.target.read_bytes(), modified)
        (self.root / "_version.py").write_text("__version__ = '2.0.0'\n")
        with self.assertRaises(ValueError):
            module.run(self.root, "apply")


if __name__ == "__main__":
    unittest.main()
