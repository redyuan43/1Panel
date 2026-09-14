"""Immutable CPU staging for a managed Edge worker that is normally stopped."""
import hashlib
import os
from pathlib import Path
import re
import tempfile


def stage_input(directory, filename, content):
    if not re.fullmatch(r'asset_[a-f0-9]{32}\.(?:png|jpe?g|webp)', filename):
        raise ValueError('edge_immutable_image_name_required')
    if not isinstance(content, bytes) or not 0 < len(content) <= 32 * 1024**2:
        raise ValueError('edge_input_size_limit')
    root = Path(directory)
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError('edge_input_directory_unverified')
    target = root / filename
    expected = hashlib.sha256(content).hexdigest()
    fd, temporary = tempfile.mkstemp(prefix='.h3-input-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
        with os.fdopen(os.open(target, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
            actual = hashlib.sha256(stream.read()).hexdigest()
        if actual != expected:
            raise ValueError('edge_immutable_input_conflict')
        return {'name': filename, 'sha256': expected, 'size': len(content), 'worker_started': False}
    finally:
        Path(temporary).unlink(missing_ok=True)
