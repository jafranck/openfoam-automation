"""Configuration loading, validation, case expansion, and safe expressions."""

from __future__ import annotations

import ast
import itertools
import json
import math
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """Raised when a campaign configuration is invalid."""


_BINARY_OPERATORS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}
_UNARY_OPERATORS = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}
_FUNCTIONS = {
    "abs": abs,
    "ceil": math.ceil,
    "floor": math.floor,
    "max": max,
    "min": min,
    "round": round,
    "sqrt": math.sqrt,
}
_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_WALLTIME_RE = re.compile(
    r"^(?:(?P<days>\d+)-)?(?P<hours>\d+):"
    r"(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)$"
)
_UNEXPANDED_ENV_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")
_POLYMESH_REQUIRED_FILES = ("boundary", "faces", "neighbour", "owner", "points")

_COMPACT_CFT_PRESET: dict[str, Any] = {
    "derived": {
        "span": "span_ratio * chord",
        "nu": "U_inf * chord / Re",
        "omega": "TSR * U_inf / radius",
        "period": "2 * pi / omega",
        "end_time": "cycles * period",
    },
    "case_name": "Re_{Re:.0f}_TSR_{TSR:g}",
    "case_setup": {
        "mesh_destination": "constant/polyMesh",
        "copy_ignore": [
            "processor*",
            "postProcessing",
            "output",
            "log.*",
            "slurm-*.out",
            "run_status.txt",
            "submission.json",
        ],
        "foam_entries": [
            {
                "file": "constant/transportProperties",
                "keyword": "nu",
                "value": "{nu:.12g}",
            },
            {
                "file": "constant/dynamicMeshDict",
                "keyword": "omega",
                "value": "{omega:.12g}",
            },
            {
                "file": "system/controlDict",
                "keyword": "endTime",
                "value": "{end_time:.12g}",
            },
            {
                "file": "system/decomposeParDict",
                "keyword": "numberOfSubdomains",
                "value": "{cores}",
            },
        ],
        "literal_replacements": [],
    },
    "scheduler": {
        "type": "slurm",
        "script": "script.sh",
        "submit_command": "sbatch --parsable {script}",
        "dependency_submit_command": (
            "sbatch --parsable --dependency=afterany:{dependencies} {script}"
        ),
        "solver_log": "output/foamout",
        "launcher_log": "output/toolkit_launcher.log",
        "completion_files": ["postProcessing/forces_foil1/*/forces.dat"],
        "failure_patterns": [
            "FOAM FATAL ERROR",
            "Segmentation fault",
            "Floating point exception",
        ],
        "resources": {
            "cores": "{cores}",
            "walltime": "{walltime}",
            "memory": None,
            "partition": None,
            "account": None,
        },
        "finalizer": {
            "resources": {
                "walltime": "00:30:00",
                "cores": 1,
                "memory": "4G",
                "partition": "shared",
                "account": None,
            },
            "preamble": [],
        },
    },
    "monitoring": {"control_dict": "system/controlDict"},
    "postprocessing": {
        "enabled": True,
        "force_objects": ["forces_foil1"],
        "auto_discover_force_objects": False,
        "cycles": "from_case",
        "include_partial_cycles": True,
        "complete_cycle_minimum_degrees": 350.0,
        "angle_offset_degrees": 0.0,
        "duplicate_time_tolerance": 1e-10,
        "force_coefficient_denominator": "0.5 * U_inf**2 * chord",
        "power_coefficient_denominator": (
            "0.5 * U_inf**3 * span * radius"
        ),
        "moment_axis": "z",
        "plot_dpi": 220,
    },
    "workflow": {
        "automatic_finalization": True,
        "poll_interval_seconds": 60,
    },
    "limits": {"max_cases": 10000},
}


def _json_object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(
                f"duplicate JSON key {key!r}; each setting must appear once"
            )
        result[key] = value
    return result


def _valid_walltime(value: str) -> bool:
    match = _WALLTIME_RE.fullmatch(value)
    if not match:
        return False
    days = int(match.group("days") or 0)
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    if match.group("days") is not None and hours > 23:
        return False
    return days + hours + minutes + seconds > 0


