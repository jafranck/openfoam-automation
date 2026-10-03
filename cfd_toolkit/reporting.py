"""Campaign-level result tables and a compact Markdown summary."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .config import CampaignConfig, CaseDefinition
from .io_utils import atomic_write_json, atomic_write_text, utc_now, write_csv
from .monitor import refresh_state, status_rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def create_summary(
    config: CampaignConfig,
    cases: list[CaseDefinition],
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    state = refresh_state(config, cases) if refresh else None
    if state is None:
        from .state import load_state

        state = load_state(config, cases)
    statuses = status_rows(cases, state)
    force_rows = _read_csv(config.reports_dir / "force_case_summary.csv")
    force_by_case: dict[str, list[dict[str, str]]] = {}
    for row in force_rows:
        force_by_case.setdefault(row.get("case", ""), []).append(row)

    results: list[dict[str, Any]] = []
    for status in statuses:
        matched = force_by_case.get(str(status["case"]), [])
        if matched:
            for force in matched:
                results.append({**status, **force})
        else:
            results.append(status)
    write_csv(config.reports_dir / "campaign_results.csv", results)

    counts: dict[str, int] = {}
    for row in statuses:
        key = str(row.get("status", "unknown"))
        counts[key] = counts.get(key, 0) + 1
    generated_at = utc_now()
    payload = {
        "campaign": config.name,
        "generated_at": generated_at,
        "case_count": len(cases),
        "status_counts": counts,
        "reports_directory": str(config.reports_dir),
        "figures_directory": str(config.figures_dir),
        "cases": results,
    }
    atomic_write_json(config.reports_dir / "campaign_summary.json", payload)

    lines = [
        f"# Campaign summary: {config.name}",
        "",
        f"Generated: {generated_at}",
        "",
        "## Totals",
        "",
    ]
    lines.extend(f"- {status}: {count}" for status, count in sorted(counts.items()))
    lines += [
        "",
        "## Cases",
        "",
        "| Case | Status | Progress | Job ID | Last-cycle mean CP |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in results:
        progress = row.get("progress_percent")
        progress_text = f"{progress}%" if progress not in (None, "") else "-"
        mean_cp = row.get("mean_CP_last_cycle", "-") or "-"
        lines.append(
            f"| {row.get('case')} | {row.get('status')} | {progress_text} | "
            f"{row.get('job_id') or '-'} | {mean_cp} |"
        )
    lines += [
        "",
        "## Output locations",
        "",
        f"- CSV and JSON reports: `{config.reports_dir}`",
        f"- Force figures: `{config.figures_dir}`",
    ]
    atomic_write_text(
        config.reports_dir / "SUMMARY.md", "\n".join(lines) + "\n"
    )
    return payload
