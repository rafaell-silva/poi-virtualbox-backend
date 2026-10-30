import importlib.util
import pathlib
import sys

from fastapi.testclient import TestClient

# Carrega app/main.py com um nome de módulo único: os três projetos expõem
# `app.main`, e importá-los pelo nome colidiria no sys.modules do pytest.
_MODULE_NAME = "io_bound_app_main"
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
    assert body["io_dir"]


def test_info():
    body = client.get("/info").json()
    assert body["app"] == "io-bound-api"
    assert body["defaults"]["size_mb"] >= 1


def test_process_writes_and_reads_expected_bytes():
    response = client.get(
        "/process", params={"size_mb": 1, "iterations": 1, "chunk_kb": 64}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["io"]["mib_written"] == 1.0
    assert body["io"]["mib_read"] == 1.0
    assert body["timing"]["app_total_ms"] >= 0


def test_process_without_fsync():
    response = client.get(
        "/process",
        params={"size_mb": 1, "iterations": 1, "chunk_kb": 64, "fsync": "false"},
    )
    assert response.status_code == 200
    assert response.json()["params"]["fsync"] is False


def test_process_rejects_invalid_size():
    response = client.get("/process", params={"size_mb": 0})
    assert response.status_code == 422
