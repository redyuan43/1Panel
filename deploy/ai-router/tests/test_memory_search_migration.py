from pathlib import Path
import runpy
import sqlite3

from cryptography.fernet import Fernet
import pytest

from ai_router.memory_index import MemoryIndex


prepare = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                            "scripts/prepare-memory-search-indexes.py"))["prepare"]


def test_search_index_migration_is_explicit_idempotent_and_reversible(tmp_path):
    path = tmp_path / "memory.sqlite3"
    MemoryIndex(path, Fernet.generate_key().decode())
    assert not prepare(path)["ready"]
    assert prepare(path, "apply")["ready"]
    assert prepare(path, "apply")["ready"]
    assert not prepare(path, "rollback")["ready"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_documents").fetchone() == (0,)


def test_migration_rolls_back_when_existing_index_has_different_definition(tmp_path):
    path = tmp_path / "memory.sqlite3"
    MemoryIndex(path, Fernet.generate_key().decode())
    with sqlite3.connect(path) as db:
        db.execute("CREATE INDEX memory_documents_search ON memory_documents(owner)")
    with pytest.raises(ValueError, match="definition differs"):
        prepare(path, "apply")
    assert prepare(path)["indexes"] == ["memory_documents_search"]


@pytest.mark.parametrize("mode", ["check", "apply ", "", None])
def test_migration_rejects_unknown_mode_before_opening_database(tmp_path, mode):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError, match="unsupported migration mode"):
        prepare(path, mode)
    assert not path.exists()
