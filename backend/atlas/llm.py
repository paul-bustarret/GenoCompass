"""The single LLM entry point. Every other module calls `complete()`; only this file knows the backend.

Backends (env ATLAS_LLM):
  claude  (default) — `claude -p` headless, uses the local Claude Code login; no API key needed.
  ollama            — local model via Ollama's native /api/chat (e.g. qwen3:30b-a3b), thinking off.

Tiers map to models per backend: "fast" for bulk extraction, "smart" for writing.
The "fast" tier runs without extended thinking: extraction output is checked in code (quote, relevance),
and thinking made one abstract take ~50 s instead of ~6 s with `claude -p`.
Every call is cached on disk (data/llm_cache/) keyed by backend, model, prompts and schema,
so rebuilds are free and reproducible. Set ATLAS_LLM_NOCACHE=1 to bypass.
"""
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "llm_cache"
MODELS = {
    "claude": {"fast": "haiku", "smart": "sonnet"},
    "ollama": {"fast": "qwen3:30b-a3b", "smart": "qwen3:30b-a3b"},
}


class LLMError(RuntimeError):
    pass


def backend() -> str:
    return os.environ.get("ATLAS_LLM", "claude")


def complete(system: str, user: str, schema: dict | None = None, tier: str = "fast",
             timeout: int = 300, cache: bool = True) -> dict | str:
    """Return parsed JSON (dict) when `schema` is given, else plain text."""
    be = backend()
    model = MODELS[be][tier]
    key = hashlib.sha1(json.dumps([be, model, system, user, schema], sort_keys=True).encode()).hexdigest()
    path = CACHE / f"{key}.json"
    if cache and path.exists() and not os.environ.get("ATLAS_LLM_NOCACHE"):
        return json.loads(path.read_text())["output"]
    if be == "claude":
        out = _claude(system, user, schema, model, timeout, think=tier != "fast")
    else:
        out = _ollama(system, user, schema, model, timeout)
    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"backend": be, "model": model, "output": out}))
    return out


def _claude(system, user, schema, model, timeout, think=True):
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
           "--no-session-persistence", "--setting-sources", "", "--system-prompt", system]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    env = None if think else {**os.environ, "MAX_THINKING_TOKENS": "0"}
    res, last_err = None, None
    for _attempt in range(2):  # claude -p occasionally fails transiently; retry once
        try:
            proc = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=timeout, cwd="/tmp",
                                  env=env)
            res = json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            last_err = f"claude -p failed: {e}"
            continue
        if not res.get("is_error"):
            break
        last_err = (f"claude -p error ({res.get('subtype')}, api status {res.get('api_error_status')}): "
                    f"{res.get('result') or (proc.stderr or '').strip()[:200]}")
    else:
        raise LLMError(last_err)
    if schema:
        if res.get("structured_output") is not None:
            return res["structured_output"]
        return _parse_json(res.get("result", ""))
    return res.get("result", "")


def _ollama(system, user, schema, model, timeout):
    body = {"model": model, "stream": False, "think": False, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if schema:
        body["format"] = schema
    req = urllib.request.Request("http://localhost:11434/api/chat", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = json.load(r)["message"]["content"]
    except OSError as e:
        raise LLMError(f"ollama failed: {e}") from e
    return _parse_json(text) if schema else text


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"model did not return JSON: {text[:200]}") from e
