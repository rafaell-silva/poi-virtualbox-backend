import pathlib
import sys

# Permite `from app.main import app` ao rodar o pytest da raiz do repositório.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
