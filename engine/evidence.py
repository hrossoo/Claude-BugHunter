#!/usr/bin/env python3
"""
evidence.py — deterministic gates for the hackbot harness.

Three checks the podcast hackbots learned to do in CODE rather than by asking nicely
(docs/ctbb-claude-playbook.md):

  * op-ID evidence store  — every agent request is saved as evidence/requests/<op-id>.txt.
    A claim whose op IDs are not in the store is rejected before any LLM judges it
    (CTBB Ep 178: op-ID rule cut ~90% noise; Ep 196: agents "tested" without sending).
  * coverage gate         — a hunter's claimed rows must each come back with a status, and a
    "tested" row must cite at least one op ID that exists (Ep 178/182/197).
  * evidence scope audit  — hosts named in the evidence files are checked against scope.

Stdlib only.
"""
import os
import re

OP_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
ROW_STATUSES = ("tested-negative", "candidate", "blocked")


def requests_dir(eng_dir):
    d = os.path.join(eng_dir, "evidence", "requests")
    os.makedirs(d, exist_ok=True)
    return d


def op_path(eng_dir, op_id):
    if not OP_ID_RE.match(op_id or ""):
        return None
    return os.path.join(requests_dir(eng_dir), f"{op_id}.txt")


def op_exists(eng_dir, op_id):
    p = op_path(eng_dir, op_id)
    return bool(p and os.path.isfile(p) and os.path.getsize(p) > 0)


def missing_ops(eng_dir, op_ids):
    return [o for o in (op_ids or []) if not op_exists(eng_dir, o)]


def evidence_gate(eng_dir, claim):
    """(ok, reason). A claim must cite >=1 op ID and every cited op ID must exist."""
    ops = claim.get("op_ids") or []
    if not ops:
        return False, "no op IDs cited — claim has no evidence behind it"
    miss = missing_ops(eng_dir, ops)
    if miss:
        return False, f"op IDs not in evidence store: {', '.join(miss[:5])}"
    return True, ""


def coverage_gate(eng_dir, claimed_keys, reported_rows):
    """Split claimed rows into covered / uncovered / blocked.

    covered   : status tested-negative|candidate AND >=1 existing op ID
    blocked   : status blocked (reason kept; NOT counted as tested)
    uncovered : missing from the report, unknown status, or "tested" with no real evidence
    """
    by_key = {r.get("key"): r for r in (reported_rows or []) if r.get("key")}
    covered, uncovered, blocked = [], [], []
    for k in claimed_keys:
        r = by_key.get(k)
        if not r:
            uncovered.append((k, "not reported"))
            continue
        st = (r.get("status") or "").split(":")[0]
        if st == "blocked":
            blocked.append((k, r.get("reason") or r.get("status")))
        elif st in ("tested-negative", "candidate"):
            ops = r.get("op_ids") or []
            if ops and not missing_ops(eng_dir, ops):
                covered.append(k)
            else:
                uncovered.append((k, "claimed tested but no evidence file for its op IDs"))
        else:
            uncovered.append((k, f"unknown status {r.get('status')!r}"))
    return covered, uncovered, blocked


HOST_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)")
HOST_HDR_RE = re.compile(r"^\s*>?\s*Host:\s*([A-Za-z0-9.\-]+)", re.I | re.M)


REQ_LINE_RE = re.compile(r"^\s*(?:>|\$|curl\b|(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s|Host:|URL:|Request URL:"
                         r"|Request:|\* Connected to)", re.I)


def hosts_in_evidence(eng_dir, op_ids):
    """Hosts the agent SENT requests to — request-side lines only. Response bodies (HTML pages full of
    third-party links) are ignored, otherwise every crawl looks like a scope violation."""
    hosts = set()
    for o in op_ids or []:
        p = op_path(eng_dir, o)
        if not p or not os.path.isfile(p):
            continue
        for line in open(p, encoding="utf-8", errors="replace").read(200_000).splitlines():
            if REQ_LINE_RE.match(line):
                hosts |= set(HOST_RE.findall(line)) | set(HOST_HDR_RE.findall(line))
    return hosts


def scope_audit(eng_dir, scope, op_ids, ignore=()):
    """Out-of-scope hosts that appear in the cited evidence."""
    return sorted(h for h in hosts_in_evidence(eng_dir, op_ids)
                  if h not in ignore and not scope.in_scope_host(h))


def _selftest():
    import tempfile
    d = tempfile.mkdtemp()
    open(op_path(d, "r1-1"), "w").write("> GET /a HTTP/1.1\n> Host: localhost\n< 200\n")
    open(op_path(d, "r2-1"), "w").write("curl https://evil.example/x\n")
    open(op_path(d, "r3-1"), "w").write("> GET / HTTP/1.1\n> Host: localhost\n< 200\n<a href=https://owasp.org>x</a>\n")
    assert evidence_gate(d, {"op_ids": ["r1-1"]}) == (True, "")
    assert not evidence_gate(d, {"op_ids": []})[0]
    assert not evidence_gate(d, {"op_ids": ["r1-1", "nope"]})[0]
    assert op_path(d, "../etc/passwd") is None            # traversal-safe op IDs
    cov, unc, blk = coverage_gate(d, ["a", "b", "c", "d"], [
        {"key": "a", "status": "tested-negative", "op_ids": ["r1-1"]},
        {"key": "b", "status": "tested-negative", "op_ids": []},      # no evidence -> uncovered
        {"key": "c", "status": "blocked:auth", "reason": "auth broken"},
    ])                                                                 # d not reported
    assert cov == ["a"] and [u[0] for u in unc] == ["b", "d"] and blk[0][0] == "c"

    class S:
        def in_scope_host(self, h):
            return h in ("localhost", "127.0.0.1")
    assert scope_audit(d, S(), ["r1-1", "r2-1"]) == ["evil.example"]
    assert scope_audit(d, S(), ["r3-1"]) == []          # links in a response body are not requests
    print("evidence.py self-test: PASS")


if __name__ == "__main__":
    _selftest()
