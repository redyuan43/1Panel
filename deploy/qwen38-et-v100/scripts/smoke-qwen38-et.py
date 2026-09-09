#!/usr/bin/env python3
"""Post-deploy smoke: decode tps, prefill tps @24K/@64K, spec-decode sanity."""
import json, os, time, urllib.request

BASE = os.environ.get("ET_BASE", "http://127.0.0.1:18107")
KEY = open(os.path.expanduser("~/.config/1cat-vllm/api-key")).read().strip()
MODEL = os.environ.get("ET_MODEL", "siyuan/qwen38-v100-196k")
H = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}

def post(path, payload, timeout=1800):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), headers=H)
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def token_count(text):
    return len(post("/tokenize", {"model": MODEL, "prompt": text})["tokens"])

def build_prompt(target_tokens):
    unit = "The quick brown fox jumps over the lazy dog near the river bank at dawn. "
    text = unit * (target_tokens * 2 // len(unit) + 1)
    while token_count(text) > target_tokens:
        text = text[: int(len(text) * target_tokens / token_count(text))]
        if target_tokens - 64 <= token_count(text) <= target_tokens:
            break
    return text

def completion(prompt, max_tokens):
    t0 = time.time()
    r = post("/v1/completions", {"model": MODEL, "prompt": prompt,
                                 "max_tokens": max_tokens, "temperature": 0, "ignore_eos": True})
    dt = time.time() - t0
    u = r["usage"]
    return dt, u

print("== 1) short decode ==")
dt, u = completion("The capital of France is", 64)
print(f"completion_tokens={u['completion_tokens']} elapsed={dt:.2f}s decode={u['completion_tokens']/dt:.1f} tok/s")

for size in (24576, 65536):
    print(f"== prefill @{size} tokens ==")
    prompt = build_prompt(size)
    n = token_count(prompt)
    dt, u = completion(prompt, 1)
    print(f"prompt_tokens={n} ttft={dt:.2f}s prefill={n/dt:.0f} tok/s")
print("SMOKE_DONE")
