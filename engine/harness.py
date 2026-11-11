#!/usr/bin/env python3
"""
harness.py — code-driven hackbot harness for the claude-bughunter subagents.

The orchestration pattern the Critical Thinking podcast hackbots converged on
(docs/ctbb-claude-playbook.md): deterministic code owns control flow, scope, coverage and
evidence; each judgment step is one headless Claude Code subagent from agents/*.md.

  recon (deterministic, engine.py) -> recon-ranker -> threat-modeler -> hunter xN
    -> reflector -> escalator -> validator -> poc-builder -> report-writer -> skill-distiller

Enforced in code, not in prompts:
  * scope            — PreToolUse hook (engine/hooks/scope_guard.py) blocks off-scope hosts,
                       state-changing methods in read-only runs, deletes outside the engagement
  * coverage gate    — a hunter's claimed rows must each come back covered (with evidence) or
                       blocked; uncovered rows are re-dispatched with a scope-restating
                       "keep going" nudge (CTBB Ep 175/178/182/197)
  * op-ID evidence   — candidates whose op IDs aren't in evidence/requests/ are rejected before
                       any LLM sees them (Ep 178, 196)
  * checkpoints      — the run stops for a human at review points and when it needs input;
                       re-running the same command resumes from the saved phase

Usage:
  python3 engine/harness.py --scope my-engagement.json --proxy http://127.0.0.1:8080
  python3 engine/harness.py --scope engine/engagement.example.json --mock --base /tmp/eng
  python3 engine/harness.py --scope my-engagement.json --status
  python3 engine/harness.py --scope my-engagement.json --watch      # tmux: log + live status
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone

ENGINE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ENGINE)
sys.path.insert(0, ENGINE)
from scope import Scope            # noqa: E402
from state import Engagement       # noqa: E402
import agent as A                  # noqa: E402
import evidence as EV              # noqa: E402
import memory                      # noqa: E402
import video as VID                # noqa: E402

PHASES = ["recon", "recon_ranker", "threat_model", "hunt", "reflect", "escalate",
          "validate", "poc", "video", "report", "distill"]
CHECKPOINTS = {"paranoid": {"threat_model", "reflect", "validate"},
               "normal": {"validate"},
               "yolo": set()}
STOP = "STOP"

# ---------------------------------------------------------------- output schemas
S_STR = {"type": "string"}
S_STRS = {"type": "array", "items": S_STR}
SCHEMAS = {
    "recon-ranker": {"type": "object", "required": ["rows", "summary"], "properties": {
        "rows": {"type": "array", "items": {"type": "object", "required": ["url", "vuln_classes"], "properties": {
            "url": S_STR, "method": S_STR, "auth_role": S_STR, "vuln_classes": S_STRS,
            "changed": {"type": "boolean"}, "note": S_STR}}},
        "hidden_headers": S_STRS, "summary": S_STR}},
    "threat-modeler": {"type": "object", "required": ["hypotheses"], "properties": {
        "needs_human": {"type": ["string", "null"]},
        "hypotheses": {"type": "array", "items": {"type": "object", "required": ["bug_class", "endpoints"], "properties": {
            "area": S_STR, "bug_class": S_STR, "endpoints": S_STRS, "why": S_STR, "cheapest_test": S_STR}}}}},
    "hunter": {"type": "object", "required": ["rows", "candidates"], "properties": {
        "rows": {"type": "array", "items": {"type": "object", "required": ["key", "status"], "properties": {
            "key": S_STR, "status": S_STR, "reason": S_STR, "op_ids": S_STRS}}},
        "candidates": {"type": "array", "items": {"type": "object", "required": ["title", "vuln_class", "op_ids"], "properties": {
            "title": S_STR, "vuln_class": S_STR, "url": S_STR, "rows": S_STRS, "op_ids": S_STRS,
            "impact": S_STR, "unproven": S_STR, "finding_file": S_STR}}},
        "dead_ends": {"type": "array", "items": {"type": "object", "properties": {"key": S_STR, "why": S_STR}}}}},
    "reflector": {"type": "object", "required": ["label", "why"], "properties": {
        "label": {"enum": ["gadget", "lead", "finding", "reject", "dead", "retry"]},
        "confidence": S_STR, "why": S_STR, "next": S_STR}},
    "escalator": {"type": "object", "required": ["escalated", "proposed_severity"], "properties": {
        "escalated": {"type": "boolean"}, "proposed_severity": S_STR, "op_ids": S_STRS, "summary": S_STR}},
    "validator": {"type": "object", "required": ["verdict", "severity", "reasons"], "properties": {
        "verdict": {"enum": ["PASS", "DOWNGRADE", "KILL"]}, "severity": S_STR, "cvss": S_STR,
        "reasons": S_STR, "unproven": S_STR, "stores_checked": S_STRS,
        "council": {"type": "array", "items": {"type": "object", "properties": {
            "persona": S_STR, "objection": S_STR, "defeated": {"type": "boolean"}}}}}},
    "poc-builder": {"type": "object", "required": ["format", "path", "run_output"], "properties": {
        "format": {"enum": ["python", "html", "manual"]},
        "path": {"type": "string", "description": "relative path of the PoC file ONLY, e.g. pocs/f01-x/poc.py"},
        "command_unix": S_STR, "command_windows": S_STR,
        "run_output": {"type": "string", "description": "relative path of the saved run output file ONLY "
                       "(e.g. pocs/f01-x/run-2026-01-01.txt) — no prose; put notes in `notes`"},
        "notes": S_STR, "video_checklist": S_STR}},
    "report-writer": {"type": "object", "required": ["report_path"], "properties": {
        "report_path": S_STR, "needs_human": {"type": ["string", "null"]}, "rubric_pass": {"type": "boolean"}}},
    "storyboard": VID.STORYBOARD_SCHEMA,
    "skill-distiller": {"type": "object", "required": ["proposal_path"], "properties": {
        "proposal_path": S_STR, "items": {"type": "integer"}}},
}

KEEP_GOING = ("KEEP GOING: these claimed rows have no covered result yet (or claimed 'tested' without an "
              "evidence file). Test each one now. Remember the rules: only hosts in scope, no write/delete "
              "to prove impact, no paid features, all traffic through the proxy, every request saved to "
              "evidence/requests/<op-id>.txt. If a row truly cannot be tested, mark it blocked:<reason>.")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _slug(s, n=40):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n] or "x"


class Harness:
    def __init__(self, a):
        self.a = a
        self.scope = Scope.load(a.scope)
        self.eng = Engagement(a.base, self.scope.name)
        self.dir = self.eng.dir
        for d in ("findings", "notes", "pocs", "logs", ".harness", "evidence/requests"):
            os.makedirs(os.path.join(self.dir, d), exist_ok=True)
        self.scope_copy = os.path.join(self.dir, ".harness", "scope.json")
        shutil.copyfile(a.scope, self.scope_copy)
        self.settings = self._write_settings()
        self.env = {"HARNESS_SCOPE": self.scope_copy, "HARNESS_ENG_DIR": self.dir,
                    "HARNESS_INTRUSIVE": "1" if a.allow_intrusive else "0",
                    "HARNESS_PROXY": a.proxy or ""}
        # NOTE: do NOT export HTTP(S)_PROXY — Claude Code itself honours them, so its own API calls would
        # go through Burp/Caido and fail TLS. Agents proxy target traffic explicitly via $HARNESS_PROXY.
        self.errors = 0
        self.H = self.eng.state.setdefault("harness", {
            "done": [], "hypotheses": [], "findings": {}, "blocked": {}, "uncovered": {},
            "dead_ends": [], "sessions": [], "nfind": 0})
        import engine as E            # deterministic recon/rank/RoE come from the existing engine
        self.E = E
        self.engine = E.Engine(a.scope, a.base, a.model or "", 0, a.max_turns, a.timeout, a.mock,
                               a.allow_intrusive, a.parallel)
        self.engine.eng = self.eng    # ONE state object: engine recon/rank and the harness share state.json

    # ------------------------------------------------------------ plumbing
    def _write_settings(self):
        hook = f"python3 {os.path.join(ENGINE, 'hooks', 'scope_guard.py')}"
        settings = {"hooks": {"PreToolUse": [
            {"matcher": "Bash|WebFetch", "hooks": [{"type": "command", "command": hook}]}]}}
        p = os.path.join(self.dir, ".harness", "settings.json")
        json.dump(settings, open(p, "w"), indent=2)
        return p

    def log(self, msg):
        self.eng.log(msg)

    def save(self):
        self.eng.save()

    def note(self, section, text):
        """Append under a `## section` heading in notes.md (created if missing)."""
        p = os.path.join(self.dir, "notes.md")
        body = open(p, encoding="utf-8").read() if os.path.isfile(p) else f"# Notes — {self.scope.name}\n"
        head = f"## {section}"
        if head not in body:
            body = body.rstrip() + f"\n\n{head}\n"
        idx = body.index(head) + len(head)
        nxt = body.find("\n## ", idx)
        nxt = len(body) if nxt == -1 else nxt
        body = body[:nxt].rstrip() + f"\n- {text}\n" + body[nxt:]
        open(p, "w", encoding="utf-8").write(body)

    def context(self):
        proxy = self.a.proxy or "(none configured — still save every request to the evidence store)"
        return (self.engine._roe() +
                f"\nENGAGEMENT FOLDER (your cwd): {self.dir}\n"
                f"- findings/ (finding-<NN>-<slug>.md), notes.md, notes/app-book.md, pocs/, evidence/requests/\n"
                f"REPO (read-only reference: skills/, agents/, docs/): {REPO}\n"
                f"PROXY: {proxy}. Send ALL target traffic through it explicitly: `curl -sk -x \"$HARNESS_PROXY\" …` "
                "(it is NOT set as HTTP(S)_PROXY). Python: urllib ProxyHandler with os.environ['HARNESS_PROXY'].\n"
                "EVIDENCE RULE: give every HTTP request an op ID (send header `X-Op-Id: <op-id>`) and save the "
                "full exchange (request line + headers + body, response status + headers + body) to "
                "evidence/requests/<op-id>.txt. Op IDs: letters/digits/.-_: only. Claims without saved op IDs "
                "are rejected automatically by the harness.\n"
                "TEST ACCOUNTS: store credentials only in .harness/accounts.json (never notes/ or findings/); never "
                "print passwords or tokens — load them into variables with jq. Redact JWTs/passwords in evidence.\n"
                "BACKGROUND PROCESSES: anything you start (e.g. python3 -m http.server to serve a PoC) you must stop "
                "before you finish; use a random high port, never reuse another agent's.\n"
                "A PreToolUse hook blocks off-scope hosts and destructive actions; if blocked, adapt — do not "
                "try to route around it.\n"
                "HARNESS MODE: your final message must be ONLY the JSON object your output schema asks for.\n")

    def dispatch(self, agent, task, label, schema_key=None):
        sid = str(uuid.uuid4())
        name = f"{self.scope.name}-{agent}-{_slug(label, 30)}"[:80]
        rec = {"agent": agent, "label": label, "session_id": sid, "resume": f"claude --resume {sid}",
               "log": f"logs/{name}.json"}
        if self.a.mock:
            r = {"data": MOCK.get(agent, lambda h, t: None)(self, task), "error": None, "session_id": sid}
        else:
            r = A.run_named_agent(agent, self.context() + "\nTASK:\n" + task,
                                  schema=SCHEMAS[schema_key or agent], cwd=self.dir, env=self.env,
                                  settings=self.settings, session_id=sid, session_name=name,
                                  model=self.a.model, max_turns=self.a.max_turns, timeout=self.a.timeout,
                                  add_dirs=[REPO], skills_on=self.a.skills,
                                  log_path=os.path.join(self.dir, rec["log"]))
        rec.update(error=r.get("error"), cost_usd=r.get("cost_usd"))
        return r, rec

    def record(self, rec):
        self.H["sessions"].append(rec)
        self.log(f"  {rec['agent']}[{rec['label']}] {'ERROR ' + rec['error'] if rec.get('error') else 'ok'} "
                 f"· resume: {rec['resume']}")

    def parallel(self, jobs):
        """jobs: list of (agent, task, label, ctx). Runs agents concurrently; yields (ctx, r, rec)
        in completion order. All state mutation happens in the caller (main thread)."""
        with cf.ThreadPoolExecutor(max_workers=max(1, self.a.parallel)) as ex:
            futs = {ex.submit(self.dispatch, j[0], j[1], j[2], j[4] if len(j) > 4 else None): j[3] for j in jobs}
            for f in cf.as_completed(futs):
                r, rec = f.result()
                self.record(rec)
                yield futs[f], r, rec

    def failed(self, r, what):
        """An agent that returned no structured data did NOT do the work: count it, leave the item where
        it was, and let run() stop the phase so a re-run retries only what failed."""
        if r.get("data") is None:
            self.errors += 1
            self.log(f"  ✗ {what}: agent error ({r.get('error') or 'no data'}) — left unchanged for retry")
            return True
        return False

    def new_finding(self, c, source):
        for f in self.H["findings"].values():          # same claim re-reported (e.g. after a nudge)
            if (f["vuln_class"], f["url"], f["title"]) == (c.get("vuln_class"), c.get("url", ""), c.get("title")):
                f["op_ids"] = sorted(set(f["op_ids"]) | set(c.get("op_ids") or []))
                if f["stage"] == "lead":
                    f["stage"] = "candidate"
                return f
        self.H["nfind"] += 1
        fid = f"F{self.H['nfind']:02d}"
        ff = c.get("finding_file") or f"findings/finding-{self.H['nfind']:02d}-{_slug(c.get('title'))}.md"
        path = os.path.join(self.dir, ff)
        if not os.path.isfile(path):
            open(path, "w", encoding="utf-8").write(
                f"# {c.get('title')}\n\n- class: {c.get('vuln_class')}\n- url: {c.get('url', '')}\n"
                f"- rows: {', '.join(c.get('rows') or [])}\n- op IDs: {', '.join(c.get('op_ids') or [])}\n\n"
                f"## Claimed impact\n{c.get('impact', '')}\n\n## Not proven\n{c.get('unproven', '')}\n")
        url = c.get("url") or next((r.split("|")[0] for r in (c.get("rows") or []) if "://" in r), "")
        f = {"id": fid, "title": c.get("title"), "vuln_class": c.get("vuln_class"), "url": url,
             "rows": c.get("rows") or [], "op_ids": list(c.get("op_ids") or []), "file": ff,
             "stage": "candidate", "source": source, "severity": "", "rounds": 0}
        self.H["findings"][fid] = f
        self.eng.add_candidate({k: f[k] for k in ("id", "title", "vuln_class", "url", "op_ids", "file")})
        return f

    def stage(self, *stages):
        only = {x.strip().upper() for x in (self.a.only or "").split(",") if x.strip()}
        return [f for f in self.H["findings"].values() if f["stage"] in stages and (not only or f["id"] in only)]

    # ------------------------------------------------------------ phases
    def p_recon(self):
        if self.eng.state["surface"] and not self.a.recon_again:
            self.log("recon: surface already present — skipping (use --recon-again to redo)")
        else:
            self.engine.recon()
        self.engine.rank()

    def p_recon_ranker(self):
        top = [{"url": s["url"], "param": s.get("param", ""), "class": s.get("vuln_class")}
               for s in self.eng.worklist()[:40]]
        task = ("Deterministic recon already produced this surface (top items):\n" + json.dumps(top, indent=1) +
                "\n\nYour job: get ALL the JS for the in-scope seeds " + ", ".join(self.scope.seeds) +
                " (bundles, source maps, lazy chunks), extract every endpoint / GraphQL operation / hidden header, "
                "diff against any earlier run under evidence/js/, and return coverage rows "
                "(one per endpoint+method+auth_role) with the bug classes worth testing on each.")
        r, rec = self.dispatch("recon-ranker", task, "js")
        self.record(rec)
        if self.failed(r, "recon-ranker"):
            return
        d = r.get("data") or {}
        items, dropped = [], 0
        for row in d.get("rows", []):
            if not self.scope.in_scope_host(row.get("url", "")):
                dropped += 1
                continue
            for cls in row.get("vuln_classes") or []:
                items.append({"url": row["url"], "param": "", "vuln_class": cls, "method": row.get("method", "GET"),
                              "auth_role": row.get("auth_role", ""), "source": "recon-ranker",
                              "note": row.get("note", ""), "changed": bool(row.get("changed"))})
        added = self.eng.add_surface(items)
        for h in d.get("hidden_headers", []) or []:
            self.note("Hidden headers", h)
        self.engine.rank()
        for s in self.eng.state["surface"]:
            if s.get("changed"):
                s["priority"] = s.get("priority", 0) + 20
        self.save()
        self.log(f"recon-ranker: +{added} coverage row(s), {dropped} off-scope row(s) dropped")

    def p_threat_model(self):
        task = ("Write notes/app-book.md for this target from the recon manifest/state (state.json), the JS in "
                "evidence/js/, public docs, and the account setup in CLAUDE.md/scope.md. Then return narrow, "
                "ranked hypotheses (one bug class + explicit endpoints each). If the account setup (roles, test "
                "accounts, enabled features) is unknown, set needs_human to the exact question.")
        r, rec = self.dispatch("threat-modeler", task, "book")
        self.record(rec)
        if self.failed(r, "threat-modeler"):
            return
        d = r.get("data") or {}
        if d.get("needs_human"):
            self.note("Questions for the human", d["needs_human"])
            self.checkpoint("threat-modeler needs input: " + d["needs_human"])
            return STOP
        self.H["hypotheses"] = d.get("hypotheses", [])
        items = []
        for h in self.H["hypotheses"]:
            for ep in h.get("endpoints") or []:
                if self.scope.in_scope_host(ep):
                    items.append({"url": ep, "param": "", "vuln_class": h.get("bug_class", ""),
                                  "source": "hypothesis", "note": h.get("why", "")})
        added = self.eng.add_surface(items)
        self.engine.rank()
        for s in self.eng.state["surface"]:
            if s.get("source") == "hypothesis":
                s["priority"] = s.get("priority", 0) + 30
        self.save()
        self.log(f"threat-modeler: {len(self.H['hypotheses'])} hypothesis(es), +{added} row(s)")

    def _hunt_task(self, cls, rows, extra=""):
        return (f"BUG CLASS: {cls}\nCLAIMED ROWS (test every one; report each by its exact key):\n" +
                json.dumps([{"key": Engagement._key(s), "url": s["url"], "param": s.get("param", ""),
                             "method": s.get("method", ""), "auth_role": s.get("auth_role", ""),
                             "note": s.get("note", "")} for s in rows], indent=1) +
                "\nRow status must be one of tested-negative | candidate | blocked:<reason>, with the op IDs "
                "that prove you tested it. Write candidates to findings/ and list them; list dead ends with "
                "why you think they are impossible." + (f"\n{extra}" if extra else ""))

    def _absorb_hunt(self, rows, r):
        """Apply coverage + evidence gates to one hunter result. Returns uncovered rows."""
        d = r.get("data") or {}
        keys = [Engagement._key(s) for s in rows]
        covered, uncovered, blocked = EV.coverage_gate(self.dir, keys, d.get("rows"))
        by_key = {Engagement._key(s): s for s in rows}
        for k in covered:
            self.eng.mark_tested(by_key[k])
            self.H["blocked"].pop(k, None)
            self.H["uncovered"].pop(k, None)
        for k, why in blocked:
            self.H["blocked"][k] = why
        for c in d.get("candidates", []) or []:
            ok, why = EV.evidence_gate(self.dir, c)
            if not ok:
                self.note("Validator rejections", f"[harness evidence gate] {c.get('title')}: {why}")
                self.log(f"  evidence gate REJECTED candidate '{c.get('title')}': {why}")
                continue
            f = self.new_finding(c, "hunter")
            oos = EV.scope_audit(self.dir, self.scope, f["op_ids"], ignore=set(filter(None, [self._proxy_host()])))
            if oos:
                f["scope_warning"] = oos
                self.log(f"  ⚠ {f['id']} evidence mentions off-scope host(s): {', '.join(oos)}")
            self.log(f"  candidate {f['id']} {f['vuln_class']} — {f['title']}")
        for de in d.get("dead_ends", []) or []:
            self.H["dead_ends"].append(de)
            self.note("Dead ends", f"{de.get('key', '')}: {de.get('why', '')}")
        self.save()
        return [by_key[k] for k, _ in uncovered], uncovered

    def _proxy_host(self):
        m = re.match(r"^(?:https?://)?([^:/]+)", self.a.proxy or "")
        return m.group(1) if m else None

    def p_hunt(self):
        wl = [s for s in self.eng.worklist() if Engagement._key(s) not in self.H["blocked"]][: self.a.max_rows]
        if not wl:
            self.log("hunt: worklist empty")
            return
        groups = {}
        for s in wl:
            groups.setdefault(s.get("vuln_class") or "misc", []).append(s)
        jobs = []
        for cls, rows in groups.items():
            for i in range(0, len(rows), self.a.slice_size):
                sl = rows[i:i + self.a.slice_size]
                jobs.append(("hunter", self._hunt_task(cls, sl), f"{cls}-{i // self.a.slice_size}", (cls, sl, 0)))
        self.log(f"hunt: {len(wl)} row(s) in {len(jobs)} hunter task(s), {self.a.parallel} at a time")
        while jobs:
            nxt = []
            for (cls, sl, nudge), r, rec in self.parallel(jobs):
                if self.failed(r, f"hunter {cls}"):
                    continue
                left, why = self._absorb_hunt(sl, r)
                if left and nudge < self.a.max_nudges:
                    self.log(f"  coverage gate: {len(left)} row(s) uncovered in {cls} — keep-going nudge {nudge + 1}")
                    nxt.append(("hunter", self._hunt_task(cls, left, KEEP_GOING),
                                f"{cls}-nudge{nudge + 1}", (cls, left, nudge + 1)))
                elif left:
                    for k, w in why:
                        self.H["uncovered"][k] = w
                    self.log(f"  coverage gate: {len(left)} row(s) still uncovered after {nudge} nudge(s) — surfaced at checkpoint")
            jobs = nxt
        self.save()

    def p_reflect(self):
        for rnd in range(2):
            todo = self.stage("candidate")
            dead = self.H["dead_ends"][:3] if rnd == 0 else []
            jobs = [("reflector", f"MODE A — classify candidate {f['id']}: read {f['file']} and its op-ID evidence "
                     f"({', '.join(f['op_ids'])}). Labels: gadget | lead | finding | reject. For lead, give the ONE next "
                     f"test in `next`.", f["id"], ("cand", f)) for f in todo]
            jobs += [("reflector", f"MODE B — fresh eyes on this dead end (you did not do the work): "
                      f"{json.dumps(de)}. Label `dead` if you agree, otherwise `retry` with the concrete next test "
                      f"in `next`.", f"dead-{i}", ("dead", de)) for i, de in enumerate(dead)]
            if not jobs:
                break
            rehunt = []
            for (kind, obj), r, rec in self.parallel(jobs):
                if self.failed(r, f"reflector {obj.get('id', obj.get('key', ''))}"):
                    continue
                d = r.get("data") or {}
                lab = d.get("label")
                if kind == "dead":
                    if lab == "retry" and d.get("next"):
                        self.note("Fresh-eyes follow-ups", f"{obj.get('key')}: {d['next']}")
                        rows = [s for s in self.eng.state["surface"] if Engagement._key(s) == obj.get("key")]
                        if rows:
                            rehunt.append((rows[0].get("vuln_class"), rows, f"Fresh-eyes next test: {d['next']}"))
                    continue
                f = obj
                f["reflector"] = d
                if lab == "gadget":
                    f["stage"] = "gadget"
                    self.note("Gadgets", f"{f['id']} {f['title']} ({f['vuln_class']}): {d.get('why', '')}")
                elif lab == "finding":
                    f["stage"] = "finding"
                elif lab == "lead" and f["rounds"] < 1 and d.get("next"):
                    f["stage"], f["rounds"] = "lead", f["rounds"] + 1
                    rows = [s for s in self.eng.state["surface"] if Engagement._key(s) in f["rows"]] or \
                           [{"url": f["url"], "param": "", "vuln_class": f["vuln_class"]}]
                    rehunt.append((f["vuln_class"], rows, f"Follow up lead {f['id']} (file {f['file']}): {d['next']}. "
                                   f"Append results to that same file and re-list it as a candidate if impact is proven."))
                else:
                    f["stage"] = "rejected"
                    self.note("Validator rejections", f"[reflector] {f['id']} {f['title']}: {d.get('why', lab)}")
                self.log(f"  reflector: {f['id']} -> {f['stage']}")
            self.H["dead_ends"] = [] if rnd == 0 else self.H["dead_ends"]
            self.save()
            if not rehunt:
                break
            jobs = [("hunter", self._hunt_task(c, rows, extra), f"followup-{i}", (c, rows, self.a.max_nudges))
                    for i, (c, rows, extra) in enumerate(rehunt)]
            for (c, rows, _), r, rec in self.parallel(jobs):
                if not self.failed(r, f"follow-up hunter {c}"):
                    self._absorb_hunt(rows, r)
            for f in self.stage("lead"):
                f["stage"] = "rejected"          # lead not re-raised by the follow-up hunter
                self.note("Validator rejections", f"[reflector] {f['id']}: lead not proven after follow-up")
            self.save()
        for f in self.stage("candidate"):        # raised in the last follow-up round, never classified
            f["stage"] = "unreviewed"
            self.note("Questions for the human", f"{f['id']} {f['title']} was raised in the last follow-up "
                      f"round and not classified — review {f['file']} by hand")
        self.save()

    def p_escalate(self):
        jobs = [("escalator", f"Escalate finding {f['id']} in {f['file']} (class {f['vuln_class']}). Walk the "
                 f"ladder; append `## Escalation` to the same file.", f["id"], f) for f in self.stage("finding")]
        for f, r, rec in self.parallel(jobs):
            if self.failed(r, f"escalator {f['id']}"):
                continue
            d = r.get("data") or {}
            ops = [o for o in d.get("op_ids", []) or [] if EV.op_exists(self.dir, o)]
            f["op_ids"] = sorted(set(f["op_ids"]) | set(ops))
            f["severity"] = d.get("proposed_severity") or f["severity"]
            f["stage"] = "escalated"
            self.log(f"  escalator: {f['id']} -> {f['severity'] or '?'} ({'escalated' if d.get('escalated') else 'no change'})")
        self.save()

    def p_validate(self):
        jobs = []
        for f in self.stage("escalated"):
            ok, why = EV.evidence_gate(self.dir, f)
            if not ok:
                f["stage"] = "killed"
                self.note("Validator rejections", f"[harness evidence gate] {f['id']}: {why}")
                continue
            jobs.append(("validator", f"Validate {f['id']} in {f['file']} (class {f['vuln_class']}, proposed "
                         f"severity {f['severity'] or 'unset'}). Evidence store op IDs: {', '.join(f['op_ids'])}. "
                         f"Append `## Validator verdict` to the file.", f["id"], f))
        for f, r, rec in self.parallel(jobs):
            if self.failed(r, f"validator {f['id']}"):
                continue                     # an errored validator is NOT a KILL verdict
            d = r.get("data") or {}
            v = d.get("verdict", "KILL")
            f["validator"] = {**d, "resume": rec["resume"]}
            if v in ("PASS", "DOWNGRADE"):
                f["stage"], f["severity"] = "confirmed", d.get("severity") or f["severity"]
                self.eng.confirm({"url": f["url"], "vuln_class": f["vuln_class"], "id": f["id"],
                                  "severity": f["severity"]}, {"real": True, "severity": f["severity"],
                                                               "reason": d.get("reasons", "")})
                try:
                    memory.record_finding(self.scope.name, memory._host(f["url"]), [],
                                          {"url": f["url"], "vuln_class": f["vuln_class"],
                                           "evidence_ref": f["file"]}, {"severity": f["severity"],
                                                                        "reason": d.get("reasons", "")})
                except Exception as e:
                    self.log(f"  memory: capture skipped ({e})")
            else:
                f["stage"] = "killed"
                self.note("Validator rejections", f"{f['id']} {f['title']}: {d.get('reasons', 'no verdict')}")
            self.log(f"  validator: {f['id']} -> {v} {f.get('severity', '')}")
        self.save()

    def p_poc(self):
        jobs = [("poc-builder", f"Build the PoC for {f['id']} ({f['file']}, {f['vuln_class']}, severity {f['severity']}). "
                 f"Write it to pocs/{f['id'].lower()}-{_slug(f['title'], 24)}/ and run it exactly as the triager will.",
                 f["id"], f) for f in self.stage("confirmed")]
        for f, r, rec in self.parallel(jobs):
            if self.failed(r, f"poc-builder {f['id']}"):
                continue
            d = r.get("data") or {}
            out = self._run_output_path(f, d)
            txt = open(out, encoding="utf-8", errors="replace").read() if out else ""
            proven = bool(re.search(r"(?<!NOT )VULNERABLE", txt))
            d = {**d, "run_output": os.path.relpath(out, self.dir) if out else d.get("run_output", "")[:200]}
            f["poc"] = {**d, "verified": proven}
            f["stage"] = "poc"
            self.log(f"  poc-builder: {f['id']} -> {d.get('format', '?')} "
                     f"{'VERIFIED (run output says VULNERABLE)' if proven else '⚠ run output missing or not VULNERABLE'}")
        self.save()

    def p_video(self):
        """PoC video: the poc-builder writes a storyboard (captions + browser steps in the target's own
        UI); engine/video/record.js films it deterministically — off-scope browser requests are aborted
        in code — then replays the PoC run output and a summary card. API-only bugs still get a video."""
        todo = [f for f in self.stage("poc", "reported") if not (f.get("video") or {}).get("path")]
        if self.a.no_video or not todo:
            return
        node_path = None if self.a.mock else VID.find_playwright()
        if not node_path:
            self.log("video: skipped (mock mode, or node + playwright not found — set HARNESS_PLAYWRIGHT_NODE_PATH)")
            return
        jobs = [("poc-builder", f"VIDEO STORYBOARD MODE for {f['id']} ({f['file']}, PoC {f['poc'].get('path', '')}). "
                 "Do not change any state; you may GET pages/JS through the proxy only to find selectors. Return a "
                 "storyboard of browser steps in the target's OWN web UI showing baseline -> exploit -> proof, each "
                 "step with a short caption (shown BEFORE the action) and a label like '1 · BASELINE'. Allowed "
                 f"actions: {', '.join(sorted(VID.ACTIONS))}. In-scope URLs only. Test-account secrets come from "
                 ".harness/accounts.json and must never appear in captions. Add cookies to dismiss consent/welcome "
                 "banners and hide_selectors for UI noise. If the bug is not visible in the UI (pure API), set "
                 "ui_visible=false and omit steps — the recorder still films the PoC run output. Give 3–4 summary "
                 "bullets (class, payload, impact, CVSS, fix).", f["id"], f, "storyboard")
                for f in todo]
        boards = []
        for f, r, rec in self.parallel(jobs):
            if self.failed(r, f"storyboard {f['id']}"):
                continue
            boards.append((f, r["data"]))
        for f, sb in boards:                                     # record sequentially: browsers are heavy
            steps, dropped = VID.clean_steps(sb.get("steps") if sb.get("ui_visible") else [], self.scope)
            for d in dropped:
                self.log(f"  video {f['id']}: dropped step ({d})")
            pdir = os.path.dirname(os.path.join(self.dir, f["poc"].get("path") or f"pocs/{f['id'].lower()}/x"))
            ro = os.path.join(self.dir, f["poc"].get("run_output") or "")
            base = next((st["url"] for st in steps if st.get("action") == "goto"), self.scope.seeds[0] if self.scope.seeds else "")
            v = f.get("validator") or {}
            job = {"out": os.path.join(pdir, f"{f['id'].lower()}-poc.webm"),
                   "title": sb.get("title") or f["title"],
                   "subtitle": sb.get("subtitle") or f"{f['vuln_class']} · {f.get('severity', '')} · {f['url']}",
                   "scope": {"in": self.scope.in_scope, "out": self.scope.out_of_scope},
                   "proxy": self.a.proxy or "", "intrusive": bool(self.a.allow_intrusive),
                   "hide_selectors": sb.get("hide_selectors") or [],
                   "cookies": [{"name": c.get("name"), "value": c.get("value", ""), "url": base}
                               for c in (sb.get("cookies") or []) if c.get("name") and base],
                   "steps": steps,
                   "run_output": open(ro, encoding="utf-8", errors="replace").read() if os.path.isfile(ro) else "",
                   "poc_command": f["poc"].get("command_unix") or "python3 poc.py",
                   "summary": sb.get("summary") or [f["title"], f"Severity: {f.get('severity', '')} {v.get('cvss', '')}"]}
            path, res = VID.record(job, node_path)
            if not path:
                self.log(f"  video {f['id']}: recording FAILED — {res.get('error', '?')[:200]}")
                self.note("Questions for the human", f"{f['id']}: PoC video failed — record it by hand")
                continue
            bad = [st for st in res.get("steps", []) if not st.get("ok")]
            f["video"] = {"path": os.path.relpath(path, self.dir), "ui": bool(steps),
                          "failed_steps": bad, "blocked_hosts": res.get("blocked_hosts", [])}
            self.log(f"  video {f['id']}: {f['video']['path']} ({'UI + ' if steps else ''}PoC replay"
                     f"{f'; {len(bad)} step(s) failed — review before attaching' if bad else ''}"
                     f"{'; blocked off-scope: ' + ', '.join(res['blocked_hosts']) if res.get('blocked_hosts') else ''})")
        self.save()

    def _run_output_path(self, f, d):
        """Find the saved PoC run output: the schema field if it is a real path, else a path mentioned in
        it, else the newest run*.txt in this finding's PoC folder."""
        cands = []
        ro = (d.get("run_output") or "").strip()
        if ro and "\n" not in ro and len(ro) < 300:
            cands.append(ro)
        cands += re.findall(r"(pocs/[^\s,;)'\"`]+?\.txt)", ro + " " + (d.get("notes") or ""))
        for c in cands:
            p = os.path.join(self.dir, c)
            if os.path.isfile(p):
                return p
        pdir = os.path.dirname(os.path.join(self.dir, d.get("path") or "")) if d.get("path") else ""
        globs = [pdir] if pdir and os.path.isdir(pdir) else []
        globs += [os.path.join(self.dir, "pocs", x) for x in os.listdir(os.path.join(self.dir, "pocs"))
                  if x.lower().startswith(f["id"].lower())]
        runs = [os.path.join(g, x) for g in globs for x in os.listdir(g) if x.startswith("run") and x.endswith(".txt")]
        return max(runs, key=os.path.getmtime) if runs else ""

    def p_report(self):
        ready, missing = [], []
        for f in self.stage("poc"):
            summ = os.path.join(self.dir, "findings", f"{f['id']}.summary.md")
            (ready if os.path.isfile(summ) and open(summ).read().strip() else missing).append(f)
        if missing:
            lines = [f"findings/{f['id']}.summary.md  — 2–5 lines in your own words for: {f['title']}" for f in missing]
            for ln in lines:
                self.note("Questions for the human", "write " + ln)
            self.checkpoint("report-writer needs YOUR summary for each confirmed finding:\n  " + "\n  ".join(lines))
            if not ready:
                return STOP
        jobs = [("report-writer", f"Write the report for {f['id']} ({f['file']}). Human summary: "
                 f"findings/{f['id']}.summary.md. PoC: {json.dumps(f.get('poc', {}))}. "
                 f"Video: {(f.get('video') or {}).get('path') or 'none — say so'}.", f["id"], f) for f in ready]
        for f, r, rec in self.parallel(jobs):
            if self.failed(r, f"report-writer {f['id']}"):
                continue
            d = r.get("data") or {}
            f["report"] = d.get("report_path")
            f["stage"] = "reported"
            self.log(f"  report-writer: {f['id']} -> {f['report']} (never auto-submitted)")
        self.save()
        if missing:
            return STOP

    def p_distill(self):
        summary = {fid: {k: f.get(k) for k in ("title", "vuln_class", "stage", "severity", "file")}
                   for fid, f in self.H["findings"].items()}
        r, rec = self.dispatch("skill-distiller", "Distil this run into notes/distill-" + _now() + ".md (propose, "
                               "don't apply). Findings: " + json.dumps(summary) + ". Read notes.md sections "
                               "Gadgets / Dead ends / Validator rejections and any edited final reports.", "run")
        self.record(rec)
        if self.failed(r, "skill-distiller"):
            return
        self.log(f"  skill-distiller: {(r.get('data') or {}).get('proposal_path', '?')}")

    def reap(self):
        """Kill PoC web servers agents left running in the engagement folder (a stale one on a reused port
        made a later agent re-run an old exploit). Only processes whose cwd is inside this engagement."""
        if self.a.mock or not shutil.which("pgrep"):
            return
        root = os.path.realpath(self.dir)
        out = subprocess.run(["pgrep", "-f", "http.server|SimpleHTTPServer"], capture_output=True, text=True).stdout
        for pid in out.split():
            cwd = ""
            if os.path.exists(f"/proc/{pid}/cwd"):
                cwd = os.path.realpath(f"/proc/{pid}/cwd")
            elif shutil.which("lsof"):
                r = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
                cwd = next((l[1:] for l in r.splitlines() if l.startswith("n")), "")
            if cwd and (cwd == root or cwd.startswith(root + "/")):
                subprocess.run(["kill", pid])
                self.log(f"  reaped orphaned PoC server pid {pid} (cwd {cwd})")

    # ------------------------------------------------------------ control
    def coverage(self):
        s = self.eng.summary()
        return (f"surface {s['surface']} · tested {s['tested']} · blocked {len(self.H['blocked'])} · "
                f"uncovered-after-nudges {len(self.H['uncovered'])}")

    def checkpoint(self, why):
        by = {}
        for f in self.H["findings"].values():
            by.setdefault(f["stage"], []).append(f"{f['id']} {f['vuln_class']} {f.get('severity', '')} — {f['title']}")
        md = [f"# Checkpoint — {self.scope.name} — {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
              "", f"**Why stopped:** {why}", "", f"**Coverage:** {self.coverage()}", ""]
        if self.H["blocked"]:
            md += ["## Blocked rows (not counted as tested)"] + [f"- {k}: {v}" for k, v in self.H["blocked"].items()] + [""]
        if self.H["uncovered"]:
            md += ["## Uncovered after keep-going nudges"] + [f"- {k}: {v}" for k, v in self.H["uncovered"].items()] + [""]
        for st, items in by.items():
            md += [f"## Findings: {st}"] + [f"- {i}" for i in items] + [""]
        md += ["## Resume any agent session", *[f"- {s['agent']}[{s['label']}]: `{s['resume']}`"
                                                 for s in self.H["sessions"][-15:]], "",
               "Review notes.md (Gadgets — chaining is your job), notes/app-book.md and findings/, then re-run the "
               "same harness command to continue."]
        p = os.path.join(self.dir, "notes", "CHECKPOINT.md")
        open(p, "w", encoding="utf-8").write("\n".join(md) + "\n")
        self.log(f"CHECKPOINT: {why.splitlines()[0]} -> {p}")

    def run(self):
        start = PHASES.index(self.a.from_phase) if self.a.from_phase else 0
        if self.a.from_phase:
            self.H["done"] = [p for p in self.H["done"] if PHASES.index(p) < start]
        stops = CHECKPOINTS[self.a.checkpoint]
        for ph in PHASES[start:]:
            if ph in self.H["done"]:
                continue
            self.eng.set_phase(f"harness:{ph}")
            self.log(f"== {ph} ==")
            self.errors = 0
            res = getattr(self, f"p_{ph}")()
            self.reap()
            if res == STOP:
                self.save()
                return 3
            if self.errors:
                self.save()
                self.checkpoint(f"{self.errors} agent error(s) in `{ph}` — phase NOT marked done; check logs/ "
                                f"and re-run the same command to retry only the failed items")
                return 4
            self.H["done"].append(ph)
            self.save()
            if self.a.until and ph == self.a.until:
                self.checkpoint(f"--until {ph} reached")
                return 0
            if ph in stops and ph != PHASES[-1]:
                self.checkpoint(f"--{self.a.checkpoint} review point after `{ph}`")
                return 0
        self.eng.set_phase("harness:done")
        self.checkpoint("run complete")
        self.log(f"harness done | {self.coverage()}")
        return 0


