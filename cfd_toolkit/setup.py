"""Generate clean, parameterized OpenFOAM cases."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import (
    CampaignConfig,
    CaseDefinition,
    ConfigurationError,
    format_template,
)
from .io_utils import atomic_write_json, utc_now
from .state import load_state, save_state, update_case_status_file


_SBATCH_ALIASES = {
    "cores": (
        "--ntasks",
        "--ntasks-per-node",
        "--tasks-per-node",
        "-n",
    ),
    "walltime": ("--time", "-t"),
    "memory": ("--mem",),
    "partition": ("--partition", "-p"),
    "account": ("--account", "-A"),
    "cpus_per_task": ("--cpus-per-task", "-c"),
    "nodes": ("--nodes", "-N"),
    "job_name": ("--job-name", "-J"),
}
_SBATCH_LOG_RE = re.compile(
    r"(?m)^\s*#SBATCH\s+(?:-o|-e|--output|--error)"
    r"(?:=|\s+)(?P<path>[^\s#]+)"
)
_SHELL_REDIRECTION_RE = re.compile(
    r"(?:^|[\s;])(?:\d*>>?|&>)\s*(?P<path>[^\s;&|]+)"
)
_SBATCH_CANONICAL = {
    "cores": "--ntasks",
    "walltime": "--time",
    "memory": "--mem",
    "partition": "--partition",
    "account": "--account",
    "cpus_per_task": "--cpus-per-task",
    "nodes": "--nodes",
    "job_name": "--job-name",
}


def _mesh_source(config: CampaignConfig) -> Path | None:
    if config.mesh is None:
        return None
    nested = config.mesh / "polyMesh"
    return nested if nested.is_dir() else config.mesh


def _replace_foam_entry(
    text: str,
    *,
    keyword: str,
    value: str,
    occurrence: int | None,
    file_label: str,
) -> str:
    pattern = re.compile(
        rf"(?m)^(\s*{re.escape(keyword)}\s+)"
        rf"((?:\[[^\]\n]*\]\s+)?)([^;\n]+)(;[^\n]*)$"
    )
    matches = list(pattern.finditer(text))
    if not matches:
        raise ConfigurationError(
            f"OpenFOAM keyword {keyword!r} was not found in {file_label}"
        )
    if occurrence is None and len(matches) != 1:
        raise ConfigurationError(
            f"OpenFOAM keyword {keyword!r} occurs {len(matches)} times in "
            f"{file_label}; set an explicit zero-based occurrence"
        )
    index = 0 if occurrence is None else int(occurrence)
    if index < 0 or index >= len(matches):
        raise ConfigurationError(
            f"occurrence {index} for {keyword!r} is outside the "
            f"{len(matches)} matches in {file_label}"
        )
    selected = matches[index]
    replacement = (
        f"{selected.group(1)}{selected.group(2)}{value}{selected.group(4)}"
    )
    return text[: selected.start()] + replacement + text[selected.end() :]


def _apply_case_edits(
    config: CampaignConfig, case: CaseDefinition, target: Path
) -> None:
    setup = config.data["case_setup"]
    for edit in setup.get("foam_entries", []):
        relative = Path(str(edit["file"]))
        path = target / relative
        text = path.read_text(encoding="utf-8")
        value = str(format_template(edit["value"], case.values))
        updated = _replace_foam_entry(
            text,
            keyword=str(edit["keyword"]),
            value=value,
            occurrence=edit.get("occurrence"),
            file_label=str(relative),
        )
        path.write_text(updated, encoding="utf-8")

    for edit in setup.get("literal_replacements", []):
        relative = Path(str(edit["file"]))
        path = target / relative
        text = path.read_text(encoding="utf-8")
        old = str(edit["old"])
        new = str(format_template(edit["new"], case.values))
        expected = edit.get("expected_count")
        actual = text.count(old)
        if actual == 0:
            raise ConfigurationError(
                f"literal token {old!r} was not found in {relative}"
            )
        if expected is not None and actual != int(expected):
            raise ConfigurationError(
                f"literal token {old!r} occurs {actual} times in {relative}; "
                f"expected {expected}"
            )
        path.write_text(text.replace(old, new), encoding="utf-8")


def _patch_slurm_script(
    config: CampaignConfig, case: CaseDefinition, target: Path
) -> dict[str, Any]:
    if config.scheduler["type"] != "slurm":
        return {"directives": [], "srun_task_counts": 0}
    script_path = target / str(config.scheduler["script"])
    text = script_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    resources = dict(config.scheduler.get("resources", {}))
    resources.setdefault("job_name", case.name)

    formatted: dict[str, str] = {}
    for key, raw in resources.items():
        if key in _SBATCH_ALIASES and raw not in (None, ""):
            formatted[key] = str(format_template(raw, case.values))

    found: set[str] = set()
    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#SBATCH"):
            continue
        directive = stripped[len("#SBATCH") :].strip()
        option = directive.split("=", 1)[0].split(maxsplit=1)[0]
        for key, aliases in _SBATCH_ALIASES.items():
            if key in formatted and option in aliases:
                replacement_option = (
                    "--ntasks-per-node"
                    if key == "cores"
                    and option in {
                        "--ntasks-per-node",
                        "--tasks-per-node",
                    }
                    else _SBATCH_CANONICAL[key]
                )
                lines[line_index] = (
                    f"#SBATCH {replacement_option}={formatted[key]}"
                )
                found.add(key)
                break

    additions = [
        f"#SBATCH {_SBATCH_CANONICAL[key]}={formatted[key]}"
        for key in _SBATCH_CANONICAL
        if key in formatted and key not in found
    ]
    if additions:
        insertion = 1 if lines and lines[0].startswith("#!") else 0
        lines[insertion:insertion] = additions

    launcher_task_counts = 0
    if "cores" in formatted:
        long_task_pattern = re.compile(
            r"(?<!\S)(?P<option>--ntasks)"
            r"(?P<separator>=|[ \t]+)(?P<count>[0-9]+)\b"
        )
        short_task_pattern = re.compile(
            r"(?<!\S)(?P<option>-n)"
            r"(?P<separator>[ \t]*)(?P<count>[0-9]+)\b"
        )
        mpi_task_pattern = re.compile(
            r"(?<!\S)(?P<option>-np)"
            r"(?P<separator>[ \t]*)(?P<count>[0-9]+)\b"
        )
        for line_index, line in enumerate(lines):
            if line.lstrip().startswith("#") or not re.search(
                r"\b(?:srun|mpirun|mpiexec)\b", line
            ):
                continue

            def replace_count(match: re.Match[str]) -> str:
                nonlocal launcher_task_counts
                launcher_task_counts += 1
                return (
                    f"{match.group('option')}{match.group('separator')}"
                    f"{formatted['cores']}"
                )

            updated = mpi_task_pattern.sub(replace_count, line)
            updated = long_task_pattern.sub(replace_count, updated)
            lines[line_index] = short_task_pattern.sub(
                replace_count, updated
            )

    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "directives": sorted(
            key for key in formatted if key in found or key in _SBATCH_CANONICAL
        ),
        "launcher_task_counts": launcher_task_counts,
    }


def _static_runtime_paths(script_text: str) -> list[Path]:
    candidates = [
        *(match.group("path") for match in _SBATCH_LOG_RE.finditer(script_text)),
        *(
            match.group("path")
            for line in script_text.splitlines()
            if not line.lstrip().startswith("#")
            for match in _SHELL_REDIRECTION_RE.finditer(line)
        ),
    ]
    paths: list[Path] = []
    for candidate in candidates:
        cleaned = candidate.strip("\"'")
        if (
            not cleaned
            or cleaned.startswith("&")
            or any(character in cleaned for character in "$*?[]{}")
        ):
            continue
        path = Path(cleaned)
        if path.is_absolute() or ".." in path.parts:
            continue
        paths.append(path)
    return paths


def _prepare_runtime_directories(
    config: CampaignConfig, target: Path
) -> list[str]:
    """Create clean parents required by configured runtime log paths."""

    created: list[str] = []
    runtime_paths: list[Path] = []
    for key in ("solver_log", "launcher_log"):
        raw = config.scheduler.get(key)
        if raw in (None, ""):
            continue
        log_path = Path(str(raw))
        if log_path.is_absolute() or ".." in log_path.parts:
            raise ConfigurationError(
                f"scheduler.{key} must stay inside the generated case"
            )
        runtime_paths.append(log_path)
    if config.scheduler["type"] == "slurm":
        script_path = target / str(config.scheduler["script"])
        runtime_paths.extend(
            _static_runtime_paths(
                script_path.read_text(encoding="utf-8", errors="replace")
            )
        )
    for runtime_path in runtime_paths:
        parent = runtime_path.parent
        if parent == Path("."):
            continue
        directory = target / parent
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(str(parent))
    return sorted(set(created))


def _ignored_source_path(relative: Path, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(part, pattern)
        for part in relative.parts
        for pattern in patterns
    )


def _tree_signature(root: Path, ignore_patterns: list[str]) -> str:
    """Hash copied source bytes, paths, modes, and symlink targets once."""

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if _ignored_source_path(relative, ignore_patterns):
            continue
        mode = path.lstat().st_mode & 0o777
        if path.is_symlink():
            digest.update(
                f"L\0{relative.as_posix()}\0{mode:o}\0"
                f"{path.readlink()}\0".encode("utf-8")
            )
        elif path.is_dir():
            digest.update(
                f"D\0{relative.as_posix()}\0{mode:o}\0".encode("utf-8")
            )
        elif path.is_file():
            digest.update(
                f"F\0{relative.as_posix()}\0{mode:o}\0"
                f"{path.stat().st_size}\0".encode("utf-8")
            )
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def _source_signatures(
    config: CampaignConfig, mesh_source: Path | None
) -> dict[str, str | None]:
    ignore_patterns = list(
        config.data["case_setup"].get("copy_ignore", [])
    )
    return {
        "base_case": _tree_signature(config.base_case, ignore_patterns),
        "mesh": (
            _tree_signature(mesh_source, [])
            if mesh_source is not None
            else None
        ),
    }


def _safe_remove_case(case_dir: Path, cases_dir: Path) -> None:
    resolved = case_dir.resolve()
    parent = cases_dir.resolve()
    if resolved.parent != parent or resolved == parent:
        raise ConfigurationError(f"refusing to remove unsafe case path: {resolved}")
    shutil.rmtree(resolved)


def _case_fingerprint(
    config: CampaignConfig,
    case: CaseDefinition,
    mesh_source: Path | None,
    source_signatures: dict[str, str | None],
) -> str:
    payload = {
        "schema_version": 2,
        "case": case.as_dict(),
        "base_case": str(config.base_case),
        "mesh": str(mesh_source) if mesh_source else None,
        "source_signatures": source_signatures,
        "case_setup": config.data["case_setup"],
        "scheduler_script": config.scheduler.get("script"),
        "scheduler_resources": config.scheduler.get("resources", {}),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_generated_cases(
    config: CampaignConfig,
    cases: list[CaseDefinition],
) -> None:
    """Refuse to submit generated cases that no longer match their sources."""

    mesh_source = _mesh_source(config)
    signatures = _source_signatures(config, mesh_source)
    for case in cases:
        case_dir = config.cases_dir / case.name
        manifest_path = case_dir / "case_manifest.json"
        if not case_dir.is_dir() or not manifest_path.is_file():
            raise ConfigurationError(
                f"case is not generated: {case_dir}. Run generate first."
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"generated case manifest is invalid: {manifest_path}"
            ) from exc
        expected = _case_fingerprint(
            config, case, mesh_source, signatures
        )
        if manifest.get("fingerprint") != expected:
            raise ConfigurationError(
                f"generated case is stale or does not match the current "
                f"JSON/base case/mesh: {case.name}. Regenerate with "
                "'generate --force' before submitting."
            )


def generate_campaign(
    config: CampaignConfig,
    cases: list[CaseDefinition],
    *,
    force: bool = False,
) -> dict[str, int]:
    config.cases_dir.mkdir(parents=True, exist_ok=True)
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.figures_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(config, cases)
    counts = {"generated": 0, "skipped": 0}
    mesh_source = _mesh_source(config)
    signatures = _source_signatures(config, mesh_source)
    mesh_destination = Path(
        str(config.data["case_setup"].get("mesh_destination", "constant/polyMesh"))
    )

    for case in cases:
        target = config.cases_dir / case.name
        fingerprint = _case_fingerprint(
            config, case, mesh_source, signatures
        )
        if target.exists() and not force:
            manifest_path = target / "case_manifest.json"
            if manifest_path.is_file():
                try:
                    existing = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError as exc:
                    raise ConfigurationError(
                        f"existing case manifest is invalid: {manifest_path}"
                    ) from exc
                if existing.get("fingerprint") != fingerprint:
                    raise ConfigurationError(
                        f"existing case {case.name} was generated from different "
                        "settings; use generate --force or change the case name"
                    )
            else:
                raise ConfigurationError(
                    f"existing case has no manifest: {target}; move it away or "
                    "use generate --force"
                )
            counts["skipped"] += 1
            continue
        staging = config.cases_dir / (
            f".{case.name}.tmp-{uuid.uuid4().hex}"
        )
        try:
            ignore_patterns = config.data["case_setup"].get("copy_ignore", [])
            shutil.copytree(
                config.base_case,
                staging,
                symlinks=True,
                ignore=shutil.ignore_patterns(*ignore_patterns),
            )
            if mesh_source is not None:
                destination = staging / mesh_destination
                if destination.exists():
                    if destination.is_dir() and not destination.is_symlink():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(mesh_source, destination, symlinks=True)

            _apply_case_edits(config, case, staging)
            runtime_directories = _prepare_runtime_directories(config, staging)
            scheduler_patch = _patch_slurm_script(config, case, staging)
            manifest = {
                "schema_version": 2,
                "campaign": config.name,
                **case.as_dict(),
                "fingerprint": fingerprint,
                "generated_at": utc_now(),
                "source_config": str(config.source),
                "base_case": str(config.base_case),
                "mesh": str(mesh_source) if mesh_source else None,
                "source_signatures": signatures,
                "runtime_adaptation": {
                    "directories_created": runtime_directories,
                    "scheduler_patch": scheduler_patch,
                },
            }
            atomic_write_json(staging / "case_manifest.json", manifest)
            if target.exists():
                _safe_remove_case(target, config.cases_dir)
            staging.replace(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        state["cases"][case.name] = {
            "status": "generated",
            "attempts": 0,
            "parameters": case.parameters,
            "generated_at": manifest["generated_at"],
            "updated_at": manifest["generated_at"],
        }
        update_case_status_file(target, state["cases"][case.name])
        counts["generated"] += 1

    state["config_snapshot"] = str(
        config.state_dir / "config_snapshot.json"
    )
    atomic_write_json(config.state_dir / "config_snapshot.json", config.data)
    atomic_write_json(
        config.state_dir / "campaign_manifest.json",
        {
            "schema_version": 2,
            "campaign": config.name,
            "source_config": str(config.source),
            "campaign_dir": str(config.campaign_dir),
            "reports_dir": str(config.reports_dir),
            "figures_dir": str(config.figures_dir),
            "case_count": len(cases),
            "cases": [case.as_dict() for case in cases],
            "updated_at": utc_now(),
        },
    )
    save_state(config, state)
    return counts
