import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PROJECTS = ["cpu-bound", "memory-bound", "io-bound"]


def _load_plots(project):
    path = _ROOT / "src" / project / "plots" / "generate_plots.py"
    name = "plots_" + project.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=_PROJECTS)
def plots(request):
    return _load_plots(request.param)


def test_join_exato_por_run_id(plots):
    run_4vus = "cpu_C1_w1_uniform_4vus_r1_X"
    exp = {
        "run_id": run_4vus,
        "vus": "4",
        "timestamp_utc": "2026-10-30T12:05:00+00:00",
        "_source_file": f"exp_{run_4vus}.csv",
    }
    # O nome do sys_* diz "1vus" (timestamp de fim da execução anterior), mas o
    # run_id é o do nível de 4 VUs: o join exato deve vencer a heurística.
    sys_data = {
        "sys_local_cpu_C1_w1_uniform_1vus_r1_ANTIGO_20261030T120010Z.csv": [
            {"run_id": run_4vus, "elapsed_s": "1"}
        ],
        "sys_local_cpu_C1_w1_uniform_1vus_r1_Z_20261030T120050Z.csv": [
            {"run_id": "cpu_C1_w1_uniform_1vus_r1_Z", "elapsed_s": "1"}
        ],
    }

    index = plots.build_sys_run_index(sys_data)
    matched = plots._match_sys_file(exp, sys_data, index)

    assert matched == "sys_local_cpu_C1_w1_uniform_1vus_r1_ANTIGO_20261030T120010Z.csv"


def test_classifica_monitor_continuo_pelo_run_id(plots):
    run_level = "cpu_C1_w1_uniform_4vus_r1_X"
    run_monitor = "cpu_C1_w1_uniform_sweep_r1_T_all"
    exp = {
        "run_id": run_level,
        "vus": "4",
        "timestamp_utc": "2026-10-30T12:05:00+00:00",
        "_source_file": f"exp_{run_level}.csv",
    }
    level_path = "sys_local_cpu_C1_w1_uniform_4vus_r1_X_20261030T120010Z.csv"
    monitor_path = "sys_local_cpu_C1_w1_uniform_sweep_r1_T_all_20261030T120000Z.csv"
    sys_data = {
        level_path: [{"run_id": run_level, "elapsed_s": "1"}],
        monitor_path: [{"run_id": run_monitor, "elapsed_s": "1"}],
    }
    sys_series = {path: {} for path in sys_data}

    level, monitor = plots.classify_sys_files([exp], sys_series, sys_data)

    assert level == [level_path]
    assert monitor == [monitor_path]


def test_legacy_ainda_usa_heuristica_de_nome(plots):
    exp = {
        "vus": "4",
        "timestamp_utc": "2026-10-30T12:05:00+00:00",
        "_source_file": "exp_legacy_4vus.csv",
    }
    sys_data = {"sys_local_legacy_4vus_x.csv": [{"elapsed_s": "1"}]}

    index = plots.build_sys_run_index(sys_data)
    assert index == {}
    assert (
        plots._match_sys_file(exp, sys_data, index)
        == "sys_local_legacy_4vus_x.csv"
    )


def test_run_id_duplicado_prefere_arquivo_maior(plots):
    sys_data = {
        "sys_a.csv": [{"run_id": "r"}],
        "sys_b.csv": [{"run_id": "r"}, {"run_id": "r"}, {"run_id": "r"}],
    }
    index = plots.build_sys_run_index(sys_data)
    assert index["r"] == "sys_b.csv"
