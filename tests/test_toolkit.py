from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cfd_toolkit.config import (
    ConfigurationError,
    build_cases,
    load_config,
    safe_eval,
    validate_config,
)
from cfd_toolkit.forces import (
    ForceDataError,
    process_campaign,
    read_force_history,
)
from cfd_toolkit.monitor import inspect_case
from cfd_toolkit.reporting import create_summary
from cfd_toolkit.scheduler import submit_cases
from cfd_toolkit.setup import generate_campaign, verify_generated_cases


ROOT = Path(__file__).resolve().parents[1]


def force_line(time_value: float, fpx: float, *, columns: int = 19) -> str:
    if columns == 19:
        values = [
            time_value,
            fpx,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.2,
            0.0,
            0.0,
            0.0,
        ]
    else:
        values = [
            time_value,
            fpx,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.2,
        ]
    return " ".join(str(value) for value in values)


def make_compact_fixture(
    temporary: Path,
    *,
    cases: list[dict] | None = None,
    cores: int | float = 8,
    script_text: str | None = None,
) -> tuple[Path, Path]:
    base = temporary / "base"
    mesh = temporary / "mesh" / "polyMesh"
    shutil.copytree(ROOT / "examples" / "demo_base_case", base)
    shutil.copytree(ROOT / "examples" / "demo_mesh" / "polyMesh", mesh)
    script = base / "par_run.sh"
    script.write_text(
        script_text
        or (
            "#!/bin/bash\n"
            "#SBATCH --time=4-00:00:00\n"
            "#SBATCH --nodes=1\n"
            "#SBATCH --tasks-per-node=8\n"
            "#SBATCH --mem=32G\n"
            "#SBATCH --partition=shared\n"
            "decomposePar > output/parout\n"
            "srun -n 8 pimpleFoam -parallel > output/foamout\n"
        ),
        encoding="utf-8",
    )
    path = temporary / "compact.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "preset": "cft_slurm",
                "name": "compact_fixture",
                "paths": {
                    "base_case": str(base),
                    "mesh": str(mesh),
                    "campaigns_root": str(temporary / "campaigns"),
                    "figures_root": str(temporary / "figures"),
                },
                "global_parameters": {
                    "U_inf": 1.0,
                    "chord": 1.0,
                    "radius": 2.06509248,
                    "span_ratio": 0.2,
                    "rho": 1.0,
                    "cycles": 2,
                    "cores": cores,
                    "walltime": "2-00:00:00",
                    "run_script": "par_run.sh",
                },
                "cases": cases or [{"Re": 100000, "TSR": 1.5}],
            }
        ),
        encoding="utf-8",
    )
    return path, base


class ExpressionTests(unittest.TestCase):
    def test_safe_arithmetic(self):
        self.assertAlmostEqual(
            safe_eval("2 * pi / omega", {"pi": 3.141592653589793, "omega": 2}),
            3.141592653589793,
        )

    def test_blocks_python_execution(self):
        with self.assertRaises(ConfigurationError):
            safe_eval("__import__('os').system('id')", {})


