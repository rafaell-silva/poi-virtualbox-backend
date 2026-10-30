import csv
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "validate_results", _ROOT / "tools" / "validate_results.py"
)
validate_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_results)


def write_metric_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)


def write_sys_csv(path, run_id=None, samples=1):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if run_id is None:
            writer.writerow(["timestamp_utc", "elapsed_s"])
            for i in range(samples):
                writer.writerow([f"2026-10-30T12:00:0{i}Z", i])
        else:
            writer.writerow(["run_id", "timestamp_utc", "elapsed_s"])
            for i in range(samples):
                writer.writerow([run_id, f"2026-10-30T12:00:0{i}Z", i])


def test_join_por_run_id_valido(tmp_path):
    write_metric_csv(
        tmp_path / "exp_nivel_2vus.csv",
        [("run_id", "cpu_C1_w1_uniform_2vus_r1_X"), ("vus", "2")],
    )
    write_sys_csv(tmp_path / "sys_local_cpu_C1_w1_uniform_2vus_r1_X_20261030T120000Z.csv",
                  run_id="cpu_C1_w1_uniform_2vus_r1_X")

    errors, _ = validate_results.validate_dir(str(tmp_path))
    assert errors == []


def test_run_id_duplicado_e_erro(tmp_path):
    write_metric_csv(
        tmp_path / "exp_a.csv",
        [("run_id", "cpu_C1_w1_uniform_1vus_r1_X"), ("vus", "1")],
    )
    write_metric_csv(
        tmp_path / "exp_b.csv",
        [("run_id", "cpu_C1_w1_uniform_1vus_r1_X"), ("vus", "2")],
    )
    write_sys_csv(tmp_path / "sys_local_1.csv", run_id="cpu_C1_w1_uniform_1vus_r1_X")

    errors, _ = validate_results.validate_dir(str(tmp_path))
    assert any("duplicado" in e for e in errors)


def test_experimento_sem_sys_e_erro(tmp_path):
    write_metric_csv(
        tmp_path / "exp_a.csv",
        [("run_id", "cpu_C1_w1_uniform_1vus_r1_X"), ("vus", "1")],
    )

    errors, _ = validate_results.validate_dir(str(tmp_path))
    assert any("sem CSV de sistema" in e for e in errors)


def test_csv_legacy_nao_quebra_validacao(tmp_path):
    write_metric_csv(tmp_path / "exp_antigo.csv", [("vus", "1")])
    write_sys_csv(tmp_path / "sys_local_antigo_20261030T120000Z.csv")

    errors, warnings = validate_results.validate_dir(str(tmp_path))
    assert errors == []
    assert any("legacy" in w for w in warnings)


def test_monitor_continuo_all_nao_exige_experimento(tmp_path):
    write_metric_csv(
        tmp_path / "exp_a.csv",
        [("run_id", "cpu_C1_w1_uniform_1vus_r1_X"), ("vus", "1")],
    )
    write_sys_csv(tmp_path / "sys_local_nivel.csv", run_id="cpu_C1_w1_uniform_1vus_r1_X")
    write_sys_csv(tmp_path / "sys_local_sweep.csv", run_id="cpu_C1_w1_uniform_sweep_r1_T_all")

    errors, _ = validate_results.validate_dir(str(tmp_path))
    assert errors == []
