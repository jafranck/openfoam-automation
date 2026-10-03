# OpenFOAM Automation Toolkit

**Version 2.2.0**

A JSON-driven workflow for repeatable OpenFOAM parameter campaigns:

`validate → generate → submit → monitor → post-process → summarize`

The toolkit is designed for the Franck Lab cross-flow-turbine workflow, while
keeping the case-generation and scheduler layers reusable for other OpenFOAM
studies.

## What 2.2 provides

- one JSON controls the base case, mesh, Re, TSR, cycles, cores, and walltime
- clean campaign storage with every generated case below `campaigns/<name>/cases`
- all CSV/JSON/Markdown results below `campaigns/<name>/reports`
- all plots below `figures/<name>/<case>/<force-object>`
- structured edits of OpenFOAM scalar entries instead of fragile global text changes
- automatic `constant/polyMesh` replacement
- per-case Slurm directive/launcher updates and job-ID tracking
- compatibility with existing Slurm scripts using `--tasks-per-node` and
  hard-coded `srun -n N`, without editing the base-case script
- normalization of generated copies to the current CHTC/Slurm
  `--ntasks-per-node` spelling
- preservation of the base-case script's memory and partition settings
- automatic recreation of clean runtime log directories such as `output/`
- content fingerprints that detect a changed base case or mesh before submission
- progress, latest residuals, and Courant-number monitoring
- scheduler-aware completion detection that does not treat a live force file as
  proof that a running job is finished
- force-file discovery across multiple OpenFOAM restart/time directories
- duplicate-time removal when force histories overlap after a restart
- support for both 19-column and 13-column OpenFOAM force records
- automatic cycle detection from `omega` and force timestamps
- individual cycle plots, all-cycle overlays, and full-history plots
- per-sample, per-cycle, per-case, and whole-campaign CSV reports
- a dependent Slurm finalizer job that runs after all simulation jobs
- atomic state/report writes and restart-safe case skipping

The toolkit requires Python 3.10 or newer. The normal user workflow and the
compact JSON are in `QUICK_START.md`.

Research-specific base cases and meshes are intentionally not included. Users
must supply their own inputs in `base_cases/` and `meshes/`, or run the
synthetic example under `examples/`.


## Output layout

```text
campaigns/
└── my_campaign/
    ├── .campaign/
    │   ├── campaign_manifest.json
    │   ├── config_snapshot.json
    │   ├── finalize_campaign.sh
    │   └── state.json
    ├── cases/
    │   ├── Re_100000_TSR_1.5/
    │   └── Re_100000_TSR_2/
    └── reports/
        ├── campaign_results.csv
        ├── campaign_status.csv
        ├── campaign_status.json
        ├── campaign_summary.json
        ├── force_case_summary.csv
        ├── force_cycle_summary.csv
        ├── force_postprocessing.json
        ├── SUMMARY.md
        └── cases/
            └── Re_100000_TSR_1.5/
                ├── forces_foil1_timeseries.csv
                ├── forces_foil1_cycle_summary.csv
                └── postprocessing_manifest.json

figures/
└── my_campaign/
    └── Re_100000_TSR_1.5/
        └── forces_foil1/
            ├── cycle_001.png
            ├── cycle_002.png
            ├── cycles_overlay.png
            └── full_history.png
```

OpenFOAM output stays inside each generated case, including
`postProcessing/`, `output/`, `processor*/`, `slurm.out`, and
`run_status.txt`.

## Configuration

Start from `configs/cft_slurm_template.json`.

### Normal compact format

Keep shared values in `global_parameters`, then list only the exact cases to
run:

```json
"global_parameters": {
  "U_inf": 1.0,
  "chord": 1.0,
  "radius": 2.06509248,
  "span_ratio": 0.2,
  "rho": 1.0,
  "cycles": 5,
  "cores": 8,
  "walltime": "2-00:00:00",
  "run_script": "par_run.sh"
},
"cases": [
  {"Re": 100000, "TSR": 1.5},
  {"Re": 100000, "TSR": 2.0},
  {"Re": 150000, "TSR": 1.2},
  {"Re": 150000, "TSR": 1.8}
]
```

This creates exactly four cases—there is no automatic Cartesian product.
`run_script` may be changed to a different base-case filename such as
`par_run.sh`.

A value can be overridden for only one case:

```json
{"Re": 150000, "TSR": 1.8, "cycles": 7, "cores": 16,
 "walltime": "3-00:00:00"}
```

All other cases continue using the global values.

The `cft_slurm` preset supplies the derived equations, OpenFOAM dictionary
edits, Slurm settings, monitoring rules, force-file discovery, and plotting
settings internally. The original full schema-v2 format is still supported for
advanced studies.

### Derived physics

The preset evaluates these expressions with a restricted arithmetic parser:

```json
"derived": {
  "nu": "U_inf * chord / Re",
  "omega": "TSR * U_inf / radius",
  "period": "2 * pi / omega",
  "end_time": "cycles * period"
}
```

### OpenFOAM edits