# ---------------------------------------------------------------- mock agents (offline dry-run)
def _mock_ev(h, op, url):
    open(EV.op_path(h.dir, op), "w").write(f"> GET {url} HTTP/1.1\n> X-Op-Id: {op}\n< HTTP/1.1 200 OK\n< {{\"mock\":true}}\n")


def _mock_hunter(h, task):
    rows = json.loads(task[task.index("[") : task.index("\nRow status")])
    first = "KEEP GOING" not in task
    out = {"rows": [], "candidates": [], "dead_ends": []}
    for i, row in enumerate(rows):
        op = f"m-{abs(hash(row['key'])) % 10**8}-{i}"
        if first and "search" in row["url"]:            # claim tested with no evidence -> nudge path
            out["rows"].append({"key": row["key"], "status": "tested-negative", "op_ids": ["missing-op"]})
            continue
        _mock_ev(h, op, row["url"])
        if "account" in row["url"] and "BUG CLASS: idor" in task:
            out["rows"].append({"key": row["key"], "status": "candidate", "op_ids": [op]})
            out["candidates"].append({"title": "IDOR on account id", "vuln_class": "idor", "url": row["url"],
                                      "rows": [row["key"]], "op_ids": [op], "impact": "other users' PII"})
        elif "redirect" in row["url"]:
            out["rows"].append({"key": row["key"], "status": "candidate", "op_ids": [op]})
            out["candidates"].append({"title": "Open redirect via next", "vuln_class": "open-redirect",
                                      "url": row["url"], "rows": [row["key"]], "op_ids": [op]})
        else:
            out["rows"].append({"key": row["key"], "status": "tested-negative", "op_ids": [op]})
    if first and "BUG CLASS: xss" in task:
        out["candidates"].append({"title": "Unevidenced claim", "vuln_class": "xss", "op_ids": ["never-saved"]})
    return out


