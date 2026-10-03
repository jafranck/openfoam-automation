# Changelog

## 2.2.0

This is the consolidated release that replaces 2.1.0 and 2.1.1.

- kept the compact `global_parameters` plus explicit `cases` JSON
- preserved an existing validated base case, `par_run.sh`, and `par_rec.sh`
- adapted only generated run-script copies
- normalized legacy `--tasks-per-node` to CHTC's documented
  `--ntasks-per-node`
- updated numeric `srun`, `mpirun`, and `mpiexec` task counts
- preserved each base-case script's memory and partition instead of silently
  applying hidden defaults
- inferred and recreated clean runtime directories from script redirections
- added content fingerprints for the copied base case and selected mesh
- blocked submission of stale or mismatched generated cases
- prevented a live `forces.dat` file from marking a running job complete
- made fatal solver-log patterns take priority over an active scheduler state
- preserved other aggregate rows when reprocessing only one case
- removed stale generated plots before replotting
- added stricter checks for JSON keys, numeric inputs, walltime, run scripts,
  paths, and required `polyMesh` files
- kept active job IDs in finalizer dependencies during resumed campaigns
- added a clear laptop-versus-CHTC submission preflight
- expanded regression coverage from 8 to 16 tests before packaging

## 2.1.1

- preserved existing base-case run scripts while adapting only generated copies
- added support for legacy `#SBATCH --tasks-per-node=N` scripts
- updated numeric `srun -n N` task counts from each case's JSON core value
- recreated clean runtime log directories such as `output/` after stale output
  is excluded from the base-case copy
- improved missing-script errors by listing detected `.sh` files
- added a regression test using an unchanged `par_run.sh` layout

## 2.1.0

- added a compact `global_parameters` + explicit `cases` configuration
- moved the CFT OpenFOAM, Slurm, monitoring, and post-processing rules into
  internal preset defaults
- added per-case overrides for cycles, cores, walltime, and other common values
- retained compatibility with the full advanced schema-v2 configuration
- updated setup instructions to use a Python virtual environment

## 2.0.0

- rebuilt the v1 campaign tool as a modular workflow
- added mesh installation and structured OpenFOAM entry updates
- added Slurm resource patching, job tracking, and dependent finalization
- added cycle-aware `forces.dat` processing and clean reports/figures storage
- added restart-segment merging, force CSV exports, and three plot types
- added local end-to-end demonstration and expanded automated tests
