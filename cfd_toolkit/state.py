"""Persistent campaign state with atomic updates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import CampaignConfig, CaseDefinition
from .io_utils import atomic_write_json, read_json, utc_now


def state_path(config: CampaignConfig) -> Path:
    return config.state_dir / "state.json"


def load_state(
    config: CampaignConfig, cases: list[CaseDefinition]
) -> dict[str, Any]:
    path = state_path(config)
    if path.exists():
        state = read_json(path)
    else:
        state = {
            "schema_version": 2,
            "campaign": config.name,
            "created_at": utc_now(),
            "cases": {},
        }
    if state.get("campaign") != config.name:
        raise ValueError(
            f"state campaign {state.get('campaign')!r} does not match "
            f"configuration {config.name!r}"
        )
    for case in cases:
        state["cases"].setdefault(
            case.name,
            {
                "status": "not_generated",
                "attempts": 0,
                "parameters": case.parameters,
            },
        )
    return state


def save_state(config: CampaignConfig, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_write_json(state_path(config), state)


def update_case_status_file(
    case_dir: Path, item: dict[str, Any]
) -> None:
    lines = [
        f"status={item.get('status', 'unknown')}",
        f"updated_at={item.get('updated_at', utc_now())}",
    ]
    for key in (
        "job_id",
        "scheduler_state",
        "latest_time",
        "end_time",
        "progress_percent",
        "return_code",
        "detail",
    ):
        if item.get(key) is not None:
            lines.append(f"{key}={item[key]}")
    (case_dir / "run_status.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
