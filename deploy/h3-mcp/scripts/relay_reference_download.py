import base64
import importlib.util
from pathlib import Path
import subprocess
import urllib.request


source = Path(__file__).with_name("acquire_reference_model.py")
spec = importlib.util.spec_from_file_location("reference_source", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
code = "__name__='h3_reference_receiver'\n" + source.read_text() + "\nmain(sys.stdin.buffer)\n"
code = code.replace("from __future__ import annotations\n", "", 1)
remote = 'ionice -c 3 nice -n 15 python3 -c "import base64;exec(base64.b64decode(\'' + base64.b64encode(code.encode()).decode() + '\'))"'
with urllib.request.urlopen(module.URL, timeout=60) as response:
    declared = response.headers.get("content-length")
    if declared is not None and int(declared) != module.SIZE:
        raise ValueError("source size changed")
    with subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "ivan", remote], stdin=subprocess.PIPE) as receiver:
        try:
            count = 0
            while chunk := response.read(1024**2):
                count += len(chunk)
                if count > module.SIZE:
                    raise ValueError("source exceeds pinned size")
                receiver.stdin.write(chunk)
        finally:
            receiver.stdin.close()
        if receiver.wait() != 0 or count != module.SIZE:
            raise RuntimeError("download not complete; inspect preserved receipt")