def _mock_poc(h, task):
    fid = re.search(r"PoC for (F\d+)", task).group(1)
    d = os.path.join(h.dir, "pocs", fid.lower())
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "run.txt"), "w").write("[setup] ok\n[baseline] 403\n[exploit] 200\nVULNERABLE\n")
    return {"format": "python", "path": f"pocs/{fid.lower()}/poc.py", "run_output": f"pocs/{fid.lower()}/run.txt",
            "command_unix": "curl -s https://poc.example/x/poc.py | python3"}


MOCK = {
    "recon-ranker": lambda h, t: {"rows": [
        {"url": "http://localhost:3002/api/account?id=5", "method": "GET", "auth_role": "user", "vuln_classes": ["idor"]},
        {"url": "http://evil.example/api", "vuln_classes": ["ssrf"]}], "hidden_headers": ["X-Debug: 1"],
        "summary": "mock"},
    "threat-modeler": lambda h, t: {"needs_human": None, "hypotheses": [
        {"area": "accounts", "bug_class": "idor", "endpoints": ["http://localhost:3002/api/account?id=5"],
         "why": "sequential ids"}]},
    "hunter": _mock_hunter,
    "reflector": lambda h, t: ({"label": "dead", "why": "agree"} if "MODE B" in t else
                               {"label": "gadget", "why": "redirect only"} if "open-redirect" in t else
                               {"label": "finding", "why": "PII of another user in evidence"}),
    "escalator": lambda h, t: {"escalated": False, "proposed_severity": "high", "op_ids": []},
    "validator": lambda h, t: {"verdict": "PASS", "severity": "high", "reasons": "replayed: other user's PII",
                               "stores_checked": ["evidence/requests"]},
    "poc-builder": _mock_poc,
    "report-writer": lambda h, t: {"report_path": "findings/%s-report.md" % re.search(r"(F\d+)", t).group(1),
                                   "rubric_pass": True},
    "skill-distiller": lambda h, t: {"proposal_path": f"notes/distill-{_now()}.md", "items": 0},
}


