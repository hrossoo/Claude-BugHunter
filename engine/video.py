#!/usr/bin/env python3
"""
video.py — harness glue for engine/video/record.js (deterministic PoC video recorder).

The poc-builder agent supplies a storyboard; this module validates it, builds the recorder job
(scope, proxy, run output, summary), runs Node + Playwright, and converts the result to MP4 with
ffmpeg when available. Videos are expected by triagers (CTBB Ep 170/173/179/194).
Stdlib only.
"""
import glob
import json
import os
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
RECORDER = os.path.join(HERE, "video", "record.js")
ACTIONS = {"goto", "type", "fill", "click", "press", "waitFor", "wait", "scroll", "caption"}

STORYBOARD_SCHEMA = {"type": "object", "required": ["ui_visible"], "properties": {
    "ui_visible": {"type": "boolean", "description": "true only if the bug can be shown in the target's own web UI"},
    "title": {"type": "string"}, "subtitle": {"type": "string"},
    "summary": {"type": "array", "items": {"type": "string"}},
    "cookies": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "value": {"type": "string"}}}},
    "hide_selectors": {"type": "array", "items": {"type": "string"}},
    "steps": {"type": "array", "items": {"type": "object", "required": ["action"], "properties": {
        "label": {"type": "string"}, "caption": {"type": "string"},
        "action": {"enum": sorted(ACTIONS)}, "url": {"type": "string"}, "selector": {"type": "string"},
        "text": {"type": "string"}, "key": {"type": "string"}, "ms": {"type": "integer"},
        "y": {"type": "integer"}, "hold": {"type": "integer"}}}}}}


def find_playwright():
    """Return a NODE_PATH dir from which `require('playwright')` resolves, or None."""
    if not shutil.which("node"):
        return None
    env_dir = os.environ.get("HARNESS_PLAYWRIGHT_NODE_PATH")
    cands = [env_dir] if env_dir else []
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20).stdout.strip()
        if root:
            cands.append(root)
    except Exception:
        pass
    cands += sorted(glob.glob(os.path.expanduser("~/.npm/_npx/*/node_modules")), key=os.path.getmtime, reverse=True)
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "playwright", "package.json")):
            r = subprocess.run(["node", "-e", "require('playwright')"], env={**os.environ, "NODE_PATH": c},
                               capture_output=True, text=True)
            if r.returncode == 0:
                return c
    return None


def clean_steps(steps, scope):
    """Keep only known actions; drop goto/URLs outside scope (the recorder also blocks them)."""
    out, dropped = [], []
    for s in steps or []:
        if s.get("action") not in ACTIONS:
            dropped.append(f"unknown action {s.get('action')!r}")
            continue
        if s.get("action") == "goto" and not scope.in_scope_host(s.get("url", "")):
            dropped.append(f"out-of-scope goto {s.get('url')}")
            continue
        out.append({k: v for k, v in s.items() if k in STORYBOARD_SCHEMA["properties"]["steps"]["items"]["properties"]})
    return out[:40], dropped


def record(job, node_path, timeout=300):
    """Run the recorder. Returns (mp4_or_webm_path or None, result dict)."""
    out_webm = job["out"]
    os.makedirs(os.path.dirname(out_webm), exist_ok=True)
    jpath = out_webm + ".job.json"
    json.dump(job, open(jpath, "w"), indent=1)
    try:
        p = subprocess.run(["node", RECORDER, jpath], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "NODE_PATH": node_path})
    except subprocess.TimeoutExpired:
        return None, {"error": "recorder timeout"}
    if p.returncode != 0 or not os.path.isfile(out_webm):
        return None, {"error": (p.stderr or p.stdout)[-400:]}
    try:
        res = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        res = {}
    final = out_webm
    if shutil.which("ffmpeg"):
        mp4 = out_webm[:-5] + ".mp4"
        c = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", out_webm, "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-crf", "23", mp4],
                           capture_output=True, text=True)
        if c.returncode == 0 and os.path.isfile(mp4):
            os.remove(out_webm)
            final = mp4
    os.remove(jpath)
    return final, res


def _selftest():
    class S:
        def in_scope_host(self, u):
            return "127.0.0.1" in u or "localhost" in u
    steps, dropped = clean_steps([{"action": "goto", "url": "http://localhost/x"},
                                  {"action": "goto", "url": "https://evil.example/"},
                                  {"action": "eval", "text": "alert(1)"},
                                  {"action": "type", "selector": "#e", "text": "x", "bogus": 1}], S())
    assert [s["action"] for s in steps] == ["goto", "type"] and "bogus" not in steps[1]
    assert len(dropped) == 2
    print("video.py self-test: PASS")


if __name__ == "__main__":
    _selftest()