def _finite_number(label: str, value: Any, case_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(
            f"{label} must be numeric (case {case_name})"
        )
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ConfigurationError(
            f"{label} must be finite (case {case_name})"
        )
    return numeric


def _whole_positive_number(label: str, value: Any, case_name: str) -> int:
    numeric = _finite_number(label, value, case_name)
    if numeric < 1 or not numeric.is_integer():
        raise ConfigurationError(
            f"{label} must be a positive whole number (case {case_name})"
        )
    return int(numeric)


def _merge_mappings(
    defaults: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """Recursively apply advanced overrides to preset defaults."""

    result = deepcopy(defaults)
    for key, value in overrides.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = _merge_mappings(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _expand_compact_config(data: dict[str, Any]) -> dict[str, Any]:
    """Translate the user-friendly global-parameters layout to schema v2."""

    compact_keys = [
        key for key in ("global_parameters", "global_settings") if key in data
    ]
    if not compact_keys:
        return data
    if len(compact_keys) > 1:
        raise ConfigurationError(
            "use only one of global_parameters or global_settings"
        )
    if "constants" in data or "parameters" in data:
        raise ConfigurationError(
            "compact configuration uses global_parameters + cases; "
            "do not also define constants or parameters"
        )
    if data.get("preset", "cft_slurm") != "cft_slurm":
        raise ConfigurationError(
            "the compact configuration currently supports preset 'cft_slurm'"
        )

    compact_key = compact_keys[0]
    globals_raw = data.pop(compact_key)
    if not isinstance(globals_raw, dict):
        raise ConfigurationError(f"{compact_key!r} must be a JSON object")
    globals_data = deepcopy(globals_raw)

    run_script_values = [
        globals_data.pop(key)
        for key in ("run_script", "script")
        if key in globals_data
    ]
    if len(run_script_values) > 1:
        raise ConfigurationError(
            "use only one of global_parameters.run_script or "
            "global_parameters.script"
        )
    run_script = (
        run_script_values[0]
        if run_script_values
        else data.pop("run_script", "script.sh")
    )
    if not isinstance(run_script, str) or not run_script.strip():
        raise ConfigurationError("run_script must be a non-empty filename")

    required_globals = {
        "U_inf",
        "chord",
        "radius",
        "span_ratio",
        "cycles",
        "cores",
        "walltime",
    }
    missing_globals = sorted(required_globals - globals_data.keys())
    if missing_globals:
        raise ConfigurationError(
            "global_parameters is missing: " + ", ".join(missing_globals)
        )

    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ConfigurationError(
            "compact configuration requires a non-empty cases list"
        )
    for index, case in enumerate(raw_cases, start=1):
        if not isinstance(case, dict):
            raise ConfigurationError(
                f"cases item {index} must be a JSON object"
            )
        missing_case = [key for key in ("Re", "TSR") if key not in case]
        if missing_case:
            raise ConfigurationError(
                f"cases item {index} is missing: {', '.join(missing_case)}"
            )

    advanced_sections = (
        "derived",
        "case_setup",
        "scheduler",
        "monitoring",
        "postprocessing",
        "workflow",
        "limits",
    )
    preset = deepcopy(_COMPACT_CFT_PRESET)
    for section in advanced_sections:
        override = data.get(section)
        if override is not None:
            if not isinstance(override, dict):
                raise ConfigurationError(
                    f"{section!r} must be a JSON object"
                )
            preset[section] = _merge_mappings(preset[section], override)
        data[section] = preset[section]

    data["scheduler"]["script"] = run_script
    data["constants"] = globals_data
    data.setdefault("case_name", preset["case_name"])
    data["preset"] = "cft_slurm"
    return data


def safe_eval(expression: str, values: dict[str, Any]) -> float:
    """Evaluate arithmetic without permitting Python code execution."""

    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in values and isinstance(values[node.id], (int, float)):
                return values[node.id]
            raise ConfigurationError(
                f"unknown or non-numeric name {node.id!r} in expression {expression!r}"
            )
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            return _BINARY_OPERATORS[type(node.op)](
                evaluate(node.left), evaluate(node.right)
            )
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            return _UNARY_OPERATORS[type(node.op)](evaluate(node.operand))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCTIONS
            and not node.keywords
        ):
            return _FUNCTIONS[node.func.id](*(evaluate(arg) for arg in node.args))
        raise ConfigurationError(f"unsafe element in expression {expression!r}")

    try:
        parsed = ast.parse(str(expression), mode="eval")
        result = evaluate(parsed)
    except (SyntaxError, ArithmeticError, TypeError) as exc:
        raise ConfigurationError(
            f"could not evaluate expression {expression!r}: {exc}"
        ) from exc
    if not isinstance(result, (int, float)) or not math.isfinite(float(result)):
        raise ConfigurationError(
            f"expression {expression!r} did not produce a finite number"
        )
    return result


def format_template(value: Any, values: dict[str, Any]) -> Any:
    """Format strings with case values; return non-strings unchanged."""

    if not isinstance(value, str):
        return value
    try:
        return value.format(**values)
    except (KeyError, ValueError) as exc:
        raise ConfigurationError(f"could not format template {value!r}: {exc}") from exc


@dataclass(frozen=True)
class CaseDefinition:
    name: str
    index: int
    parameters: dict[str, Any]
    values: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "index": self.index,
            "parameters": self.parameters,
            "values": self.values,
        }


@dataclass
class CampaignConfig:
    source: Path
    data: dict[str, Any]
    base_case: Path
    mesh: Path | None
    campaign_dir: Path
    cases_dir: Path
    reports_dir: Path
    figures_dir: Path
    state_dir: Path

    @property
    def name(self) -> str:
        return str(self.data["name"])

    @property
    def scheduler(self) -> dict[str, Any]:
        return self.data["scheduler"]

    @property
    def postprocessing(self) -> dict[str, Any]:
        return self.data["postprocessing"]


def _resolve_path(config_dir: Path, raw: str | None) -> Path | None:
    if raw in (None, ""):
        return None
    expanded = os.path.expandvars(str(raw))
    if _UNEXPANDED_ENV_RE.search(expanded):
        raise ConfigurationError(
            f"path contains an undefined environment variable: {raw}"
        )
    path = Path(expanded).expanduser()
    return path.resolve() if path.is_absolute() else (config_dir / path).resolve()


def _require_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key!r} must be a JSON object")
    return value


