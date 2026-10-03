"""Command-line interface for OpenFOAM Campaign Toolkit v2."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import (
    ConfigurationError,
    build_cases,
    load_config,
    validate_config,
)
from .forces import ForceDataError, process_campaign
from .monitor import refresh_state
from .reporting import create_summary
from .scheduler import submit_cases
from .setup import generate_campaign


TOOLKIT_ROOT = Path(__file__).resolve().parents[1]
_TERMINAL = {"completed", "failed", "finished_missing_outputs", "cancelled"}


def _load(path: str, *, validate: bool = False):
    config = load_config(path)
    cases = build_cases(config)
    warnings = validate_config(config, cases) if validate else []
    return config, cases, warnings


def _print_paths(config) -> None:
    print(f"Campaign : {config.name}")
    print(f"Cases    : {config.cases_dir}")
    print(f"Reports  : {config.reports_dir}")
    print(f"Figures  : {config.figures_dir}")


def command_validate(args: argparse.Namespace) -> int:
    config, cases, warnings = _load(args.config, validate=True)
    print(f"VALID: {config.name} ({len(cases)} cases)")
    _print_paths(config)
    for warning in warnings:
        print(f"WARNING: {warning}")
    if args.show_cases:
        for case in cases:
            print(f"  {case.name}: {json.dumps(case.values, sort_keys=True)}")
    return 0


def command_generate(args: argparse.Namespace) -> int:
    config, cases, warnings = _load(args.config, validate=True)
    for warning in warnings:
        print(f"WARNING: {warning}")
    counts = generate_campaign(config, cases, force=args.force)
    print(
        f"Generated {counts['generated']} case(s); "
        f"skipped {counts['skipped']} existing case(s)."
    )
    _print_paths(config)
    return 0


def command_submit(args: argparse.Namespace) -> int:
    config, cases, _ = _load(args.config, validate=True)
    result = submit_cases(
        config,
        cases,
        dry_run=args.dry_run,
        include_all=args.all,
        limit=args.limit,
        jobs=args.jobs,
        toolkit_root=TOOLKIT_ROOT,
    )
    for command in result["commands"]:
        print(command)
    if args.dry_run:
        print(f"Dry run: {result['selected']} case(s); nothing submitted.")
    else:
        print(
            f"Submitted {result['submitted']} Slurm case(s); "
            f"completed {result['completed']} local case(s); "
            f"failed {result['failed']} case(s)."
        )
        if result.get("finalizer_job_id"):
            print(
                "Automatic post-processing finalizer job: "
                f"{result['finalizer_job_id']}"
            )
        if result.get("finalizer_error"):
            print(
                "WARNING: simulation jobs were submitted, but automatic "
                f"finalizer submission failed: {result['finalizer_error']}"
            )
            print(
                "After the jobs finish, run scripts/finalize_campaign.py "
                "manually."
            )
    return (
        0
        if result["failed"] == 0 and not result.get("finalizer_error")
        else 1
    )


def _print_status(config, cases, state) -> None:
    print(f"\n{config.name}")
    print(f"{'STATUS':<25} {'CASE':<38} {'PROGRESS':>10} {'JOB':>12}")
    for case in cases:
        item = state["cases"][case.name]
        progress = item.get("progress_percent")
        progress_text = f"{progress:.2f}%" if isinstance(progress, float) else "-"
        print(
            f"{str(item.get('status')):<25} {case.name:<38} "
            f"{progress_text:>10} {str(item.get('job_id') or '-'):>12}"
        )


def command_status(args: argparse.Namespace) -> int:
    config, cases, _ = _load(args.config)
    if args.watch and args.interval < 1:
        raise ConfigurationError("--interval must be at least 1 second")
    try:
        while True:
            state = refresh_state(config, cases)
            _print_status(config, cases, state)
            if not args.watch:
                break
            if all(
                state["cases"][case.name].get("status") in _TERMINAL
                for case in cases
            ):
                print("All cases are terminal.")
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped watching. Submitted jobs continue running.")
    return 0


def command_postprocess(args: argparse.Namespace) -> int:
    config, cases, _ = _load(args.config)
    if not config.postprocessing["enabled"]:
        print("Post-processing is disabled in the JSON configuration.")
        return 0
    result = process_campaign(
        config,
        cases,
        selected_case=args.case,
        make_plots=not args.no_plots,
        strict=args.strict,
    )
    print(
        f"Processed {result['processed_cases']} case(s), "
        f"{result['cycle_rows']} cycle summary row(s)."
    )
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")
    print(f"Reports: {config.reports_dir}")
    print(f"Figures: {config.figures_dir}")
    return 0


def command_summarize(args: argparse.Namespace) -> int:
    config, cases, _ = _load(args.config)
    create_summary(config, cases, refresh=True)
    print(f"Campaign summary: {config.reports_dir / 'SUMMARY.md'}")
    return 0


def _finalize(config, cases, *, strict: bool = False) -> int:
    refresh_state(config, cases)
    result = {"warnings": []}
    if config.postprocessing["enabled"]:
        result = process_campaign(
            config, cases, make_plots=True, strict=strict
        )
    create_summary(config, cases, refresh=True)
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")
    print(f"Reports: {config.reports_dir}")
    print(f"Figures: {config.figures_dir}")
    return 0


def command_finalize(args: argparse.Namespace) -> int:
    config, cases, _ = _load(args.config)
    return _finalize(config, cases, strict=args.strict)


def command_workflow(args: argparse.Namespace) -> int:
    config, cases, warnings = _load(args.config, validate=True)
    if args.interval is not None and args.interval < 1:
        raise ConfigurationError("--interval must be at least 1 second")
    print(f"VALID: {config.name} ({len(cases)} cases)")
    for warning in warnings:
        print(f"WARNING: {warning}")
    if args.dry_run:
        result = submit_cases(
            config,
            cases,
            dry_run=True,
            include_all=args.all,
            limit=args.limit,
            jobs=args.jobs,
            toolkit_root=TOOLKIT_ROOT,
        )
        for command in result["commands"]:
            print(command)
        print("Dry run complete; no cases generated or jobs submitted.")
        return 0

    counts = generate_campaign(config, cases, force=args.force)
    print(
        f"Generated {counts['generated']} case(s); "
        f"skipped {counts['skipped']} existing case(s)."
    )
    if args.wait and config.scheduler["type"] == "slurm":
        config.data["workflow"]["automatic_finalization"] = False
    result = submit_cases(
        config,
        cases,
        dry_run=False,
        include_all=args.all,
        limit=args.limit,
        jobs=args.jobs,
        toolkit_root=TOOLKIT_ROOT,
    )
    print(
        f"Submitted {result['submitted']} Slurm case(s); "
        f"completed {result['completed']} local case(s); "
        f"failed {result['failed']} case(s)."
    )
    if config.scheduler["type"] == "local":
        return _finalize(config, cases, strict=False)
    if args.wait:
        interval = args.interval or int(
            config.data["workflow"]["poll_interval_seconds"]
        )
        while True:
            state = refresh_state(config, cases)
            _print_status(config, cases, state)
            selected_names = set(result["selected_case_names"])
            if all(
                state["cases"][name].get("status") in _TERMINAL
                for name in selected_names
            ):
                break
            time.sleep(interval)
        return _finalize(config, cases, strict=False)
    if result.get("finalizer_job_id"):
        print(
            "Post-processing will run automatically in dependent Slurm job "
            f"{result['finalizer_job_id']}."
        )
    elif result.get("finalizer_error"):
        print(
            "WARNING: simulation jobs were submitted, but automatic "
            f"finalizer submission failed: {result['finalizer_error']}"
        )
        print(
            "After the jobs finish, run scripts/finalize_campaign.py "
            "manually."
        )
    else:
        print(
            "After jobs finish, run scripts/finalize_campaign.py to create "
            "reports and figures."
        )
    return (
        0
        if result["failed"] == 0 and not result.get("finalizer_error")
        else 1
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="JSON-driven OpenFOAM campaign generation, Slurm execution, "
        "monitoring, and cycle-aware force post-processing."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("config")
    validate.add_argument("--show-cases", action="store_true")
    validate.set_defaults(function=command_validate)

    generate = subparsers.add_parser("generate")
    generate.add_argument("config")
    generate.add_argument("--force", action="store_true")
    generate.set_defaults(function=command_generate)

    submit = subparsers.add_parser("submit")
    submit.add_argument("config")
    submit.add_argument("--dry-run", action="store_true")
    submit.add_argument("--all", action="store_true")
    submit.add_argument("--limit", type=int)
    submit.add_argument("--jobs", type=int, default=1)
    submit.set_defaults(function=command_submit)

    status = subparsers.add_parser("status")
    status.add_argument("config")
    status.add_argument("--watch", action="store_true")
    status.add_argument("--interval", type=int, default=30)
    status.set_defaults(function=command_status)

    postprocess = subparsers.add_parser("postprocess")
    postprocess.add_argument("config")
    postprocess.add_argument("--case")
    postprocess.add_argument("--no-plots", action="store_true")
    postprocess.add_argument("--strict", action="store_true")
    postprocess.set_defaults(function=command_postprocess)

    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("config")
    summarize.set_defaults(function=command_summarize)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("config")
    finalize.add_argument("--strict", action="store_true")
    finalize.set_defaults(function=command_finalize)

    workflow = subparsers.add_parser("workflow")
    workflow.add_argument("config")
    workflow.add_argument("--dry-run", action="store_true")
    workflow.add_argument("--force", action="store_true")
    workflow.add_argument("--all", action="store_true")
    workflow.add_argument("--limit", type=int)
    workflow.add_argument("--jobs", type=int, default=1)
    workflow.add_argument("--wait", action="store_true")
    workflow.add_argument("--interval", type=int)
    workflow.set_defaults(function=command_workflow)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.function(args))
    except (
        ConfigurationError,
        ForceDataError,
        OSError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
