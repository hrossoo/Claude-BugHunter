#!/usr/bin/env python3
"""
agent.py — the engine's LLM dispatch. Only recon/hunt/validate are model-driven;
the orchestrator, scope, and state are deterministic code.

Engine = headless `claude -p`: skills auto-activate, Burp MCP is the hands. Agents
are asked to end with a fenced ```json``` block which we parse into structured data.
"""
import json
import os
import re
import subprocess
import time

ENGINE = os.path.dirname(os.path.abspath(__file__))
MCP_CONFIG = os.path.join(ENGINE, "burp-mcp.json")
ALLOWED_TOOLS = " ".join([
    "mcp__burp__send_http1_request", "mcp__burp__send_http2_request",
    "mcp__burp__get_collaborator_interactions", "mcp__burp__generate_collaborator_payload",
    "Bash(curl:*)", "Bash(python3:*)", "Bash(jq:*)", "Bash(openssl:*)", "Bash(base64:*)",
])


def run_agent(task, skills_on=False, model="claude-sonnet-4-6", max_turns=40, timeout=600):
    # skills OFF by default: the eval showed they add ~0 capability but cost ~12-15k tokens/agent.
    cmd = ["claude", "-p", task,
           "--mcp-config", MCP_CONFIG, "--strict-mcp-config",
           "--permission-mode", "bypassPermissions",
           "--allowedTools", ALLOWED_TOOLS,
           "--max-turns", str(max_turns), "--model", model,
           "--output-format", "json"]
    if not skills_on:
        cmd.append("--disable-slash-commands")
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"result": "", "error": "timeout", "duration_s": round(time.time() - t0, 1)}
    except (FileNotFoundError, OSError) as e:
        # e.g. the `claude` CLI isn't installed / not on PATH — return a structured
        # error instead of raising, so a single missing binary can't crash the phase.
        return {"result": "", "error": f"exec:{e}", "duration_s": round(time.time() - t0, 1)}
    try:
        d = json.loads(p.stdout)
        res = d.get("result") or ""
        # usage-limit / API errors come back as a short result with no real work
        if "usage limit" in res.lower() or "session limit" in res.lower():
            return {"result": res, "error": "rate-limited", "duration_s": round(time.time() - t0, 1)}
        return {"result": res, "cost_usd": d.get("total_cost_usd"),
                "num_turns": d.get("num_turns"), "error": None,
                "duration_s": round(time.time() - t0, 1)}
    except Exception as e:
        return {"result": p.stdout[:300], "error": f"parse:{e}", "duration_s": round(time.time() - t0, 1)}


REPO = os.path.dirname(ENGINE)
AGENTS_DIR = os.path.join(REPO, "agents")


def load_agent_def(name):
    """Read agents/<name>.md -> {description, prompt, tools?} for `claude --agents`.

    Loading the definition from the repo (instead of relying on an installed copy) pins the
    harness to the agent text in this checkout and works without install.sh."""
    raw = open(os.path.join(AGENTS_DIR, f"{name}.md"), encoding="utf-8").read()
    parts = raw.split("\n")
    close = next(i for i in range(1, len(parts)) if parts[i].strip() == "---")
    fm = {}
    for line in parts[1:close]:
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            fm[m.group(1)] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v
    d = {"description": fm.get("description", name), "prompt": "\n".join(parts[close + 1:]).strip()}
    if fm.get("tools"):
        d["tools"] = [t.strip() for t in fm["tools"].split(",") if t.strip()]
    return d