def load_config(config_path: str | Path) -> CampaignConfig:
    source = Path(config_path).expanduser().resolve()
    try:
        data = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"invalid JSON in {source}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigurationError("configuration root must be a JSON object")
    data = deepcopy(data)
    if data.get("schema_version", 2) != 2:
        raise ConfigurationError("this toolkit requires schema_version 2")
    data = _expand_compact_config(data)

    name = data.get("name", source.stem)
    if not isinstance(name, str) or not _SAFE_CASE_RE.fullmatch(name):
        raise ConfigurationError(
            "name must start with a letter or number and contain only "
            "letters, numbers, '.', '_', '+', or '-'"
        )
    data["name"] = name

    paths = _require_mapping(data, "paths")
    config_dir = source.parent
    base_case = _resolve_path(config_dir, paths.get("base_case"))
    if base_case is None:
        raise ConfigurationError("paths.base_case is required")
    mesh = _resolve_path(config_dir, paths.get("mesh"))
    campaigns_root = _resolve_path(config_dir, paths.get("campaigns_root"))
    figures_root = _resolve_path(config_dir, paths.get("figures_root"))
    if campaigns_root is None or figures_root is None:
        raise ConfigurationError(
            "paths.campaigns_root and paths.figures_root are required"
        )

    campaign_dir = campaigns_root / name
    cases_dir = campaign_dir / "cases"
    reports_dir = campaign_dir / "reports"
    figures_dir = figures_root / name
    state_dir = campaign_dir / ".campaign"

    data.setdefault("constants", {})
    data.setdefault("derived", {})
    data.setdefault("parameters", {})
    data.setdefault("case_name", "case_{index:04d}")
    data.setdefault("case_setup", {})
    data["case_setup"].setdefault(
        "copy_ignore",
        [
            "processor*",
            "postProcessing",
            "output",
            "log.*",
            "slurm-*.out",
            "run_status.txt",
            "submission.json",
        ],
    )
    data.setdefault("monitoring", {})
    data.setdefault("limits", {})
    data["limits"].setdefault("max_cases", 10000)
    data.setdefault("workflow", {})
    data["workflow"].setdefault("automatic_finalization", True)
    data["workflow"].setdefault("poll_interval_seconds", 60)

    scheduler = data.setdefault("scheduler", {})
    scheduler.setdefault("type", "slurm")
    scheduler.setdefault("script", "script.sh")
    scheduler.setdefault("submit_command", "sbatch --parsable {script}")
    scheduler.setdefault(
        "dependency_submit_command",
        "sbatch --parsable --dependency=afterany:{dependencies} {script}",
    )
    scheduler.setdefault("run_command", "./Allrun")
    scheduler.setdefault("solver_log", "output/foamout")
    scheduler.setdefault("launcher_log", "output/toolkit_launcher.log")
    scheduler.setdefault("completion_files", [])
    scheduler.setdefault("failure_patterns", [])
    scheduler.setdefault("resources", {})
    scheduler.setdefault("finalizer", {})

    post = data.setdefault("postprocessing", {})
    post.setdefault("enabled", True)
    post.setdefault("force_objects", ["forces_foil1"])
    post.setdefault("auto_discover_force_objects", False)
    post.setdefault("cycles", "from_case")
    post.setdefault("include_partial_cycles", True)
    post.setdefault("complete_cycle_minimum_degrees", 350.0)
    post.setdefault("angle_offset_degrees", 0.0)
    post.setdefault("duplicate_time_tolerance", 1e-10)
    post.setdefault(
        "force_coefficient_denominator", "0.5 * U_inf**2 * chord"
    )
    post.setdefault(
        "power_coefficient_denominator",
        "0.5 * U_inf**3 * span * radius",
    )
    post.setdefault("moment_axis", "z")
    post.setdefault("plot_dpi", 220)

    return CampaignConfig(
        source=source,
        data=data,
        base_case=base_case,
        mesh=mesh,
        campaign_dir=campaign_dir,
        cases_dir=cases_dir,
        reports_dir=reports_dir,
        figures_dir=figures_dir,
        state_dir=state_dir,
    )


