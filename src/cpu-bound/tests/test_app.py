import importlib.util
import pathlib
import sys

from fastapi.testclient import TestClient

# Carrega app/main.py com um nome de módulo único: os três projetos expõem
# `app.main`, e importá-los pelo nome colidiria no sys.modules do pytest.
_MODULE_NAME = "cpu_bound_app_main"
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
    assert response.json() == {"status": "ok"}


def test_info():
    body = client.get("/info").json()
    assert body["pod"]
    assert body["version"]


def test_process_image_get():
    response = client.get(
        "/process-image", params={"intensity": 1, "chunk_kib": 4}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processed"
    assert body["intensity"] == 1
    assert body["chunk_kib"] == 4
    assert len(body["checksum"]) == 16
    assert body["served_by"]


def test_process_image_post():
    response = client.post(
        "/process-image", params={"intensity": 1, "chunk_kib": 4}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "processed"


def test_process_image_rejects_invalid_intensity():
    response = client.get("/process-image", params={"intensity": 0})
    assert response.status_code == 422
