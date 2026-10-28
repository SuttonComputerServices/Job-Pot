import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("JOBPOT_DATA", "data")).resolve()
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'jobpot.db'}")
HTTPS = os.environ.get("JOBPOT_HTTPS") == "1"
WORKER_INTERVAL = int(os.environ.get("JOBPOT_WORKER_INTERVAL", "900"))
# Default instance-wide Ollama URL; admins can change it in the Admin page.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