def build_cases(config: CampaignConfig) -> list[CaseDefinition]:
    data = config.data
    constants = data["constants"]
    derived = data["derived"]
    if not isinstance(constants, dict) or not isinstance(derived, dict):
        raise ConfigurationError("constants and derived must be JSON objects")
    for key in [*constants, *derived]:
        if not _SAFE_NAME_RE.fullmatch(str(key)):
            raise ConfigurationError(f"invalid variable name: {key!r}")

    if "cases" in data:
        raw_cases = data["cases"]
        if not isinstance(raw_cases, list) or not all(
            isinstance(item, dict) for item in raw_cases
        ):
            raise ConfigurationError("cases must be a list of JSON objects")
    else:
        parameters = data.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ConfigurationError("parameters must be a JSON object")
        for key, values in parameters.items():
            if not _SAFE_NAME_RE.fullmatch(str(key)):
                raise ConfigurationError(f"invalid parameter name: {key!r}")
            if not isinstance(values, list) or not values:
                raise ConfigurationError(
                    f"parameter {key!r} must contain a non-empty JSON list"
                )
        keys = list(parameters)
        raw_cases = [
            dict(zip(keys, combination))
            for combination in itertools.product(
                *(parameters[key] for key in keys)
            )
        ]
        if not keys:
            raw_cases = [{}]

    cases: list[CaseDefinition] = []
    for index, parameters in enumerate(raw_cases, start=1):
        values = {"pi": math.pi, **constants, **parameters, "index": index}
        for key, expression in derived.items():
            values[key] = safe_eval(str(expression), values)
        try:
            name = str(data["case_name"]).format(**values)
        except (KeyError, ValueError) as exc:
            raise ConfigurationError(
                f"could not format case_name for case {index}: {exc}"
            ) from exc
        if not _SAFE_CASE_RE.fullmatch(name):
            raise ConfigurationError(
                f"unsafe case name {name!r}; use only letters, numbers, '.', "
                "'_', '+', and '-'"
            )
        cases.append(
            CaseDefinition(
                name=name,
                index=index,
                parameters=dict(parameters),
                values=values,
            )
        )

    names = [case.name for case in cases]
    if len(names) != len(set(names)):
        raise ConfigurationError("case_name creates duplicate case directories")
    return cases


