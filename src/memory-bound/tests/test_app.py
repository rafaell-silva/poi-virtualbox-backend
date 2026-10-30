import importlib.util
import pathlib
import sys

from fastapi.testclient import TestClient

# Carrega app/main.py com um nome de módulo único: os três projetos expõem
# `app.main`, e importá-los pelo nome colidiria no sys.modules do pytest.
_MODULE_NAME = "memory_bound_app_main"
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    _MODULE_NAME, _PROJECT_ROOT / "app" / "main.py"
)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = _module
_spec.loader.exec_module(_module)

app = _module.app
client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["process_pid"] > 0
    assert "system_memory" in body


def test_info():
    body = client.get("/info").json()
    assert body["app"] == "memory-bound-api"
    assert body["limits"]["max_size_mib_per_request"] >= 1


def test_workload_small_buffer():
    response = client.get(
        "/workload", params={"size_mib": 1, "iterations": 1, "dwell_ms": 0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["params"]["size_mib"] == 1
    assert body["timing"]["app_total_ms"] >= 0
    assert "memory_snapshots" in body


def test_workload_size_mb_alias():
    response = client.get(
        "/workload", params={"size_mb": 1, "iterations": 1, "dwell_ms": 0}
    )
    assert response.status_code == 200
    assert response.json()["params"]["size_mib"] == 1


def test_workload_rejects_buffer_above_limit():
    response = client.get("/workload", params={"size_mib": 9999})
    assert response.status_code == 422
