"""Check that OPENAI_API_KEY works, at a cost of well under one cent. Never prints the key.

Run:  python3 scripts/check_openai_key.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

KEY = os.environ.get("OPENAI_API_KEY", "")
if not KEY:
    sys.exit("OPENAI_API_KEY is not set in this shell.")
print(f"key: set (length {len(KEY)})")


def call(path, body=None):
    req = urllib.request.Request(
        "https://api.openai.com/v1/" + path,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# 1. Free: list models (proves the key is valid and shows what it can use)
status, data = call("models")
if status != 200:
    sys.exit(f"models: HTTP {status} -> {data.get('error', {}).get('message', data)}")
ids = sorted(m["id"] for m in data["data"])
print(f"models: HTTP 200, {len(ids)} models available")
chat = [i for i in ids if i.startswith(("gpt-", "o")) and not any(x in i for x in
        ("audio", "realtime", "tts", "transcribe", "image", "search", "embedding"))]
print("  chat-capable examples:", ", ".join(chat[:15]))

# 2. Tiny paid call: pick the cheapest-looking model the key has
prefer = ("nano", "luna", "mini")
model = next((i for p in prefer for i in chat if p in i), chat[0] if chat else None)
if not model:
    sys.exit("no chat model available to this key")
status, data = call("chat/completions", {
    "model": model,
    "messages": [{"role": "user", "content": "Reply with exactly: atlas key ok"}],
    "max_completion_tokens": 200})
if status != 200:
    sys.exit(f"chat ({model}): HTTP {status} -> {data.get('error', {}).get('message', data)}")
u = data.get("usage", {})
print(f"chat ({model}): HTTP 200 -> {data['choices'][0]['message']['content']!r}")
print(f"  tokens: {u.get('prompt_tokens')} in, {u.get('completion_tokens')} out — cost well under $0.01")
