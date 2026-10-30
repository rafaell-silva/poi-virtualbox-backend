import os
import pathlib
import sys
import tempfile

# Permite `from app.main import app` ao rodar o pytest da raiz do repositório.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# IO_DIR é lido no import do módulo: define um diretório temporário antes.
os.environ.setdefault("IO_DIR", tempfile.mkdtemp(prefix="poi_io_test_"))