# ---------------------------------------------------------------- CLI
def watch(h):
    if not shutil.which("tmux"):
        print("tmux not found — tail -f", h.eng.log_path)
        return 1
    sess = f"hackbot-{_slug(h.scope.name, 20)}"
    status = f"watch -n 5 python3 {os.path.abspath(__file__)} --scope {os.path.abspath(h.a.scope)} --base {h.a.base} --status"
    subprocess.call(["tmux", "new-session", "-d", "-s", sess, f"tail -n 200 -f {h.eng.log_path}"])
    subprocess.call(["tmux", "split-window", "-h", "-t", sess, status])
    return subprocess.call(["tmux", "attach", "-t", sess])


def main(argv=None):
    ap = argparse.ArgumentParser(description="claude-bughunter hackbot harness (code-driven subagent pipeline)")
    ap.add_argument("--scope", required=True, help="engagement scope JSON (name, in_scope, out_of_scope, seeds)")
    ap.add_argument("--base", default="~/.bughunter-engagements")
    ap.add_argument("--checkpoint", choices=list(CHECKPOINTS), default="paranoid",
                    help="where to stop for human review (default paranoid)")
    ap.add_argument("--proxy", default=os.environ.get("HARNESS_PROXY", ""), help="Caido/Burp proxy URL, e.g. http://127.0.0.1:8080")
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--slice-size", type=int, default=8, help="coverage rows per hunter task")
    ap.add_argument("--max-rows", type=int, default=60, help="max worklist rows per hunt phase")
    ap.add_argument("--max-nudges", type=int, default=2, help="keep-going re-dispatches for uncovered rows")
    ap.add_argument("--model", default=None, help="model override (default: your Claude Code default)")
    ap.add_argument("--max-turns", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per agent")
    ap.add_argument("--skills", action="store_true", help="let agents load skills (off by default: ~0 gain, ~12-15k tokens each)")
    ap.add_argument("--allow-intrusive", action="store_true", help="permit PUT/PATCH/DELETE (default read-only)")
    ap.add_argument("--from", dest="from_phase", choices=PHASES, help="re-run from this phase")
    ap.add_argument("--only", default="", help="comma-separated finding IDs to limit poc/video/report phases (e.g. F02)")
    ap.add_argument("--no-video", action="store_true", help="skip the PoC video phase")
    ap.add_argument("--until", choices=PHASES, help="stop after this phase")
    ap.add_argument("--recon-again", action="store_true", help="redo deterministic recon even if surface exists")
    ap.add_argument("--mock", action="store_true", help="offline dry-run with canned agent output")
    ap.add_argument("--status", action="store_true", help="print status and exit")
    ap.add_argument("--watch", action="store_true", help="tmux: live log + status panes")
    a = ap.parse_args(argv)
    h = Harness(a)
    if a.status:
        print(f"{h.scope.name}: phase={h.eng.state.get('phase')} done={h.H['done']}\n{h.coverage()}")
        for f in h.H["findings"].values():
            print(f"  {f['id']} [{f['stage']}] {f['vuln_class']} {f.get('severity', '')} — {f['title']}")
        return 0
    if a.watch:
        return watch(h)
    if not a.mock and not shutil.which("claude"):
        print("claude CLI not found on PATH", file=sys.stderr)
        return 2
    rc = h.run()
    print(f"\n{h.coverage()}\nengagement dir: {h.dir}\ncheckpoint: {os.path.join(h.dir, 'notes', 'CHECKPOINT.md')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
