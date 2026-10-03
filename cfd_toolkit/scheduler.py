"""Submit local or Slurm cases and schedule automatic finalization."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .config import (
    CampaignConfig,
    CaseDefinition,
    ConfigurationError,
    format_template,
)
from .io_utils import atomic_write_json, atomic_write_text, utc_now
from .monitor import refresh_state
from .setup import verify_generated_cases
from .state import load_state, save_state, update_case_status_file


_JOB_ID_RE = re.compile(r"(?<!\d)(\d+)(?!\d)")
_RUNNABLE = {
    "not_generated",
    "generated",
    "failed",
    "finished_missing_outputs",
    "not_submitted",
}


def _command(template: str, values: dict[str, Any]) -> list[str]:
    return shlex.split(str(format_template(template, values)))


def _run_capture(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except OSError as exc:
        raise ConfigurationError(
            f"could not execute {command[0]!r}: {exc}"
        ) from exc


def _parse_job_id(output: str) -> str:
    match = _JOB_ID_RE.search(output)
    if not match:
        raise ConfigurationError(
            f"could not parse a Slurm job ID from submission output: {output!r}"
        )
    return match.group(1)


def _selected_cases(
    state: dict[str, Any],
    cases: list[CaseDefinition],
    *,
    include_all: bool,
    limit: int | None,
) -> list[CaseDefinition]:
    selected = [
        case
        for case in cases
        if include_all
        or state["cases"][case.name].get("status") in _RUNNABLE
    ]
    return selected[:limit] if limit is not None else selected


def _run_local_case(
    config: CampaignConfig, case: CaseDefinition
) -> tuple[str, int, str]:
    case_dir = config.cases_dir / case.name
    command = _command(str(config.scheduler["run_command"]), case.values)
    launcher_log = case_dir / str(config.scheduler["launcher_log"])
    launcher_log.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.setdefault("MPLBACKEND", "Agg")
    with launcher_log.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n# toolkit local launch {utc_now()}: "
            f"{shlex.join(command)}\n"
        )
        handle.flush()
        try:
            result = subprocess.run(
                command,
                cwd=case_dir,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=environment,
                check=False,
            )
            return case.name, result.returncode, ""
        except OSError as exc:
            return case.name, 127, str(exc)


def _finalizer_script(
    config: CampaignConfig,
    *,
    toolkit_root: Path,
) -> Path:
    finalizer = config.scheduler.get("finalizer", {})
    resources = {
        "job_name": f"{config.name}_finalize",
        "walltime": "00:30:00",
        "cores": 1,
        "memory": "4G",
        **finalizer.get("resources", {}),
    }
    lines = ["#!/usr/bin/env bash"]
    directives = {
        "--job-name": resources.get("job_name"),
        "--time": resources.get("walltime"),
        "--ntasks": resources.get("cores"),
        "--mem": resources.get("memory"),
        "--partition": resources.get("partition"),
        "--account": resources.get("account"),
    }
    for option, value in directives.items():
        if value not in (None, ""):
            lines.append(f"#SBATCH {option}={value}")
    lines += [
        f"#SBATCH --chdir={config.state_dir}",
        f"#SBATCH --output={config.state_dir / 'finalizer-%j.out'}",
        "",
        "set -euo pipefail",
        "export MPLBACKEND=Agg",
    ]
    lines.extend(str(line) for line in finalizer.get("preamble", []))
    command = [
        str(finalizer.get("python", sys.executable)),
        str(toolkit_root / "scripts" / "finalize_campaign.py"),
        str(config.source),
    ]
    lines.append(shlex.join(command))
    path = config.state_dir / "finalize_campaign.sh"
    atomic_write_text(path, "\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


def _submit_finalizer(
    config: CampaignConfig,
    job_ids: list[str],
    *,
    toolkit_root: Path,
    dry_run: bool,
) -> tuple[str | None, str]:
    script = (
        config.state_dir / "finalize_campaign.sh"
        if dry_run
        else _finalizer_script(config, toolkit_root=toolkit_root)
    )
    values = {
        "dependencies": ":".join(job_ids),
        "script": str(script),
    }
    command = _command(
        str(config.scheduler["dependency_submit_command"]), values
    )
    display = shlex.join(command)
    if dry_run:
        return None, display
    result = _run_capture(command, config.state_dir)
    if result.returncode != 0:
        raise ConfigurationError(
            "finalizer submission failed with exit code "
            f"{result.returncode}: {result.stdout.strip()}"
        )
    return _parse_job_id(result.stdout), display


def submit_cases(
    config: CampaignConfig,
    cases: list[CaseDefinition],
    *,
    dry_run: bool = False,
    include_all: bool = False,
    limit: int | None = None,
    jobs: int = 1,
    toolkit_root: Path,
) -> dict[str, Any]:
    if jobs < 1:
        raise ConfigurationError("--jobs must be at least 1")
    if limit is not None and limit < 1:
        raise ConfigurationError("--limit must be at least 1")
    state = load_state(config, cases)
    selected = _selected_cases(
        state, cases, include_all=include_all, limit=limit
    )
    for case in selected:
        if not dry_run and not (config.cases_dir / case.name).is_dir():
            raise ConfigurationError(
                f"case is not generated: {config.cases_dir / case.name}"
            )

    result_summary: dict[str, Any] = {
        "selected": len(selected),
        "selected_case_names": [case.name for case in selected],
        "submitted": 0,
        "completed": 0,
        "failed": 0,
        "commands": [],
        "job_ids": [],
        "finalizer_job_id": None,
        "finalizer_error": None,
    }
    if not selected:
        return result_summary

    if not dry_run:
        verify_generated_cases(config, selected)

    if config.scheduler["type"] == "local":
        commands = [
            f"{case.name}: "
            + shlex.join(
                _command(str(config.scheduler["run_command"]), case.values)
            )
            for case in selected
        ]
        result_summary["commands"] = commands
        if dry_run:
            return result_summary
        launch_time = utc_now()
        for case in selected:
            item = state["cases"][case.name]
            item.update(
                status="running",
                attempts=item.get("attempts", 0) + 1,
                submitted_at=launch_time,
                updated_at=launch_time,
            )
        save_state(config, state)
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(_run_local_case, config, case): case
                for case in selected
            }
            for future in as_completed(futures):
                name, return_code, detail = future.result()
                item = state["cases"][name]
                item.update(
                    status="completed" if return_code == 0 else "failed",
                    return_code=return_code,
                    finished_at=utc_now(),
                    updated_at=utc_now(),
                )
                if detail:
                    item["detail"] = detail
                update_case_status_file(config.cases_dir / name, item)
                result_summary[
                    "completed" if return_code == 0 else "failed"
                ] += 1
        save_state(config, state)
        refresh_state(config, cases)
        return result_summary

    if not dry_run:
        first_case = selected[0]
        preflight_values = {
            **first_case.values,
            "script": str(config.scheduler["script"]),
            "case": first_case.name,
        }
        submit_executable = _command(
            str(config.scheduler["submit_command"]), preflight_values
        )[0]
        if shutil.which(submit_executable) is None:
            raise ConfigurationError(
                f"Slurm submission command {submit_executable!r} is not "
                "available. Run the real submission on the CHTC Slurm "
                "login node; use --dry-run on a laptop."
            )

    for case in selected:
        script = str(config.scheduler["script"])
        values = {**case.values, "script": script, "case": case.name}
        command = _command(str(config.scheduler["submit_command"]), values)
        result_summary["commands"].append(
            f"{case.name}: {shlex.join(command)}"
        )
        if dry_run:
            continue
        submission = _run_capture(command, config.cases_dir / case.name)
        item = state["cases"][case.name]
        item["attempts"] = item.get("attempts", 0) + 1
        item["submitted_at"] = utc_now()
        item["updated_at"] = utc_now()
        if submission.returncode != 0:
            item.update(
                status="failed",
                return_code=submission.returncode,
                detail=submission.stdout.strip(),
            )
            result_summary["failed"] += 1
        else:
            job_id = _parse_job_id(submission.stdout)
            item.update(
                status="submitted",
                job_id=job_id,
                scheduler_state="SUBMITTED",
                submission_output=submission.stdout.strip(),
            )
            atomic_write_json(
                config.cases_dir / case.name / "submission.json",
                {
                    "campaign": config.name,
                    "case": case.name,
                    "job_id": job_id,
                    "submitted_at": item["submitted_at"],
                    "command": command,
                    "output": submission.stdout.strip(),
                },
            )
            result_summary["job_ids"].append(job_id)
            result_summary["submitted"] += 1
        update_case_status_file(config.cases_dir / case.name, item)
        save_state(config, state)

    if (
        result_summary["job_ids"]
        and config.data["workflow"].get("automatic_finalization", True)
    ):
        dependency_job_ids = list(
            dict.fromkeys(
                [
                    *result_summary["job_ids"],
                    *(
                        str(item["job_id"])
                        for item in state["cases"].values()
                        if item.get("job_id")
                        and item.get("status")
                        in {"submitted", "queued", "running"}
                    ),
                ]
            )
        )
        try:
            finalizer_job_id, finalizer_command = _submit_finalizer(
                config,
                dependency_job_ids,
                toolkit_root=toolkit_root,
                dry_run=False,
            )
        except ConfigurationError as exc:
            result_summary["finalizer_error"] = str(exc)
            state["finalizer"] = {
                "job_id": None,
                "dependencies": dependency_job_ids,
                "submitted_at": utc_now(),
                "error": str(exc),
            }
        else:
            state["finalizer"] = {
                "job_id": finalizer_job_id,
                "dependencies": dependency_job_ids,
                "submitted_at": utc_now(),
                "command": finalizer_command,
            }
            result_summary["finalizer_job_id"] = finalizer_job_id
        save_state(config, state)
    elif dry_run and config.data["workflow"].get(
        "automatic_finalization", True
    ):
        placeholder_ids = [f"JOB_{case.name}" for case in selected]
        _, display = _submit_finalizer(
            config,
            placeholder_ids,
            toolkit_root=toolkit_root,
            dry_run=True,
        )
        result_summary["commands"].append(f"finalizer: {display}")

    return result_summary