def validate_config(
    config: CampaignConfig, cases: list[CaseDefinition]
) -> list[str]:
    """Validate all static inputs and return non-fatal warnings."""

    warnings: list[str] = []
    if not config.base_case.is_dir():
        raise ConfigurationError(
            f"base OpenFOAM case is not a directory: {config.base_case}"
        )
    if config.mesh is not None:
        mesh_dir = (
            config.mesh / "polyMesh"
            if (config.mesh / "polyMesh").is_dir()
            else config.mesh
        )
        if not mesh_dir.is_dir():
            raise ConfigurationError(
                "paths.mesh must point to a polyMesh directory or to a "
                f"directory containing polyMesh: {config.mesh}"
            )
        if not any(mesh_dir.iterdir()):
            raise ConfigurationError(f"mesh directory is empty: {mesh_dir}")
        missing_mesh_files = [
            filename
            for filename in _POLYMESH_REQUIRED_FILES
            if not (mesh_dir / filename).is_file()
        ]
        if missing_mesh_files:
            raise ConfigurationError(
                f"mesh directory is missing required polyMesh files "
                f"({', '.join(missing_mesh_files)}): {mesh_dir}"
            )

    if not cases:
        raise ConfigurationError("campaign produces zero cases")
    max_cases = int(config.data["limits"].get("max_cases", 10000))
    if max_cases < 1:
        raise ConfigurationError("limits.max_cases must be positive")
    if len(cases) > max_cases:
        raise ConfigurationError(
            f"campaign expands to {len(cases)} cases, exceeding "
            f"limits.max_cases={max_cases}"
        )

    if (
        config.campaign_dir == config.base_case
        or config.campaign_dir in config.base_case.parents
        or config.base_case in config.campaign_dir.parents
    ):
        raise ConfigurationError(
            "campaign output and base_case must not contain one another"
        )
    if config.figures_dir == config.campaign_dir:
        raise ConfigurationError(
            "figures_root must be separate from campaigns_root"
        )

    setup = config.data["case_setup"]
    if not isinstance(setup, dict):
        raise ConfigurationError("case_setup must be a JSON object")
    foam_entries = setup.get("foam_entries", [])
    literal_replacements = setup.get("literal_replacements", [])
    if not isinstance(foam_entries, list) or not isinstance(
        literal_replacements, list
    ):
        raise ConfigurationError(
            "case_setup.foam_entries and literal_replacements must be lists"
        )
    for edit in foam_entries:
        if not isinstance(edit, dict) or not all(
            key in edit for key in ("file", "keyword", "value")
        ):
            raise ConfigurationError(
                "every foam_entries item requires file, keyword, and value"
            )
    for edit in literal_replacements:
        if not isinstance(edit, dict) or not all(
            key in edit for key in ("file", "old", "new")
        ):
            raise ConfigurationError(
                "every literal_replacements item requires file, old, and new"
            )
    for edit in [*foam_entries, *literal_replacements]:
        relative = Path(str(edit["file"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ConfigurationError(
                f"case setup file must stay inside the case: {relative}"
            )
        target = config.base_case / relative
        if not target.is_file():
            raise ConfigurationError(f"base-case edit target is missing: {target}")
    mesh_destination = Path(
        str(setup.get("mesh_destination", "constant/polyMesh"))
    )
    if mesh_destination.is_absolute() or ".." in mesh_destination.parts:
        raise ConfigurationError(
            "case_setup.mesh_destination must stay inside the generated case"
        )
    copy_ignore = setup.get("copy_ignore", [])
    if not isinstance(copy_ignore, list) or not all(
        isinstance(item, str) for item in copy_ignore
    ):
        raise ConfigurationError("case_setup.copy_ignore must be a list of strings")

    scheduler = config.scheduler
    scheduler_type = scheduler.get("type")
    if scheduler_type not in {"slurm", "local"}:
        raise ConfigurationError("scheduler.type must be 'slurm' or 'local'")
    if scheduler_type == "slurm":
        script_relative = Path(str(scheduler["script"]))
        if script_relative.is_absolute() or ".." in script_relative.parts:
            raise ConfigurationError(
                "scheduler.script must stay inside the generated case"
            )
        script = config.base_case / script_relative
        if not script.is_file():
            detected = sorted(
                str(path.relative_to(config.base_case))
                for path in config.base_case.rglob("*.sh")
                if path.is_file()
            )
            detected_text = (
                "; detected shell scripts: " + ", ".join(detected)
                if detected
                else "; no .sh files were detected in the base case"
            )
            raise ConfigurationError(
                f"configured Slurm script is missing: {script}. "
                "Set global_parameters.run_script to the exact base-case "
                f"filename{detected_text}"
            )
        script_bytes = script.read_bytes()
        if not script_bytes.startswith(b"#!"):
            raise ConfigurationError(
                f"configured Slurm script must start with a shebang (#!): {script}"
            )
        if b"\r\n" in script_bytes:
            raise ConfigurationError(
                f"configured Slurm script uses Windows line endings; convert "
                f"it with 'sed -i s/\\\\r$// {script_relative}': {script}"
            )
        script_text = script_bytes.decode("utf-8", errors="replace")
        if re.search(r"(?m)^\s*#SBATCH\s+--tasks-per-node(?:=|\s)", script_text):
            warnings.append(
                "the base run script uses legacy --tasks-per-node; the "
                "generated copy will use CHTC/Slurm --ntasks-per-node"
            )
        for log_key in ("solver_log", "launcher_log"):
            log_path = Path(str(scheduler.get(log_key, "")))
            if log_path.is_absolute() or ".." in log_path.parts:
                raise ConfigurationError(
                    f"scheduler.{log_key} must stay inside the generated case"
                )
        resources = scheduler["resources"]
        for case in cases:
            cores = format_template(resources.get("cores", 1), case.values)
            try:
                cores_number = float(cores)
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"scheduler.resources.cores is not numeric for "
                    f"{case.name}: {cores!r}"
                ) from exc
            if (
                not math.isfinite(cores_number)
                or cores_number < 1
                or not cores_number.is_integer()
            ):
                raise ConfigurationError(
                    "scheduler.resources.cores must be a positive whole "
                    f"number (case {case.name})"
                )
            walltime = str(
                format_template(
                    resources.get("walltime", "01:00:00"), case.values
                )
            )
            if not _valid_walltime(walltime):
                raise ConfigurationError(
                    "scheduler.resources.walltime must be HH:MM:SS or "
                    f"D-HH:MM:SS with valid minute/second fields "
                    f"(case {case.name})"
                )

    post = config.postprocessing
    if post["moment_axis"] not in {"x", "y", "z"}:
        raise ConfigurationError("postprocessing.moment_axis must be x, y, or z")
    try:
        duplicate_tolerance = float(post["duplicate_time_tolerance"])
        minimum_degrees = float(post["complete_cycle_minimum_degrees"])
        plot_dpi = int(post["plot_dpi"])
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            "post-processing tolerances, cycle coverage, and plot_dpi must "
            "be numeric"
        ) from exc
    if duplicate_tolerance < 0 or not math.isfinite(duplicate_tolerance):
        raise ConfigurationError(
            "postprocessing.duplicate_time_tolerance must be finite and "
            "non-negative"
        )
    if not 0 < minimum_degrees <= 360:
        raise ConfigurationError(
            "postprocessing.complete_cycle_minimum_degrees must be in (0, 360]"
        )
    if plot_dpi < 50:
        raise ConfigurationError("postprocessing.plot_dpi must be at least 50")
    force_objects = post.get("force_objects", [])
    if not isinstance(force_objects, list) or not all(
        isinstance(item, str) and item.strip() for item in force_objects
    ):
        raise ConfigurationError(
            "postprocessing.force_objects must be a list of non-empty names"
        )
    for force_object in force_objects:
        relative = Path(force_object)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConfigurationError(
                f"force object must stay inside postProcessing: {force_object}"
            )
    for case in cases:
        for positive_key in (
            "Re",
            "U_inf",
            "chord",
            "radius",
            "span_ratio",
            "rho",
            "nu",
            "omega",
            "period",
            "end_time",
        ):
            if positive_key in case.values:
                numeric = _finite_number(
                    positive_key, case.values[positive_key], case.name
                )
                if numeric <= 0:
                    raise ConfigurationError(
                        f"{positive_key} must be positive (case {case.name})"
                    )
        if "TSR" in case.values:
            tsr = _finite_number("TSR", case.values["TSR"], case.name)
            if tsr <= 0:
                raise ConfigurationError(
                    f"TSR must be positive (case {case.name})"
                )
        if "cores" in case.values:
            _whole_positive_number("cores", case.values["cores"], case.name)
        if "cycles" in case.values:
            _whole_positive_number("cycles", case.values["cycles"], case.name)
        for key in (
            "force_coefficient_denominator",
            "power_coefficient_denominator",
        ):
            denominator = safe_eval(str(post[key]), case.values)
            if denominator == 0:
                raise ConfigurationError(
                    f"postprocessing.{key} cannot be zero ({case.name})"
                )
    if "omega" not in cases[0].values:
        warnings.append(
            "no 'omega' value is defined; force post-processing will require "
            "omega in each case manifest"
        )
    return warnings
