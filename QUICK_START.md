# OpenFOAM Automation Toolkit 2.2 — Quick Start

Use Python 3.10 or newer.

## A. Test once on your Ubuntu computer

### 1. Open the toolkit

```bash
cd ~/jfrancklab/OpenFOAM_Campaign_Toolkit_v2.2.0/OpenFOAM_Campaign_Toolkit_v2
```

### 2. Create the Python environment

Ubuntu 24.04 may first require:

```bash
sudo apt update
sudo apt install python3.12-venv
```

Then:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3. Test the untouched toolkit

```bash
python -m unittest discover -s tests -v
python scripts/run_campaign.py examples/demo_campaign.json
```

Expected test ending:

```text
Ran 16 tests
OK
```

The demo is local and does not need OpenFOAM, Slurm, or CHTC.

### 4. Add the real inputs

Copy the complete verified case into:

```text
base_cases/cft_working_case/
```

Copy the selected mesh contents into:

```text
meshes/current/polyMesh/
```

Do not edit the original base-case scripts; the toolkit modifies only generated copies.

### 5. Edit only the JSON

Edit:

```text
configs/cft_slurm_template.json
```

The shared values go in `global_parameters`. List only the exact Re–TSR pairs
you want under `cases`:

```json
"global_parameters": {
  "U_inf": 1.0,
  "chord": 1.0,
  "radius": 2.06509248,
  "span_ratio": 0.2,
  "rho": 1.0,
  "cycles": 5,
  "cores": 8,
  "walltime": "4-00:00:00",
  "run_script": "par_run.sh"
},
"cases": [
  {"Re": 100000, "TSR": 1.5},
  {"Re": 100000, "TSR": 2.0},
  {"Re": 150000, "TSR": 1.2}
]
```

This creates exactly three cases. A single row may override `cycles`, `cores`,
or `walltime`.

### 6. Validate and generate locally

```bash
python scripts/validate_campaign.py \
  configs/cft_slurm_template.json --show-cases

python scripts/generate_cases.py \
  configs/cft_slurm_template.json

python scripts/submit_cases.py \
  configs/cft_slurm_template.json --dry-run
```

These commands do not run OpenFOAM or submit a job. Inspect one generated case,
especially `nu`, `omega`, `endTime`, `numberOfSubdomains`, the mesh, and the
generated copy of `par_run.sh`.

Do not run the real submit command on the laptop.

## B. Run on the CHTC Slurm cluster

CHTC recommends running job data from `/scratch`, not from the small home
allocation. Put the toolkit under `/scratch/$USER/` or set `campaigns_root` and
`figures_root` to `/scratch/$USER/...` in the JSON.

Create and test a separate Python 3.10+ environment on CHTC. Do not copy the
Ubuntu `.venv` to CHTC.

Validate and regenerate the cases on CHTC, then submit one pilot:

```bash
python scripts/validate_campaign.py \
  configs/cft_slurm_template.json --show-cases

python scripts/generate_cases.py \
  configs/cft_slurm_template.json

python scripts/submit_cases.py \
  configs/cft_slurm_template.json --dry-run

python scripts/submit_cases.py \
  configs/cft_slurm_template.json --limit 1
```

After the pilot is verified, submit the remaining cases:

```bash
python scripts/submit_cases.py \
  configs/cft_slurm_template.json
```

Monitor:

```bash
python scripts/monitor_campaign.py \
  configs/cft_slurm_template.json --watch
```

A dependent Slurm finalizer creates the force CSVs, plots, and campaign summary
after the submitted simulations finish. If it cannot be submitted, the toolkit
reports that clearly and tells you to run:

```bash
python scripts/finalize_campaign.py \
  configs/cft_slurm_template.json
```

`par_rec.sh` is copied with each case but is never run automatically. Use it
separately only when reconstructed OpenFOAM fields are needed for visualization;
the force CSVs and plots do not require reconstruction.
