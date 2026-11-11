#!/usr/bin/env python3
"""
scope_guard.py — Claude Code PreToolUse hook used by the hackbot harness.

Blocks, in code, the failure modes the podcast hackbots hit (docs/ctbb-claude-playbook.md §3):
  * requests to hosts outside the engagement scope  ("Claude will just go out of scope to get
    impact" — CTBB Ep 175; agents following in-scope links off-scope — Ep 181)
  * state-changing HTTP methods when the run is read-only (deleting ~10M records / overwriting
    a bucket index "to prove impact" — Ep 198)
  * recursive deletes outside the engagement folder (the rm -rf incident — Ep 181)

Contract: reads the hook JSON on stdin; exit 0 = allow, exit 2 = block (stderr is shown to the
agent so it can adjust). Configured via env vars set by engine/harness.py:
  HARNESS_SCOPE      path to the engagement scope JSON (required; missing -> allow + warn)
  HARNESS_INTRUSIVE  "1" to allow PUT/PATCH/DELETE (default read-only)
  HARNESS_ENG_DIR    engagement folder (deletes are only allowed inside it)
  HARNESS_PROXY      proxy URL; its host is always allowed (it is the tester's own proxy)
Stdlib only.
"""
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL_RE = re.compile(r"\b(?:https?|wss?)://([A-Za-z0-9.\-]+|\[[0-9a-fA-F:]+\])(?::\d+)?", re.I)
METHOD_RE = re.compile(r"(?:-X|--request)\s*['\"]?(PUT|PATCH|DELETE)\b", re.I)
PY_METHOD_RE = re.compile(r"method\s*=\s*['\"](PUT|PATCH|DELETE)['\"]", re.I)
RM_RE = re.compile(r"(?:^|[;&|]\s*|\s)rm\s+(-[a-zA-Z]*[rR][a-zA-Z]*\s+|--recursive\s+)(.+?)(?=$|[;&|])", re.M)
CURL_PROXY_RE = re.compile(r"(?:-x|--proxy)\s+['\"]?(?:https?://)?([A-Za-z0-9.\-]+)", re.I)


def _block(msg):
    print(f"[scope_guard] BLOCKED: {msg}", file=sys.stderr)
    sys.exit(2)


def _allowed_extra():
    hosts = set()
    proxy = os.environ.get("HARNESS_PROXY", "")
    m = re.match(r"^(?:https?://)?([^:/]+)", proxy)
    if m:
        hosts.add(m.group(1))
    return hosts


def check_hosts(text, scope, extra):
    bad = []
    proxy_hosts = set(CURL_PROXY_RE.findall(text))
    for h in URL_RE.findall(text):
        h = h.strip("[]")
        if h in extra or h in proxy_hosts:
            continue
        if not scope.in_scope_host(h):
            bad.append(h)
    return sorted(set(bad))


def check_rm(cmd, eng_dir):
    eng_dir = os.path.realpath(eng_dir) if eng_dir else ""
    for m in RM_RE.finditer(cmd):
        try:
            targets = shlex.split(m.group(2))
        except ValueError:
            return "unparseable recursive rm"
        for t in targets:
            if t.startswith("-"):
                continue
            p = os.path.realpath(os.path.expanduser(t))
            if not eng_dir or not (p == eng_dir or p.startswith(eng_dir.rstrip("/") + "/")):
                return f"recursive delete outside the engagement folder: {t}"
            if p == eng_dir:
                return "recursive delete of the whole engagement folder"
    return None


def decide(event, scope, intrusive, eng_dir, extra):
    """Return None to allow, or a reason string to block."""
    tool = event.get("tool_name", "")
    ti = event.get("tool_input") or {}
    if tool == "WebFetch":
        bad = check_hosts(ti.get("url", ""), scope, extra)
        return f"out-of-scope host(s): {', '.join(bad)}" if bad else None
    if tool == "Bash":
        cmd = ti.get("command", "")
        bad = check_hosts(cmd, scope, extra)
        if bad:
            return (f"out-of-scope host(s): {', '.join(bad)}. Only hosts in the engagement scope may be "
                    f"contacted — report the lead instead of following it off-scope.")
        if not intrusive and (METHOD_RE.search(cmd) or PY_METHOD_RE.search(cmd)):
            return ("state-changing HTTP method in a read-only run. Show the capability with a read "
                    "and report the write capability itself — never write/delete to prove impact.")
        r = check_rm(cmd, eng_dir)
        if r:
            return r + " (read a folder before deleting anything; never delete outside the engagement)"
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    scope_path = os.environ.get("HARNESS_SCOPE")
    if not scope_path or not os.path.isfile(scope_path):
        print("[scope_guard] WARNING: HARNESS_SCOPE not set — not enforcing", file=sys.stderr)
        sys.exit(0)
    from scope import Scope
    scope = Scope.load(scope_path)
    eng_dir = os.path.realpath(os.environ.get("HARNESS_ENG_DIR", "")) if os.environ.get("HARNESS_ENG_DIR") else ""
    reason = decide(event, scope, os.environ.get("HARNESS_INTRUSIVE") == "1", eng_dir, _allowed_extra())
    if reason:
        _block(reason)
    sys.exit(0)


def _selftest():
    from scope import Scope
    s = Scope(["localhost", "127.0.0.1", "*.target.test"], ["admin.target.test"])
    ex = {"127.0.0.1"}
    eng = "/tmp/eng-x"
    B = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}}
    assert decide(B("curl -sk https://api.target.test/v1/me"), s, False, eng, ex) is None
    assert decide(B("curl https://evil.example/x"), s, False, eng, ex)
    assert decide(B("curl https://admin.target.test/"), s, False, eng, ex)            # deny wins
    assert decide(B("curl -x http://127.0.0.1:8080 https://api.target.test/"), s, False, eng, ex) is None
    assert decide(B("curl -X DELETE https://api.target.test/v1/item/1"), s, False, eng, ex)
    assert decide(B("curl -X DELETE https://api.target.test/v1/item/1"), s, True, eng, ex) is None
    assert decide(B("rm -rf /tmp/eng-x/tmpdir"), s, False, eng, ex) is None
    assert decide(B("rm -rf ~/Documents"), s, False, eng, ex)
    assert decide(B("cd x && rm -rf /tmp/eng-x"), s, False, eng, ex)
    assert decide({"tool_name": "WebFetch", "tool_input": {"url": "https://evil.example"}}, s, False, eng, ex)
    assert decide({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, s, False, eng, ex) is None
    print("scope_guard.py self-test: PASS")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        main()
