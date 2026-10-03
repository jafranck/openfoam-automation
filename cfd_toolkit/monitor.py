"""Inspect solver logs, completion artifacts, and Slurm state."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import CampaignConfig, CaseDefinition, format_template
from .io_utils import atomic_write_json, utc_now, write_csv
from .state import load_state, save_state, update_case_status_file


_TIME_RE = re.compile(r"(?m)^\s*Time\s*=\s*([-+0-9.eE]+)")
_SCALAR_RE_TEMPLATE = (
    r"(?m)^\s*{keyword}\s+(?:\[[^\]\n]+\]\s+)?([-+0-9.eE]+)\s*;"
)
_RESIDUAL_RE = re.compile(
    r"Solving for\s+([^,\s]+),\s+Initial residual\s*=\s*([-+0-9.eE]+)"
)
_COURANT_RE = re.compile(
    r"Courant Number mean:\s*([-+0-9.eE]+)\s+max:\s*([-+0-9.eE]+)"
)
_SLURM_TERMINAL_SUCCESS = {"COMPLETED"}
_SLURM_ACTIVE = {
    "PENDING": "queued",
    "CONFIGURING": "queued",
    "RUNNING": "running",
    "COMPLETING": "running",
    "SUSPENDED": "running",
}
_SLURM_FAILURE = {
    "BOOT_FAIL",
    "CANCELLED",
    "DEADLINE",
    "FAILED",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "REVOKED",
    "TIMEOUT",
}


def _completion_files(case_dir: Path, patterns: list[str]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        found.extend(path for path in case_dir.glob(pattern) if path.is_file())
    return sorted(set(found))


def _read_scalar(path: Path, keyword: str) -> float | None:
    if not path.is_file():
        return None
    pattern = re.compile(
        _SCALAR_RE_TEMPLATE.format(keyword=re.escape(keyword))
    )
    match = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    return float(match.group(1)) if match else None


def _format_command(template: str, values: dict[str, Any]) -> list[str]:
    return shlex.split(str(format_template(template, values)))


def _capture(command: list[str], cwd: Path) -> tuple[int, str]:
    if not command:
        return 127, ""
    if shutil.which(command[0]) is None:
        return 127, ""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, result.stdout.strip()


def _normalize_slurm_state(value: str) -> str:
    normalized = value.split("|", 1)[0].strip().upper().split("+", 1)[0]
    return normalized.split(maxsplit=1)[0] if normalized else ""


def _query_slurm(
    config: CampaignConfig, case: CaseDefinition, job_id: str
) -> str | None:
    scheduler = config.scheduler
    values = {**case.values, "job_id": job_id}
    queue_template = scheduler.get(
        "status_command", "squeue -h -j {job_id} -o %T"
    )
    code, output = _capture(
        _format_command(str(queue_template), values),
        config.cases_dir / case.name,
    )
    if code == 0 and output:
        state = _normalize_slurm_state(output.splitlines()[0])
        return state or None

    accounting_template = scheduler.get(
        "accounting_command",
        "sacct -n -X -j {job_id} --format=State -P",
    )
    code, output = _capture(
        _format_command(str(accounting_template), values),
        config.cases_dir / case.name,
    )
    if code == 0 and output:
        states = [
            _normalize_slurm_state(line)
            for line in output.splitlines()
            if line.strip()
        ]
        if states:
            return states[0]
    return None


def inspect_case(
    config: CampaignConfig,
    case: CaseDefinition,
    previous: dict[str, Any],
) -> dict[str, Any]:
    case_dir = config.cases_dir / case.name
    item = dict(previous)
    item["case"] = case.name
    item["parameters"] = case.parameters
    item["updated_at"] = utc_now()
    if not case_dir.is_dir():
        item["status"] = "not_generated"
        return item

    solver_log = case_dir / str(config.scheduler["solver_log"])
    log_text = (
        solver_log.read_text(encoding="utf-8", errors="replace")
        if solver_log.is_file()
        else ""
    )
    times = _TIME_RE.findall(log_text)
    item["latest_time"] = float(times[-1]) if times else None
    control_dict = case_dir / str(
        config.data["monitoring"].get(
            "control_dict", "system/controlDict"
        )
    )
    start_time = _read_scalar(control_dict, "startTime")
    end_time = _read_scalar(control_dict, "endTime")
    item["start_time"] = start_time
    item["end_time"] = end_time
    if (
        item["latest_time"] is not None
        and end_time is not None
        and end_time > (start_time or 0.0)
    ):
        progress = (
            (item["latest_time"] - (start_time or 0.0))
            / (end_time - (start_time or 0.0))
            * 100.0
        )
        item["progress_percent"] = round(max(0.0, min(100.0, progress)), 2)
    else:
        item["progress_percent"] = None

    residuals: dict[str, float] = {}
    for field, value in _RESIDUAL_RE.findall(log_text):
        residuals[field] = float(value)
    item["latest_residuals"] = residuals
    courant = _COURANT_RE.findall(log_text)
    if courant:
        item["courant_mean"] = float(courant[-1][0])
        item["courant_max"] = float(courant[-1][1])

    failure_matches = [
        pattern
        for pattern in config.scheduler.get("failure_patterns", [])
        if str(pattern).lower() in log_text.lower()
    ]

    outputs = _completion_files(
        case_dir, list(config.scheduler.get("completion_files", []))
    )
    item["completion_files_found"] = [str(path) for path in outputs]

    scheduler_state: str | None = None
    if config.scheduler["type"] == "slurm" and item.get("job_id"):
        scheduler_state = _query_slurm(config, case, str(item["job_id"]))
        if scheduler_state:
            item["scheduler_state"] = scheduler_state

    reached_end = (
        item["latest_time"] is not None
        and end_time is not None
        and item["latest_time"]
        >= end_time - max(1e-10, abs(end_time) * 1e-8)
    )
    if failure_matches:
        item["status"] = "failed"
        item["detail"] = "solver log matched: " + ", ".join(failure_matches)
    elif scheduler_state in _SLURM_FAILURE:
        item["status"] = "failed"
    elif scheduler_state in _SLURM_ACTIVE:
        item["status"] = _SLURM_ACTIVE[scheduler_state]
    elif scheduler_state in _SLURM_TERMINAL_SUCCESS:
        item["status"] = (
            "completed"
            if not config.scheduler["completion_files"] or outputs
            else "finished_missing_outputs"
        )
    elif outputs and reached_end:
        item["status"] = "completed"
    if item.get("status") == "completed":
        item["progress_percent"] = 100.0
    if item.get("status") in {
        "completed",
        "failed",
        "finished_missing_outputs",
        "cancelled",
    }:
        item.setdefault("finished_at", utc_now())
    return item


def refresh_state(
    config: CampaignConfig, cases: list[CaseDefinition]
) -> dict[str, Any]:
    state = load_state(config, cases)
    for case in cases:
        item = inspect_case(config, case, state["cases"][case.name])
        state["cases"][case.name] = item
        case_dir = config.cases_dir / case.name
        if case_dir.is_dir():
            update_case_status_file(case_dir, item)
    save_state(config, state)
    write_status_reports(config, cases, state)
    return state


def status_rows(
    cases: list[CaseDefinition], state: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        item = state["cases"][case.name]
        rows.append(
            {
                "case": case.name,
                **case.parameters,
                "status": item.get("status"),
                "job_id": item.get("job_id"),
                "scheduler_state": item.get("scheduler_state"),
                "attempts": item.get("attempts", 0),
                "latest_time": item.get("latest_time"),
                "end_time": item.get("end_time"),
                "progress_percent": item.get("progress_percent"),
                "courant_mean": item.get("courant_mean"),
                "courant_max": item.get("courant_max"),
                "latest_residuals": json.dumps(
                    item.get("latest_residuals", {}), sort_keys=True
                ),
                "submitted_at": item.get("submitted_at"),
                "finished_at": item.get("finished_at"),
                "detail": item.get("detail"),
            }
        )
    return rows


def write_status_reports(
    config: CampaignConfig,
    cases: list[CaseDefinition],
    state: dict[str, Any],
) -> None:
    rows = status_rows(cases, state)
    write_csv(config.reports_dir / "campaign_status.csv", rows)
    atomic_write_json(
        config.reports_dir / "campaign_status.json",
        {
            "campaign": config.name,
            "generated_at": utc_now(),
            "cases": rows,
        },
    )
