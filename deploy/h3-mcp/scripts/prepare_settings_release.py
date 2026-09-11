import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[3]
BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_preparer", BASE / "scripts/prepare_release.py")
preparer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparer)
spec = importlib.util.spec_from_file_location("auth_package_builder", BASE / "scripts/build_workbuddy_auth.py")
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


def run(*arguments):
    return subprocess.check_output(arguments)


def main(destination):
    if destination.exists():
        raise RuntimeError("Candidate exists; never overwrite")
    destination.mkdir()
    instances = [json.loads(run("docker", "inspect", "1panel-ai-router-router-control-" + name + "-1"))[0] for name in ("local", "tail")]
    image = instances[0]["Image"]
    if image != instances[1]["Image"]:
        raise RuntimeError("Control instances differ; reconcile before release")
    live = Path(run("systemctl", "--user", "show", "h3-studio-ivan.service", "-p", "WorkingDirectory", "--value").decode().strip())
    if not live.is_relative_to(Path.home() / ".local/state/h3-studio-ivan-production/releases"):
        raise RuntimeError("Unexpected Studio release")
    files = {path.relative_to(live).as_posix(): path.read_bytes() for path in live.rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.name != "candidate-manifest.json"}
    provenance = {name: preparer.digest(content) for name, content in files.items()}
    main_source = files["app/main.py"].decode()
    hook = 'app.mount(\n    "/",\n'
    assert main_source.count(hook) == 1 and "install_mcp_settings" not in main_source
    files["app/main.py"] = main_source.replace(hook, "from .mcp_settings import install_mcp_settings\ninstall_mcp_settings(app)\n\n" + hook).encode()
    files["app/mcp_settings.py"] = (BASE / "studio/mcp_settings.py").read_bytes()
    for path in (BASE / "frontend").iterdir():
        files["frontend/" + path.name] = path.read_bytes()
    index = files["frontend/index.html"].decode()
    hook = '<div class="topbar-actions">'
    assert index.count(hook) == 1
    files["frontend/index.html"] = index.replace(hook, hook + '\n        <a class="icon-text-button" href="./mcp-settings.html">设置 · MCP / WorkBuddy</a>').encode()
    files["frontend/h3-workbuddy-auth.json"] = preparer.canonical_json(auth.declaration())
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "auth.zip"
        auth.build(archive, ROOT / "deploy/ai-router/integrations/workbuddy/h3-studio")
        files["frontend/h3-workbuddy-auth.zip"] = archive.read_bytes()
    studio_manifest = preparer.write_candidate(destination / "studio", files, {
        "kind": "studio-mcp-settings", "base_provenance": {"source": str(live), "files_sha256": provenance}})
    source = run("docker", "exec", instances[0]["Id"], "cat", "/app/ai_router/studio_proxy.py").decode()
    original_proxy_hash = preparer.digest(source.encode())
    hook = 'STATIC |= {"comparison.html", "comparison-status.js", "comparison-planned.json"}'
    assert source.count(hook) == 1
    source = source.replace(hook, hook + '\nSTATIC |= {"mcp-settings.html", "mcp-settings.js", "mcp-settings.css", "h3-workbuddy-auth.zip", "h3-workbuddy-auth.json"}')
    hook = 'or path.startswith(("api/projects/", "api/768-queue/", "api/scripts/"))'
    assert source.count(hook) == 1
    source = source.replace(hook, 'or path.startswith(("api/projects/", "api/768-queue/", "api/scripts/", "api/mcp-admin/"))')
    modules = {name: (ROOT / "deploy/ai-router/ai_router" / name).read_bytes() for name in ("h3_mcp.py", "h3_mcp_management.py")}
    modules["studio_proxy.py"] = source.encode()
    for name, content in modules.items():
        preparer.parse_python(content, name)
    control_files = {"ai_router/" + name: content for name, content in modules.items()}
    control_files["Dockerfile"] = ("FROM " + image + "\nUSER root\n" +
        "".join("COPY ai_router/" + name + " /app/ai_router/" + name + "\n" for name in modules) +
        "RUN python -m pip check\nUSER 10001:10001\n").encode()
    control_files[".dockerignore"] = b"*\n!Dockerfile\n!ai_router/\n!ai_router/*.py\n"
    control_manifest = preparer.write_candidate(destination / "control", control_files, {
        "kind": "control-mcp-settings", "base_provenance": {"image": image, "studio_proxy_sha256": original_proxy_hash}})
    (destination / "sources.json").write_bytes(preparer.canonical_json({"studio": studio_manifest, "control": control_manifest}))
    print(json.dumps({"prepared": str(destination), "deployed": False, "dependency_changes": False}))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
