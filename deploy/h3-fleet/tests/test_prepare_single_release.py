from pathlib import Path
import shutil

from scripts.prepare_single_release import copy_ignore, executable_path


def test_runtime_copy_keeps_nested_model_code_but_not_weights(tmp_path):
    source = tmp_path / "source"
    for name in ("comfy/ldm/models/autoencoder.py", "models/large.safetensors", "app/user/models.py",
                 "comfy/__pycache__/cached.pyc", "main.py"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    target = tmp_path / "copy"
    shutil.copytree(source, target, ignore=copy_ignore(source))
    assert (target / "comfy/ldm/models/autoencoder.py").exists()
    assert (target / "app/user/models.py").exists()
    assert not (target / "models").exists()
    assert not (target / "comfy/__pycache__").exists()
    assert executable_path(Path("comfy/ldm/models/autoencoder.py"))
    assert not executable_path(Path("models/model.py"))
