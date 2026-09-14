import hashlib
from types import SimpleNamespace
import pytest
from app.recipe_dispatch import verify_backend, digest

@pytest.mark.parametrize("relative", ["comfy/ldm/models/autoencoder.py", "comfy_api/input/basic_types.py"])
def test_nested_data_named_directory_is_executable_source(tmp_path, relative):
    (tmp_path / "main.py").write_text("")
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_text("pass")
    manifest = [{"path": str(tmp_path / "main.py"), "sha256": hashlib.sha256(b"").hexdigest()}]
    backend = {"id": "test", "url": "http://127.0.0.1:19188", "runtime_root": str(tmp_path), "runtime_files": manifest, "runtime_version": digest(manifest)}
    with pytest.raises(ValueError, match="complete executable tree"):
        verify_backend(backend, SimpleNamespace())