`foam_entries` preserves dimension sets and replaces only the selected keyword's
value. If a keyword occurs more than once in a file, add a zero-based
`occurrence` field. `literal_replacements` remains available for unusual files
that cannot be edited by keyword.

### Slurm resources

The toolkit patches or inserts `#SBATCH` directives for cores, walltime, memory,
partition, account, and job name when those resources are explicitly configured.
The compact preset changes only the visible `cores`, `walltime`, and per-case job
name; it preserves the base-case script's memory and partition. Both `--ntasks`
and the legacy single-node `--tasks-per-node` form are accepted. The
generated copy uses CHTC's documented `--ntasks-per-node` spelling. Numeric task
counts in `srun`, `mpirun`, or `mpiexec` commands are updated as well. The
base-case script in `base_cases/` is never edited.

If old runtime results such as `output/` are excluded while copying the base
case, the toolkit recreates clean parent directories found in configured logs,
Slurm output/error directives, and static shell redirections. This keeps
redirections such as `> output/foamout` valid without adding `mkdir` commands
to the base-case script.

The toolkit also updates `numberOfSubdomains` through the configured OpenFOAM
entry edit.

The default command is:

```text
sbatch --parsable script.sh
```

The numeric job ID is saved in campaign state and in each case's
`submission.json`.

Before a real submission, the toolkit verifies that every generated case still
matches the current JSON, copied base-case contents, selected mesh contents,
and scheduler resources. If any input changed, it requires an explicit
regeneration instead of submitting a stale case.

### Force coefficients

The default denominators reproduce the reference cross-flow-turbine workflow:

```json
"force_coefficient_denominator": "0.5 * U_inf**2 * chord",
"power_coefficient_denominator": "0.5 * U_inf**3 * span * radius"
```

Thus:

- `CFx = Fx / force_coefficient_denominator`
- `CFy = Fy / force_coefficient_denominator`
- `CP = omega * Mz / power_coefficient_denominator`

Change these expressions in the JSON if a project uses a different
reference area or explicit density convention.

## Commands

```bash
python3 scripts/validate_campaign.py CONFIG --show-cases
python3 scripts/generate_cases.py CONFIG
python3 scripts/submit_cases.py CONFIG --dry-run
python3 scripts/submit_cases.py CONFIG
python3 scripts/monitor_campaign.py CONFIG --watch --interval 30
python3 scripts/postprocess_campaign.py CONFIG
python3 scripts/summarize_campaign.py CONFIG
python3 scripts/finalize_campaign.py CONFIG
```

The one-command workflow is:

```bash
python3 scripts/run_campaign.py CONFIG
```

For a small pilot:

```bash
python3 scripts/run_campaign.py CONFIG --limit 1
```

To keep the terminal attached until all Slurm cases finish and then finalize
locally:

```bash
python3 scripts/run_campaign.py CONFIG --wait
```

Normally, leave out `--wait`; the toolkit submits a dependent finalizer job.

On a laptop, stop at validation, generation, and `--dry-run`. A real Slurm
submission is rejected clearly when `sbatch` is unavailable.

## CHTC deployment notes

- CHTC's HPC system uses Slurm and its current job-script examples use
  `--ntasks-per-node`.
- CHTC recommends running job data from `/scratch`, while keeping reusable
  software or templates in `/home` when appropriate.
- Create a separate Python 3.10+ environment on CHTC; do not copy an Ubuntu
  virtual environment between machines.
- The dependent finalizer uses the same Python executable that submitted the
  campaign, so that environment must be visible on the shared filesystem.
- The toolkit never runs `par_rec.sh` automatically. Force processing reads
  `postProcessing/.../forces.dat` directly. Run reconstruction separately only
  when full fields are needed for visualization.

Current CHTC references:

- https://chtc.cs.wisc.edu/uw-research-computing/hpc-job-submission
- https://chtc.cs.wisc.edu/uw-research-computing/hpc-overview

## Restart and safety behavior

- generation skips existing cases
- submission skips submitted, queued, running, and completed cases
- failed cases can be resubmitted with the normal submit command
- `--all` deliberately resubmits every selected case
- `generate --force` removes and rebuilds only the named generated case folders
- base cases and source meshes are never edited
- generated cases are fingerprinted against copied source content
- real submission refuses stale cases
- JSON and state reports are written atomically
- force histories from restarts are ordered and duplicate timestamps keep the
  later record
- a force file created during a run does not mark an active Slurm job complete
- reprocessing one case preserves aggregate results from the other cases

## Test the toolkit

```bash
python3 -m unittest discover -s tests -v
```

The release test suite contains 16 unit, regression, scheduler, monitoring, and
end-to-end checks.

Run the included local demonstration:

```bash
python3 scripts/run_campaign.py examples/demo_campaign.json
```

The demo creates synthetic two-cycle force histories and exercises the same
generation, post-processing, report, and plotting paths without OpenFOAM or
Slurm.

## Development

The initial toolkit and automation workflow were developed by
[Atharva Gado](https://github.com/Atharva-Gado) for cross-flow-turbine research
in the **Computational Flow Physics and Modeling Lab** at the **University of Wisconsin–Madison**.


