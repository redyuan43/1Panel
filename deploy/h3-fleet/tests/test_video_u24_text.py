from pathlib import Path

import pytest

from scripts.serve_video_u24_text import command


def test_original_4060_text_command_moves_behind_proxy(tmp_path):
    binary = tmp_path / "ninfer-serve"
    binary.write_bytes(b"test")
    saved = {"argv": [str(binary), "model", "--device", "0", "--host", "0.0.0.0",
                      "--port", "18086"]}
    result = command(saved)
    assert result[result.index("--host") + 1] == "127.0.0.1"
    assert result[result.index("--port") + 1] == "18087"
    assert saved["argv"][saved["argv"].index("--port") + 1] == "18086"
    saved["argv"][saved["argv"].index("--device") + 1] = "1"
    with pytest.raises(RuntimeError, match="original 4060 Ti"):
        command(saved)
