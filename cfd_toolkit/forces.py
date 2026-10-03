"""Cycle-aware OpenFOAM forces.dat processing and plotting."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import CampaignConfig, CaseDefinition, ConfigurationError, safe_eval
from .io_utils import atomic_write_json, utc_now, write_csv


_FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_FIELDS_19 = (
    "time",
    "Fp_x",
    "Fp_y",
    "Fp_z",
    "Fv_x",
    "Fv_y",
    "Fv_z",
    "Fporous_x",
    "Fporous_y",
    "Fporous_z",
    "Mp_x",
    "Mp_y",
    "Mp_z",
    "Mv_x",
    "Mv_y",
    "Mv_z",
    "Mporous_x",
    "Mporous_y",
    "Mporous_z",
)
_FIELDS_13 = (
    "time",
    "Fp_x",
    "Fp_y",
    "Fp_z",
    "Fv_x",
    "Fv_y",
    "Fv_z",
    "Mp_x",
    "Mp_y",
    "Mp_z",
    "Mv_x",
    "Mv_y",
    "Mv_z",
)
_POROUS_FIELDS = (
    "Fporous_x",
    "Fporous_y",
    "Fporous_z",
    "Mporous_x",
    "Mporous_y",
    "Mporous_z",
)


class ForceDataError(ValueError):
    """Raised when forces.dat data cannot be interpreted safely."""


def _time_directory_key(path: Path) -> tuple[int, float, str]:
    try:
        return (0, float(path.parent.name), str(path))
    except ValueError:
        return (1, math.inf, str(path))


def find_force_objects(
    case_dir: Path, post_config: dict[str, Any]
) -> list[str]:
    configured = [str(name) for name in post_config.get("force_objects", [])]
    if post_config.get("auto_discover_force_objects", False):
        post_root = case_dir / "postProcessing"
        discovered = []
        if post_root.is_dir():
            discovered = [
                path.name
                for path in post_root.iterdir()
                if path.is_dir()
                and "force" in path.name.lower()
                and any(path.rglob("forces.dat"))
            ]
        configured.extend(discovered)
    return list(dict.fromkeys(configured))


def find_force_files(case_dir: Path, force_object: str) -> list[Path]:
    root = case_dir / "postProcessing" / force_object
    if not root.is_dir():
        return []
    return sorted(
        (
            path
            for path in root.rglob("forces.dat")
            if path.is_file() and path.stat().st_size > 0
        ),
        key=_time_directory_key,
    )


def _parse_force_file(
    path: Path, source_order: int
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            numbers = [float(value) for value in _FLOAT_RE.findall(line)]
            if len(numbers) == 19:
                record = dict(zip(_FIELDS_19, numbers))
            elif len(numbers) == 13:
                record = dict(zip(_FIELDS_13, numbers))
                record.update({field: 0.0 for field in _POROUS_FIELDS})
            else:
                raise ForceDataError(
                    f"{path}:{line_number}: expected 13 or 19 numeric values, "
                    f"found {len(numbers)}"
                )
            record["_source_order"] = source_order
            record["_source_file"] = str(path)
            record["_line_number"] = line_number
            records.append(record)
    return records


def read_force_history(
    files: list[Path], duplicate_tolerance: float
) -> list[dict[str, Any]]:
    """Read restart segments and keep the later record at duplicate times."""

    if duplicate_tolerance < 0 or not math.isfinite(duplicate_tolerance):
        raise ForceDataError(
            "duplicate-time tolerance must be finite and non-negative"
        )
    records = [
        record
        for source_order, path in enumerate(files)
        for record in _parse_force_file(path, source_order)
    ]
    records.sort(
        key=lambda row: (
            row["time"],
            row["_source_order"],
            row["_line_number"],
        )
    )
    merged: list[dict[str, Any]] = []
    for record in records:
        if (
            merged
            and abs(record["time"] - merged[-1]["time"])
            <= duplicate_tolerance
        ):
            if record["_source_order"] >= merged[-1]["_source_order"]:
                merged[-1] = record
        else:
            merged.append(record)
    if not merged:
        raise ForceDataError("force files contain no data rows")
    return merged


def _cycle_coordinates(
    time_value: float, omega: float, angle_offset: float
) -> tuple[float, int, float]:
    progress = abs(omega) * time_value * 180.0 / math.pi
    theta_global = angle_offset + progress
    quotient = progress / 360.0
    nearest = round(quotient)
    if progress > 0.0 and abs(quotient - nearest) <= 1e-9:
        cycle = max(1, int(nearest))
        theta_cycle = 360.0
    else:
        cycle = int(math.floor(quotient)) + 1
        theta_cycle = progress - (cycle - 1) * 360.0
    return theta_global, cycle, theta_cycle


def _requested_cycles(
    post_config: dict[str, Any], case: CaseDefinition
) -> int | None:
    setting = post_config.get("cycles", "from_case")
    if setting == "auto":
        return None
    if setting == "from_case":
        setting = case.values.get("cycles")
        if setting is None:
            return None
    try:
        numeric = float(setting)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            "postprocessing.cycles must be 'auto', 'from_case', or an integer"
        ) from exc
    if not math.isfinite(numeric) or numeric < 1 or not numeric.is_integer():
        raise ConfigurationError(
            "postprocessing cycle count must be a positive whole number"
        )
    return int(numeric)


def compute_force_results(
    raw_records: list[dict[str, Any]],
    case: CaseDefinition,
    post_config: dict[str, Any],
) -> list[dict[str, Any]]:
    values = case.values
    try:
        omega = float(values["omega"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ForceDataError(
            f"case {case.name} has no numeric omega in its manifest values"
        ) from exc
    if omega == 0.0:
        raise ForceDataError(f"case {case.name} has omega=0")

    force_denominator = float(
        safe_eval(str(post_config["force_coefficient_denominator"]), values)
    )
    power_denominator = float(
        safe_eval(str(post_config["power_coefficient_denominator"]), values)
    )
    if force_denominator == 0.0 or power_denominator == 0.0:
        raise ForceDataError("coefficient denominator cannot be zero")
    moment_axis = str(post_config["moment_axis"])
    angle_offset = float(post_config.get("angle_offset_degrees", 0.0))
    requested_cycles = _requested_cycles(post_config, case)

    results: list[dict[str, Any]] = []
    for raw in raw_records:
        force = {
            axis: raw[f"Fp_{axis}"]
            + raw[f"Fv_{axis}"]
            + raw[f"Fporous_{axis}"]
            for axis in ("x", "y", "z")
        }
        moment = {
            axis: raw[f"Mp_{axis}"]
            + raw[f"Mv_{axis}"]
            + raw[f"Mporous_{axis}"]
            for axis in ("x", "y", "z")
        }
        theta_global, cycle, theta_cycle = _cycle_coordinates(
            raw["time"], omega, angle_offset
        )
        if requested_cycles is not None and cycle > requested_cycles:
            continue
        result = {
            "time": raw["time"],
            "theta_global_deg": theta_global,
            "cycle": cycle,
            "theta_cycle_deg": theta_cycle,
            "Fp_x": raw["Fp_x"],
            "Fp_y": raw["Fp_y"],
            "Fp_z": raw["Fp_z"],
            "Fv_x": raw["Fv_x"],
            "Fv_y": raw["Fv_y"],
            "Fv_z": raw["Fv_z"],
            "Fporous_x": raw["Fporous_x"],
            "Fporous_y": raw["Fporous_y"],
            "Fporous_z": raw["Fporous_z"],
            "Fx": force["x"],
            "Fy": force["y"],
            "Fz": force["z"],
            "Mx": moment["x"],
            "My": moment["y"],
            "Mz": moment["z"],
            "CFx": force["x"] / force_denominator,
            "CFy": force["y"] / force_denominator,
            "CFz": force["z"] / force_denominator,
            "CP": omega * moment[moment_axis] / power_denominator,
            "source_file": raw["_source_file"],
        }
        results.append(result)
    if not results:
        raise ForceDataError(
            f"no force records remain after cycle selection for {case.name}"
        )
    return results


def _stats(values: list[float], prefix: str) -> dict[str, float]:
    return {
        f"mean_{prefix}": statistics.fmean(values),
        f"min_{prefix}": min(values),
        f"max_{prefix}": max(values),
    }


def cycle_summaries(
    case: CaseDefinition,
    force_object: str,
    results: list[dict[str, Any]],
    post_config: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[int(result["cycle"])].append(result)
    rows: list[dict[str, Any]] = []
    minimum = float(post_config["complete_cycle_minimum_degrees"])
    include_partial = bool(post_config["include_partial_cycles"])
    for cycle, records in sorted(grouped.items()):
        angles = [float(row["theta_cycle_deg"]) for row in records]
        coverage = max(angles) - min(angles)
        complete = coverage >= minimum
        if not complete and not include_partial:
            continue
        row: dict[str, Any] = {
            "case": case.name,
            **case.parameters,
            "force_object": force_object,
            "cycle": cycle,
            "sample_count": len(records),
            "complete_cycle": complete,
            "theta_start_deg": min(angles),
            "theta_end_deg": max(angles),
            "coverage_deg": coverage,
            "time_start": min(float(record["time"]) for record in records),
            "time_end": max(float(record["time"]) for record in records),
        }
        for field in ("CFx", "CFy", "CFz", "CP", "Fx", "Fy", "Fz", "Mz"):
            row.update(
                _stats(
                    [float(record[field]) for record in records],
                    field,
                )
            )
        rows.append(row)
    return rows


def _load_plotting():
    try:
        os.environ.setdefault(
            "MPLCONFIGDIR",
            str(Path(tempfile.gettempdir()) / "cfd_toolkit_matplotlib"),
        )
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
    except ImportError as exc:
        raise ForceDataError(
            "matplotlib is required for force plots. Install it with "
            "'python3 -m pip install -r requirements.txt'."
        ) from exc
    return plt, ticker


def _style_axes(axes: Any, ticker: Any) -> None:
    labels = (r"$C_{F_x}$", r"$C_{F_y}$", r"$C_P$")
    for axis, label in zip(axes, labels):
        axis.set_ylabel(label)
        axis.grid(True, alpha=0.3)
        axis.xaxis.set_major_locator(ticker.MultipleLocator(90))
    axes[-1].set_xlabel(r"Rotor angle, $\theta$ [deg]")


def plot_force_results(
    config: CampaignConfig,
    case: CaseDefinition,
    force_object: str,
    results: list[dict[str, Any]],
) -> list[Path]:
    plt, ticker = _load_plotting()
    output = config.figures_dir / case.name / force_object
    output.mkdir(parents=True, exist_ok=True)
    for pattern in (
        "cycle_*.png",
        "cycles_overlay.png",
        "full_history.png",
    ):
        for old_plot in output.glob(pattern):
            old_plot.unlink()
    dpi = int(config.postprocessing["plot_dpi"])
    created: list[Path] = []

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[int(result["cycle"])].append(result)

    for cycle, records in sorted(grouped.items()):
        fig, axes = plt.subplots(3, 1, figsize=(9, 8), sharex=True)
        x = [row["theta_cycle_deg"] for row in records]
        axes[0].plot(x, [row["CFx"] for row in records], lw=1.4)
        axes[1].plot(x, [row["CFy"] for row in records], lw=1.4)
        axes[2].plot(x, [row["CP"] for row in records], lw=1.4)
        _style_axes(axes, ticker)
        axes[-1].set_xlim(0, 360)
        fig.suptitle(f"{case.name} | {force_object} | Cycle {cycle}")
        fig.tight_layout()
        path = output / f"cycle_{cycle:03d}.png"
        fig.savefig(path, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        created.append(path)

    fig, axes = plt.subplots(3, 1, figsize=(9, 8), sharex=True)
    for cycle, records in sorted(grouped.items()):
        x = [row["theta_cycle_deg"] for row in records]
        label = f"Cycle {cycle}"
        axes[0].plot(x, [row["CFx"] for row in records], lw=1.1, label=label)
        axes[1].plot(x, [row["CFy"] for row in records], lw=1.1, label=label)
        axes[2].plot(x, [row["CP"] for row in records], lw=1.1, label=label)
    _style_axes(axes, ticker)
    axes[-1].set_xlim(0, 360)
    for axis in axes:
        axis.legend(ncol=2, fontsize=8)
    fig.suptitle(f"{case.name} | {force_object} | Cycle comparison")
    fig.tight_layout()
    overlay = output / "cycles_overlay.png"
    fig.savefig(overlay, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    created.append(overlay)

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    x = [row["theta_global_deg"] for row in results]
    axes[0].plot(x, [row["CFx"] for row in results], lw=1.0)
    axes[1].plot(x, [row["CFy"] for row in results], lw=1.0)
    axes[2].plot(x, [row["CP"] for row in results], lw=1.0)
    _style_axes(axes, ticker)
    fig.suptitle(f"{case.name} | {force_object} | Full force history")
    fig.tight_layout()
    history = output / "full_history.png"
    fig.savefig(history, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    created.append(history)
    return created


def process_case(
    config: CampaignConfig,
    case: CaseDefinition,
    *,
    make_plots: bool = True,
) -> dict[str, Any]:
    case_dir = config.cases_dir / case.name
    manifest_path = case_dir / "case_manifest.json"
    if not manifest_path.is_file():
        raise ForceDataError(
            f"generated-case manifest is missing: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ForceDataError(
            f"generated-case manifest is invalid: {manifest_path}"
        ) from exc
    if (
        manifest.get("name") != case.name
        or manifest.get("parameters") != case.parameters
        or manifest.get("values") != case.values
    ):
        raise ForceDataError(
            f"{case.name}: current JSON does not match the generated case "
            "manifest; regenerate the case or restore the JSON used to run it"
        )
    report_dir = config.reports_dir / "cases" / case.name
    report_dir.mkdir(parents=True, exist_ok=True)
    objects = find_force_objects(case_dir, config.postprocessing)
    outcome: dict[str, Any] = {
        "case": case.name,
        "objects": [],
        "warnings": [],
        "cycle_rows": [],
        "case_rows": [],
    }
    if not objects:
        outcome["warnings"].append(
            f"{case.name}: no force objects were configured or discovered"
        )
    for force_object in objects:
        files = find_force_files(case_dir, force_object)
        if not files:
            outcome["warnings"].append(
                f"{case.name}: no forces.dat found for {force_object}"
            )
            continue
        raw = read_force_history(
            files,
            float(config.postprocessing["duplicate_time_tolerance"]),
        )
        results = compute_force_results(raw, case, config.postprocessing)
        summaries = cycle_summaries(
            case, force_object, results, config.postprocessing
        )
        timeseries_path = report_dir / f"{force_object}_timeseries.csv"
        cycle_path = report_dir / f"{force_object}_cycle_summary.csv"
        write_csv(timeseries_path, results)
        write_csv(cycle_path, summaries)
        plots = (
            plot_force_results(config, case, force_object, results)
            if make_plots
            else []
        )
        complete_cycles = [
            row for row in summaries if row["complete_cycle"]
        ]
        if not summaries:
            raise ForceDataError(
                f"{case.name}: no cycle summaries remain for {force_object}; "
                "allow partial cycles or lower the completeness threshold"
            )
        last_row = (complete_cycles or summaries)[-1]
        derived_values = {
            key: case.values[key]
            for key in (
                "nu",
                "omega",
                "period",
                "end_time",
                "U_inf",
                "chord",
                "radius",
                "span",
                "cycles",
                "cores",
            )
            if key in case.values and key not in case.parameters
        }
        case_row = {
            "case": case.name,
            **case.parameters,
            **derived_values,
            "force_object": force_object,
            "source_file_count": len(files),
            "sample_count": len(results),
            "available_cycles": len(summaries),
            "complete_cycles": len(complete_cycles),
            "last_reported_cycle": last_row["cycle"],
            "mean_CP_last_cycle": last_row["mean_CP"],
            "min_CP_last_cycle": last_row["min_CP"],
            "max_CP_last_cycle": last_row["max_CP"],
            "mean_CFx_last_cycle": last_row["mean_CFx"],
            "mean_CFy_last_cycle": last_row["mean_CFy"],
            "timeseries_csv": str(timeseries_path),
            "cycle_summary_csv": str(cycle_path),
            "figure_directory": str(
                config.figures_dir / case.name / force_object
            ),
        }
        outcome["objects"].append(
            {
                "name": force_object,
                "source_files": [str(path) for path in files],
                "timeseries_csv": str(timeseries_path),
                "cycle_summary_csv": str(cycle_path),
                "plots": [str(path) for path in plots],
            }
        )
        outcome["cycle_rows"].extend(summaries)
        outcome["case_rows"].append(case_row)

    atomic_write_json(
        report_dir / "postprocessing_manifest.json",
        {
            "campaign": config.name,
            "case": case.name,
            "generated_at": utc_now(),
            "objects": outcome["objects"],
            "warnings": outcome["warnings"],
        },
    )
    return outcome


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _row_case(row: dict[str, Any]) -> str:
    return str(row.get("case") or row.get("campaign_case") or "")


def process_campaign(
    config: CampaignConfig,
    cases: list[CaseDefinition],
    *,
    selected_case: str | None = None,
    make_plots: bool = True,
    strict: bool = False,
) -> dict[str, Any]:
    selected = (
        [case for case in cases if case.name == selected_case]
        if selected_case
        else cases
    )
    if selected_case and not selected:
        raise ConfigurationError(f"unknown case: {selected_case}")
    cycle_rows: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    processed = 0
    for case in selected:
        try:
            outcome = process_case(
                config, case, make_plots=make_plots
            )
        except (ForceDataError, OSError, ValueError) as exc:
            if strict:
                raise
            warnings.append(f"{case.name}: {exc}")
            continue
        warnings.extend(outcome["warnings"])
        cycle_rows.extend(outcome["cycle_rows"])
        case_rows.extend(outcome["case_rows"])
        if outcome["objects"]:
            processed += 1

    processed_cycle_count = len(cycle_rows)
    processed_case_row_count = len(case_rows)
    if selected_case:
        old_cycle_rows = _read_csv_rows(
            config.reports_dir / "force_cycle_summary.csv"
        )
        old_case_rows = _read_csv_rows(
            config.reports_dir / "force_case_summary.csv"
        )
        cycle_rows = [
            row for row in old_cycle_rows if _row_case(row) != selected_case
        ] + cycle_rows
        case_rows = [
            row for row in old_case_rows if _row_case(row) != selected_case
        ] + case_rows
    cycle_rows.sort(
        key=lambda row: (
            _row_case(row),
            str(row.get("force_object", "")),
            int(float(row.get("cycle", 0))),
        )
    )
    case_rows.sort(
        key=lambda row: (
            _row_case(row),
            str(row.get("force_object", "")),
        )
    )
    write_csv(config.reports_dir / "force_cycle_summary.csv", cycle_rows)
    write_csv(config.reports_dir / "force_case_summary.csv", case_rows)
    atomic_write_json(
        config.reports_dir / "force_postprocessing.json",
        {
            "campaign": config.name,
            "generated_at": utc_now(),
            "processed_cases": processed,
            "cycle_rows": cycle_rows,
            "case_rows": case_rows,
            "warnings": warnings,
        },
    )
    return {
        "processed_cases": processed,
        "case_rows": processed_case_row_count,
        "cycle_rows": processed_cycle_count,
        "warnings": warnings,
    }
