"""Optional local model via Ollama. Every caller must work when this returns None."""
import logging

import httpx

from .config import OLLAMA_MODEL, OLLAMA_URL
from .models import AppSetting

log = logging.getLogger("jobpot.ai")


def get_setting(db, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row else default


def set_setting(db, key: str, value: str):
    row = db.get(AppSetting, key) or AppSetting(key=key)
    row.value = value
    db.add(row)


def config(db) -> tuple[bool, str, str]:
    enabled = get_setting(db, "ai_enabled", "1" if OLLAMA_URL else "0") == "1"
    return enabled, get_setting(db, "ollama_url", OLLAMA_URL).rstrip("/"), get_setting(db, "ollama_model", OLLAMA_MODEL)


def ai_enabled(db) -> bool:
    enabled, url, _ = config(db)
    return enabled and bool(url)


def list_models(url: str) -> list[str]:
    r = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=5)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("that address answered, but not like an Ollama server")
    return [m.get("name", "?") for m in data["models"] if isinstance(m, dict)]


def generate(db, prompt: str, timeout: float = 60) -> str | None:
    if not ai_enabled(db):
        return None
    _, url, model = config(db)
    try:
        r = httpx.post(f"{url}/api/generate", json={"model": model, "prompt": prompt, "stream": False}, timeout=timeout)
        r.raise_for_status()
        return r.json()["response"].strip()
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("Ollama call failed: %s", e)
        return None
