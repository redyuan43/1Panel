from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    data_root: Path
    database_path: Path
    frontend_root: Path
    comparison_results_root: Path
    comparison_planned_path: Path
    workflow_root: Path
    comfy_input: Path
    comfy_url: str
    minimax_api_base: str
    minimax_credentials: Path


def get_settings() -> Settings:
    root = Path(
        os.environ.get(
            "H3_STUDIO_ROOT",
            Path(__file__).resolve().parents[1],
        )
    ).resolve()
    data_root = Path(os.environ.get("H3_STUDIO_DATA", root / "data")).resolve()
    return Settings(
        root=root,
        data_root=data_root,
        database_path=data_root / "studio.sqlite3",
        frontend_root=root / "frontend",
        comparison_results_root=Path(os.environ.get("H3_STUDIO_COMPARISON_RESULTS")
            or root / "frontend" / "comparison-results").resolve(),
        comparison_planned_path=Path(os.environ.get("H3_STUDIO_COMPARISON_PLANNED")
            or root / "frontend" / "comparison-planned.json").resolve(),
        workflow_root=Path(
            os.environ.get(
                "H3_WORKFLOW_ROOT",
                str(root / "workflows"),
            )
        ),
        comfy_input=Path(
            os.environ.get(
                "COMFY_INPUT_DIR",
                str(data_root / "input"),
            )
        ),
        comfy_url=os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/"),
        minimax_api_base=os.environ.get(
            "MINIMAX_API_BASE",
            "https://api.minimaxi.com",
        ).rstrip("/"),
        minimax_credentials=Path(
            os.environ.get(
                "MINIMAX_CREDENTIALS",
                str(Path.home() / ".config/minimax/credentials.env"),
            )
        ),
    )


SETTINGS = get_settings()


def ensure_directories() -> None:
    SETTINGS.data_root.mkdir(parents=True, exist_ok=True)
    (SETTINGS.data_root / "projects").mkdir(parents=True, exist_ok=True)


def load_minimax_key() -> str:
    value = os.environ.get("MINIMAX_API_KEY", "").strip()
    if value:
        return value
    path = SETTINGS.minimax_credentials
    if not path.exists():
        return ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        if name.strip() == "MINIMAX_API_KEY":
            return raw_value.strip().strip("\"'")
    return ""
