import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PROJECTS = ["cpu-bound", "memory-bound", "io-bound"]


def _load_plots(project):
    path = _ROOT / "src" / project / "plots" / "generate_plots.py"
    name = "plots_filters_" + project.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=_PROJECTS)
def plots(request):
    return _load_plots(request.param)


def _exp(run_id, scenario="C1", workers="1", mix="uniform", rep="1", vus="1"):
    return {
        "run_id": run_id,
        "scenario": scenario,
        "workers": workers,
        "request_mix": mix,
        "rep": rep,
        "vus": vus,
        "_source_file": f"exp_{run_id}.csv",
    }


def _sys(run_id, scenario="C1", workers="1", mix="uniform", rep="1"):
    return {
        "run_id": run_id,
        "scenario": scenario,
        "workers": workers,
        "request_mix": mix,
        "rep": rep,
        "elapsed_s": "1",
    }


@pytest.fixture
def experiments():
    return [
        _exp("cpu_C1_w1_uniform_1vus_r1_A", workers="1", mix="uniform", rep="1"),
        _exp("cpu_C1_w8_mixed_1vus_r2_B", workers="8", mix="mixed", rep="2"),
        {"vus": "1", "_source_file": "exp_legacy.csv"},
    ]


def test_sem_filtro_mantem_legacy(plots, experiments):
    assert len(plots.filter_experiments(experiments)) == 3


def test_exclude_legacy_remove_sem_run_id(plots, experiments):
    selected = plots.filter_experiments(experiments, exclude_legacy=True)
    assert len(selected) == 2
    assert all(e.get("run_id") for e in selected)


def test_filtro_por_workers(plots, experiments):
    selected = plots.filter_experiments(experiments, workers="8")
    assert len(selected) == 1
    assert selected[0]["workers"] == "8"


def test_filtro_por_mix(plots, experiments):
    selected = plots.filter_experiments(experiments, request_mix="mixed")
    assert len(selected) == 1
    assert selected[0]["request_mix"] == "mixed"


def test_filtro_por_rep(plots, experiments):
    selected = plots.filter_experiments(experiments, rep="2")
    assert len(selected) == 1
    assert selected[0]["rep"] == "2"


def test_filtro_por_run_id(plots, experiments):
    selected = plots.filter_experiments(
        experiments, run_id="cpu_C1_w1_uniform_1vus_r1_A"
    )
    assert len(selected) == 1
    assert selected[0]["run_id"] == "cpu_C1_w1_uniform_1vus_r1_A"


def test_filtro_sem_correspondencia_remove_legacy(plots, experiments):
    assert plots.filter_experiments(experiments, scenario="C2") == []


def test_filtra_sys_data_pelos_metadados(plots):
    sys_data = {
        "sys_w1.csv": [_sys("cpu_C1_w1_uniform_1vus_r1_A", workers="1")],
        "sys_w8.csv": [_sys("cpu_C1_w8_mixed_1vus_r2_B", workers="8", mix="mixed", rep="2")],
        "sys_legacy.csv": [{"elapsed_s": "1"}],
    }

    assert len(plots.filter_sys_data(sys_data)) == 3
    selected = plots.filter_sys_data(sys_data, workers="8", request_mix="mixed")
    assert list(selected) == ["sys_w8.csv"]
    only_new = plots.filter_sys_data(sys_data, exclude_legacy=True)
    assert "sys_legacy.csv" not in only_new
    assert len(only_new) == 2