def run_named_agent(name, task, schema=None, cwd=None, env=None, settings=None, session_id=None,
                    session_name=None, model=None, max_turns=60, timeout=1800, add_dirs=(),
                    skills_on=False, log_path=None, resume=None, _repair=True):
    """Run one claude-bughunter subagent headless via `claude -p --agents … --agent <name>`.

    Returns {"data": <parsed JSON or None>, "result": str, "error": str|None, "session_id",
    "cost_usd", "num_turns", "duration_s"}. `--json-schema` makes the final answer structured;
    `--session-id` + `-n` let a human `claude --resume <id>` into the exact session later
    (CTBB Ep 175: validator output carries a resume command)."""
    cmd = ["claude", "-p", task,
           "--agents", json.dumps({name: load_agent_def(name)}), "--agent", name,
           "--output-format", "json", "--permission-mode", "bypassPermissions",
           "--max-turns", str(max_turns)]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if settings:
        cmd += ["--settings", settings]
    if resume:
        cmd += ["--resume", resume]
    elif session_id:
        cmd += ["--session-id", session_id]
    if session_name:
        cmd += ["-n", session_name]
    if model:
        cmd += ["--model", model]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    if os.path.isfile(MCP_CONFIG):
        cmd += ["--mcp-config", MCP_CONFIG]
    if not skills_on:
        cmd.append("--disable-slash-commands")
    t0 = time.time()
    out = {"data": None, "result": "", "error": None, "session_id": resume or session_id,
           "cost_usd": None, "num_turns": None}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired:
        return {**out, "error": "timeout", "duration_s": round(time.time() - t0, 1)}
    except (FileNotFoundError, OSError) as e:
        return {**out, "error": f"exec:{e}", "duration_s": round(time.time() - t0, 1)}
    out["duration_s"] = round(time.time() - t0, 1)
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(p.stdout or "")
            if p.stderr:
                fh.write("\n--- stderr ---\n" + p.stderr)
    try:
        d = json.loads(p.stdout)
    except Exception as e:
        return {**out, "result": (p.stdout or p.stderr)[:300], "error": f"parse:{e}"}
    res = d.get("result") or ""
    out.update(result=res, cost_usd=d.get("total_cost_usd"), num_turns=d.get("num_turns"),
               session_id=d.get("session_id") or session_id)
    if "usage limit" in res.lower() or "session limit" in res.lower():
        return {**out, "error": "rate-limited"}
    if d.get("is_error"):
        out["error"] = f"agent:{d.get('subtype') or 'error'}"
    data = d.get("structured_output")
    if data is None:
        try:
            data = json.loads(res)
        except Exception:
            data = extract_json(res)
    if data is None and schema and out["session_id"] and not out["error"] and _repair:
        # The agent answered in prose. Resume the SAME session once and ask only for the JSON.
        rep = run_named_agent(name, "Output ONLY the final JSON object matching the output schema for the "
                              "work you just did. No prose.", schema=schema, cwd=cwd, env=env,
                              settings=settings, model=model, max_turns=3, timeout=300,
                              skills_on=skills_on, resume=out["session_id"], _repair=False)
        data = rep.get("data")
        out["cost_usd"] = (out["cost_usd"] or 0) + (rep.get("cost_usd") or 0)
    out["data"] = data
    if data is None and not out["error"]:
        out["error"] = "no-structured-output"
    return out


def extract_json(text):
    """Pull the last valid JSON array/object out of an agent reply."""
    if not text:
        return None
    blocks = re.findall(r"```json\s*(.*?)```", text, re.S)
    blocks += re.findall(r"```\s*(\[.*?\]|\{.*?\})\s*```", text, re.S)
    for b in reversed(blocks):
        try:
            return json.loads(b.strip())
        except Exception:
            pass
    for b in reversed(re.findall(r"(\[.*\]|\{.*\})", text, re.S)):
        try:
            return json.loads(b)
        except Exception:
            pass
    return None


if __name__ == "__main__":
    # offline self-test of the JSON extractor (no agent call)
    assert extract_json('blah ```json\n[{"a":1}]\n``` end') == [{"a": 1}]
    assert extract_json('text {"x": "y"} more') == {"x": "y"}
    assert extract_json("no json here") is None
    assert extract_json('first {"a":1} then ```json\n{"b":2}\n```') == {"b": 2}  # prefers fenced/last
    print("agent.py extractor self-test: PASS")