class CompactConfigurationTests(unittest.TestCase):
    def test_global_parameters_and_explicit_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            base = temporary / "base"
            mesh = temporary / "mesh" / "polyMesh"
            shutil.copytree(ROOT / "examples" / "demo_base_case", base)
            shutil.copytree(ROOT / "examples" / "demo_mesh" / "polyMesh", mesh)
            script = base / "par_run.sh"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "#SBATCH -n 1\n"
                "#SBATCH --time 00:10:00\n"
                "./Allrun\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            path = temporary / "compact.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "name": "compact_test",
                        "paths": {
                            "base_case": str(base),
                            "mesh": str(mesh),
                            "campaigns_root": str(temporary / "campaigns"),
                            "figures_root": str(temporary / "figures"),
                        },
                        "global_parameters": {
                            "U_inf": 1.0,
                            "chord": 1.0,
                            "radius": 2.0,
                            "span_ratio": 0.2,
                            "cycles": 5,
                            "cores": 8,
                            "walltime": "2-00:00:00",
                            "run_script": "par_run.sh",
                        },
                        "cases": [
                            {"Re": 100000, "TSR": 1.5},
                            {"Re": 100000, "TSR": 2.0},
                            {
                                "Re": 150000,
                                "TSR": 1.2,
                                "cycles": 7,
                                "cores": 16,
                                "walltime": "3-00:00:00",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(path)
            cases = build_cases(config)

            self.assertEqual(config.scheduler["script"], "par_run.sh")
            self.assertEqual(
                [case.name for case in cases],
                [
                    "Re_100000_TSR_1.5",
                    "Re_100000_TSR_2",
                    "Re_150000_TSR_1.2",
                ],
            )
            self.assertEqual(cases[0].values["cycles"], 5)
            self.assertEqual(cases[2].values["cycles"], 7)
            self.assertEqual(cases[2].values["cores"], 16)
            self.assertEqual(cases[2].values["walltime"], "3-00:00:00")
            self.assertAlmostEqual(cases[0].values["nu"], 1e-5)
            self.assertIn(
                "postProcessing/forces_foil1/*/forces.dat",
                config.scheduler["completion_files"],
            )
            validate_config(config, cases)
            result = generate_campaign(config, cases)
            self.assertEqual(result["generated"], 3)
            generated = config.cases_dir / cases[2].name
            self.assertIn(
                "numberOfSubdomains  16;",
                (generated / "system/decomposeParDict").read_text(),
            )
            self.assertIn(
                "#SBATCH --time=3-00:00:00",
                (generated / "par_run.sh").read_text(),
            )

    def test_verified_slurm_script_is_adapted_only_in_generated_case(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            base = temporary / "base"
            mesh = temporary / "mesh" / "polyMesh"
            shutil.copytree(ROOT / "examples" / "demo_base_case", base)
            shutil.copytree(ROOT / "examples" / "demo_mesh" / "polyMesh", mesh)
            output = base / "output"
            output.mkdir()
            (output / "old_foamout").write_text("stale\n", encoding="utf-8")
            script = base / "par_run.sh"
            original_script = (
                "#!/bin/bash\n"
                "\n"
                "# walltime:\n"
                "#SBATCH --time=4-00:00:00\n"
                "\n"
                "# Use single core:\n"
                "#SBATCH --nodes=1\n"
                "#SBATCH --tasks-per-node=8\n"
                "#SBATCH --mem=64G\n"
                "#SBATCH --partition=shared\n"
                "\n"
                "# Specify a job name:\n"
                "#SBATCH -J demo_case\n"
                "\n"
                "# Specify an output file\n"
                "#SBATCH -o slurm.out\n"
                "#SBATCH -e slurm.out\n"
                "\n"
                "# Run a command\n"
                "module load openmpi\n"
                "source /opt/openfoam/"
                "OpenFOAM-7/etc/bashrc\n"
                "decomposePar > output/parout\n"
                "srun -n 8 pimpleFoam -parallel > output/foamout\n"
            )
            script.write_text(original_script, encoding="utf-8")
            script.chmod(0o755)
            reconstruction_script = base / "par_rec.sh"
            original_reconstruction = (
                "#!/bin/bash\n"
                "#SBATCH --time=04:00:00\n"
                "reconstructPar -time \"5.2,10.4\"\n"
                "rm -r processor*\n"
            )
            reconstruction_script.write_text(
                original_reconstruction, encoding="utf-8"
            )
            path = temporary / "slurm_script.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "preset": "cft_slurm",
                        "name": "slurm_script_test",
                        "paths": {
                            "base_case": str(base),
                            "mesh": str(mesh),
                            "campaigns_root": str(temporary / "campaigns"),
                            "figures_root": str(temporary / "figures"),
                        },
                        "global_parameters": {
                            "U_inf": 1.0,
                            "chord": 1.0,
                            "radius": 2.0,
                            "span_ratio": 0.2,
                            "cycles": 5,
                            "cores": 16,
                            "walltime": "3-00:00:00",
                            "run_script": "par_run.sh",
                        },
                        "cases": [{"Re": 100000, "TSR": 1.5}],
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(path)
            cases = build_cases(config)
            validate_config(config, cases)
            generate_campaign(config, cases)

            self.assertEqual(script.read_text(encoding="utf-8"), original_script)
            self.assertEqual(
                reconstruction_script.read_text(encoding="utf-8"),
                original_reconstruction,
            )
            generated = config.cases_dir / cases[0].name
            self.assertEqual(
                (generated / "par_rec.sh").read_text(encoding="utf-8"),
                original_reconstruction,
            )
            generated_script = (generated / "par_run.sh").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "#SBATCH --ntasks-per-node=16", generated_script
            )
            self.assertNotIn("#SBATCH --ntasks=16", generated_script)
            self.assertNotIn("--tasks-per-node", generated_script)
            self.assertIn(
                "#SBATCH --time=3-00:00:00", generated_script
            )
            self.assertIn("#SBATCH --mem=64G", generated_script)
            self.assertIn("#SBATCH --partition=shared", generated_script)
            self.assertIn(
                "srun -n 16 pimpleFoam -parallel > output/foamout",
                generated_script,
            )
            self.assertTrue((generated / "output").is_dir())
            self.assertFalse((generated / "output/old_foamout").exists())
            manifest = json.loads(
                (generated / "case_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["runtime_adaptation"]["directories_created"],
                ["output"],
            )
            self.assertEqual(
                manifest["runtime_adaptation"]["scheduler_patch"][
                    "launcher_task_counts"
                ],
                1,
            )

    def test_verified_script_memory_partition_and_extra_log_directory_are_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            path, _ = make_compact_fixture(
                temporary,
                script_text=(
                    "#!/bin/bash\n"
                    "#SBATCH --time=01:00:00\n"
                    "#SBATCH --nodes=1\n"
                    "#SBATCH --ntasks-per-node=4\n"
                    "#SBATCH --mem=21G\n"
                    "#SBATCH --partition=pre\n"
                    "srun -n4 pimpleFoam -parallel > logs/foamout 2>&1\n"
                ),
            )
            config = load_config(path)
            cases = build_cases(config)
            validate_config(config, cases)
            generate_campaign(config, cases)
            generated = config.cases_dir / cases[0].name
            text = (generated / "par_run.sh").read_text(encoding="utf-8")
            self.assertIn("#SBATCH --ntasks-per-node=8", text)
            self.assertIn("#SBATCH --mem=21G", text)
            self.assertIn("#SBATCH --partition=pre", text)
            self.assertIn("srun -n8 pimpleFoam", text)
            self.assertTrue((generated / "logs").is_dir())


class ConfigurationSafetyTests(unittest.TestCase):
    def test_duplicate_json_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text(
                '{"schema_version": 2, "name": "one", "name": "two"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "duplicate JSON key"):
                load_config(path)

    def test_fractional_core_count_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = make_compact_fixture(Path(directory), cores=8.5)
            config = load_config(path)
            cases = build_cases(config)
            with self.assertRaisesRegex(ConfigurationError, "whole number"):
                validate_config(config, cases)

    def test_source_change_makes_generated_case_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            path, base = make_compact_fixture(temporary)
            config = load_config(path)
            cases = build_cases(config)
            validate_config(config, cases)
            generate_campaign(config, cases)
            verify_generated_cases(config, cases)

            control = base / "system" / "controlDict"
            control.write_text(
                control.read_text(encoding="utf-8") + "\n// changed\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "different settings"):
                generate_campaign(config, cases)
            with self.assertRaisesRegex(ConfigurationError, "stale"):
                verify_generated_cases(config, cases)

            result = generate_campaign(config, cases, force=True)
            self.assertEqual(result["generated"], 1)
            verify_generated_cases(config, cases)


class MonitorTests(unittest.TestCase):
    def test_force_file_does_not_mark_running_slurm_job_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            path, _ = make_compact_fixture(temporary)
            config = load_config(path)
            cases = build_cases(config)
            validate_config(config, cases)
            generate_campaign(config, cases)
            case = cases[0]
            case_dir = config.cases_dir / case.name
            force_path = (
                case_dir
                / "postProcessing"
                / "forces_foil1"
                / "0"
                / "forces.dat"
            )
            force_path.parent.mkdir(parents=True)
            force_path.write_text(force_line(0.1, 1.0) + "\n", encoding="utf-8")
            solver_log = case_dir / "output" / "foamout"
            solver_log.write_text(
                f"Time = {case.values['end_time'] / 2}\n",
                encoding="utf-8",
            )
            previous = {"status": "submitted", "job_id": "123"}
            with mock.patch(
                "cfd_toolkit.monitor._query_slurm", return_value="RUNNING"
            ):
                item = inspect_case(config, case, previous)
            self.assertEqual(item["status"], "running")
            self.assertLess(item["progress_percent"], 100.0)

    def test_fatal_log_takes_priority_over_running_scheduler_state(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            path, _ = make_compact_fixture(temporary)
            config = load_config(path)
            cases = build_cases(config)
            validate_config(config, cases)
            generate_campaign(config, cases)
            case = cases[0]
            solver_log = config.cases_dir / case.name / "output" / "foamout"
            solver_log.write_text("FOAM FATAL ERROR\n", encoding="utf-8")
            with mock.patch(
                "cfd_toolkit.monitor._query_slurm", return_value="RUNNING"
            ):
                item = inspect_case(
                    config, case, {"status": "submitted", "job_id": "123"}
                )
            self.assertEqual(item["status"], "failed")


class SchedulerTests(unittest.TestCase):
    def test_slurm_submission_and_dependent_finalizer_with_fake_sbatch(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            path, _ = make_compact_fixture(temporary)
            config = load_config(path)
            cases = build_cases(config)
            validate_config(config, cases)
            generate_campaign(config, cases)

            binary_dir = temporary / "bin"
            binary_dir.mkdir()
            sbatch = binary_dir / "sbatch"
            sbatch.write_text(
                "#!/bin/sh\nprintf '%s\\n' 98765\n",
                encoding="utf-8",
            )
            sbatch.chmod(0o755)
            environment_path = (
                str(binary_dir) + os.pathsep + os.environ.get("PATH", "")
            )
            with mock.patch.dict(os.environ, {"PATH": environment_path}):
                result = submit_cases(
                    config,
                    cases,
                    jobs=1,
                    toolkit_root=ROOT,
                )

            self.assertEqual(result["submitted"], 1)
            self.assertEqual(result["job_ids"], ["98765"])
            self.assertEqual(result["finalizer_job_id"], "98765")
            self.assertIsNone(result["finalizer_error"])
            case_dir = config.cases_dir / cases[0].name
            submission = json.loads(
                (case_dir / "submission.json").read_text(encoding="utf-8")
            )
            self.assertEqual(submission["job_id"], "98765")
            finalizer = config.state_dir / "finalize_campaign.sh"
            self.assertTrue(finalizer.is_file())
            self.assertIn(
                "scripts/finalize_campaign.py",
                finalizer.read_text(encoding="utf-8"),
            )


class ForceReaderTests(unittest.TestCase):
    def test_restart_segments_keep_later_duplicate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "0" / "forces.dat"
            second = root / "1" / "forces.dat"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text(
                "# first\n"
                + force_line(0.0, 1.0)
                + "\n"
                + force_line(1.0, 2.0)
                + "\n",
                encoding="utf-8",
            )
            second.write_text(
                "# restart\n"
                + force_line(1.0, 20.0, columns=13)
                + "\n"
                + force_line(2.0, 3.0, columns=13)
                + "\n",
                encoding="utf-8",
            )
            records = read_force_history([first, second], 1e-12)
            self.assertEqual([row["time"] for row in records], [0.0, 1.0, 2.0])
            self.assertEqual(records[1]["Fp_x"], 20.0)
            self.assertEqual(records[1]["Fporous_x"], 0.0)

    def test_rejects_ambiguous_columns(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "forces.dat"
            path.write_text("0 1 2\n", encoding="utf-8")
            with self.assertRaises(ForceDataError):
                read_force_history([path], 1e-12)


class EndToEndTests(unittest.TestCase):
    def _make_demo(self, temporary: Path, scheduler_type: str = "local") -> Path:
        base = temporary / "base"
        mesh = temporary / "mesh" / "polyMesh"
        shutil.copytree(ROOT / "examples" / "demo_base_case", base)
        shutil.copytree(ROOT / "examples" / "demo_mesh" / "polyMesh", mesh)
        config = json.loads(
            (ROOT / "examples" / "demo_campaign.json").read_text(encoding="utf-8")
        )
        config["name"] = "test_campaign"
        config["paths"] = {
            "base_case": str(base),
            "mesh": str(mesh),
            "campaigns_root": str(temporary / "campaigns"),
            "figures_root": str(temporary / "figures"),
        }
        config["parameters"] = {"Re": [100000], "TSR": [1.5]}
        config["scheduler"]["type"] = scheduler_type
        if scheduler_type == "slurm":
            script = base / "script.sh"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "#SBATCH -n 1\n"
                "#SBATCH --time 00:10:00\n"
                "./Allrun\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            config["scheduler"].update(
                {
                    "script": "script.sh",
                    "resources": {
                        "cores": "{cores}",
                        "walltime": "1-00:00:00",
                        "memory": "8G",
                        "partition": "shared",
                    },
                }
            )
        path = temporary / "campaign.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_generation_patches_mesh_foam_and_slurm(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            path = self._make_demo(temporary, scheduler_type="slurm")
            config = load_config(path)
            cases = build_cases(config)
            validate_config(config, cases)
            result = generate_campaign(config, cases)
            self.assertEqual(result["generated"], 1)
            case_dir = config.cases_dir / cases[0].name
            self.assertTrue((case_dir / "constant/polyMesh/points").is_file())
            transport = (case_dir / "constant/transportProperties").read_text()
            self.assertIn("[0 2 -1 0 0 0 0] 1e-05;", transport)
            decomposition = (case_dir / "system/decomposeParDict").read_text()
            self.assertIn("numberOfSubdomains  2;", decomposition)
            slurm = (case_dir / "script.sh").read_text()
            self.assertIn("#SBATCH --ntasks=2", slurm)
            self.assertIn("#SBATCH --time=1-00:00:00", slurm)
            self.assertIn("#SBATCH --mem=8G", slurm)
            self.assertIn(f"#SBATCH --job-name={cases[0].name}", slurm)

    def test_local_workflow_creates_cycle_reports_and_figures(self):
        os.environ.setdefault("MPLCONFIGDIR", "/tmp/cfd_toolkit_test_mpl")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            path = self._make_demo(temporary)
            config = load_config(path)
            cases = build_cases(config)
            validate_config(config, cases)
            generate_campaign(config, cases)
            submit = submit_cases(
                config,
                cases,
                jobs=1,
                toolkit_root=ROOT,
            )
            self.assertEqual(submit["completed"], 1)
            processed = process_campaign(config, cases, make_plots=True, strict=True)
            self.assertEqual(processed["processed_cases"], 1)
            self.assertEqual(processed["cycle_rows"], 2)
            create_summary(config, cases)

            cycle_csv = config.reports_dir / "force_cycle_summary.csv"
            with cycle_csv.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["cycle"] for row in rows], ["1", "2"])
            self.assertTrue(all(row["complete_cycle"] == "True" for row in rows))
            figure_dir = (
                config.figures_dir / cases[0].name / "forces_foil1"
            )
            self.assertEqual(len(list(figure_dir.glob("*.png"))), 4)
            self.assertTrue(
                (config.reports_dir / "campaign_results.csv").is_file()
            )

    def test_single_case_reprocessing_preserves_other_aggregate_rows(self):
        os.environ.setdefault("MPLCONFIGDIR", "/tmp/cfd_toolkit_test_mpl")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            path = self._make_demo(temporary)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["parameters"] = {"Re": [100000], "TSR": [1.5, 2.0]}
            path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(path)
            cases = build_cases(config)
            validate_config(config, cases)
            generate_campaign(config, cases)
            submit_cases(config, cases, jobs=1, toolkit_root=ROOT)
            process_campaign(config, cases, make_plots=True, strict=True)

            stale_plot = (
                config.figures_dir
                / cases[0].name
                / "forces_foil1"
                / "cycle_999.png"
            )
            stale_plot.write_bytes(b"stale")
            process_campaign(
                config,
                cases,
                selected_case=cases[0].name,
                make_plots=True,
                strict=True,
            )
            with (
                config.reports_dir / "force_case_summary.csv"
            ).open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["case"] for row in rows}, {
                cases[0].name,
                cases[1].name,
            })
            self.assertFalse(stale_plot.exists())


if __name__ == "__main__":
    unittest.main()
