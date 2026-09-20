from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("check_context_governance.py")
SPEC = importlib.util.spec_from_file_location("check_context_governance", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ContextGovernanceTests(unittest.TestCase):
    def fixture(self, verified_at="2026-09-20T00:00:00+00:00"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "docs").mkdir()
        (root / "AGENTS.md").write_text("# Rules\n\nSee [state](docs/state.md).\n", encoding="utf-8")
        (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "docs/state.md").write_text(
            "<!-- context-meta\n"
            + json.dumps({
                "status": "current",
                "last_verified_at": verified_at,
                "verified_commit": "0123456789abcdef0123456789abcdef01234567",
                "runtime_verification": "read_only_metadata",
                "authoritative_sources": ["../source.py"],
            })
            + "\n-->\n\n# State\n",
            encoding="utf-8",
        )
        config = root / "docs/context-governance.json"
        config.write_text(json.dumps({
            "version": 1,
            "instruction_budget_bytes": 4096,
            "freshness_warning_days": 30,
            "instruction_files": ["AGENTS.md"],
            "forbidden_instruction_literals": ["dynamic_key:"],
            "documents": [{
                "id": "state",
                "path": "docs/state.md",
                "triggers": ["source.py"],
            }],
        }), encoding="utf-8")
        return root, config

    def validate(self, root, config, changed_files=None):
        return MODULE.validate_repository(
            root,
            config,
            changed_files=changed_files,
            now=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )

    def test_valid_repository(self):
        root, config = self.fixture()
        errors, warnings = self.validate(root, config)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_broken_relative_link_is_error(self):
        root, config = self.fixture()
        (root / "AGENTS.md").write_text("[missing](docs/missing.md)\n", encoding="utf-8")
        errors, _ = self.validate(root, config)
        self.assertTrue(any("broken Markdown link" in item for item in errors))

    def test_stale_snapshot_warns_without_error(self):
        root, config = self.fixture("2026-01-01T00:00:00+00:00")
        errors, warnings = self.validate(root, config)
        self.assertEqual(errors, [])
        self.assertTrue(any("days old" in item for item in warnings))

    def test_governed_source_requires_document_change(self):
        root, config = self.fixture()
        errors, _ = self.validate(root, config, {"source.py"})
        self.assertTrue(any("must change with governed sources" in item for item in errors))
        errors, _ = self.validate(root, config, {"source.py", "docs/state.md"})
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
