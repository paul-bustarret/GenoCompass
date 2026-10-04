"""Cached HTTP: every response is saved under data/raw/ so reruns are free and reproducible."""
import hashlib
import json
import time
from pathlib import Path

import requests

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
UA = {"User-Agent": "rare-disease-atlas/0.1 (Hack-Nation hackathon)"}
_last_ncbi = [0.0]


def _path(source: str, key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return RAW / source / f"{h}.json"


def get_json(source: str, url: str, body: dict | None = None, retries: int = 3):
    """GET (or POST when body is given) with on-disk cache and retry. Returns parsed JSON."""
    key = url + (json.dumps(body, sort_keys=True) if body else "")
    path = _path(source, key)
    if path.exists():
        return json.loads(path.read_text())["response"]
    if "ncbi.nlm.nih.gov" in url:  # NCBI allows 3 req/s without an API key
        wait = 0.34 - (time.time() - _last_ncbi[0])
        if wait > 0:
            time.sleep(wait)
        _last_ncbi[0] = time.time()
    for attempt in range(retries):
        try:
            r = (requests.post(url, json=body, headers=UA, timeout=40) if body
                 else requests.get(url, headers=UA, timeout=40))
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            break
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "body": body, "retrieved": time.strftime("%Y-%m-%d"),
                                "response": data}))
    return data
