---
name: autopilot
description: Run autonomous hunt loop on a target — scope check → recon → rank surface → hunt → validate → report with configurable checkpoints. Usage: /autopilot target.com [--paranoid|--normal|--yolo]
---

# /autopilot

Autonomous hunt loop with deterministic scope safety and configurable checkpoints.

## Usage

```
/autopilot target.com                    # default: --paranoid mode
/autopilot target.com --normal           # batch checkpoint after validation
/autopilot target.com --yolo             # minimal checkpoints (still requires report approval)
/autopilot target.com --quick            # fast surface scan, fewer checks, lower token use
/autopilot targets.txt                   # multiple targets — one domain per line in the file
```

## Session Isolation (Important)

**Start a fresh Claude Code session per target.** Claude accumulates context across a session —
testing multiple targets in one session causes cross-contamination where findings, payloads,
and tech stack assumptions from target A bleed into target B.

Best practice:
```bash
# Terminal 1: target A
claude  →  /autopilot targetA.com

# Terminal 2: target B (separate process)
claude  →  /autopilot targetB.com
```

If you must test multiple targets in one session, run `/pickup target.com` at the start of
each target switch to reload the correct context.

## Token Optimization

Use `--quick` for faster, lower-cost scans (skips deep fuzzing and extended nuclei templates):
```
/autopilot target.com --quick    # ~40% fewer tokens, covers main attack surface
/hunt target.com --vuln-class idor   # single bug class — lowest token use
```

For long hunts, run `/compact` (Claude Code built-in) periodically to compress context
without losing findings.

## What This Does

Runs the full hunt cycle without stopping for approval at each step:

```
1. SCOPE     Load and confirm program scope
2. RECON     Run recon (or use cached if < 7 days old)
3. RANK      Prioritize attack surface (recon-ranker agent)
4. HUNT      Test P1 endpoints systematically
5. VALIDATE  7-Question Gate on findings
6. REPORT    Draft reports for validated findings
7. CHECKPOINT  Present to human for review
```

## Subagent pipeline

When the plugin's subagents are installed (`agents/`, also copied to `~/.claude/agents/` by
`install.sh`), each step above is delegated to a dedicated subagent. Full rationale and sources:
`docs/ctbb-claude-playbook.md`.

| Step | Subagent | Contract |
|---|---|---|
| RECON + RANK | `recon-ranker` | Gets *all* the JS (maps, lazy chunks), extracts endpoints / GraphQL ops / hidden headers, diffs vs last run, seeds a coverage worklist of `(endpoint, method, auth_role)` rows |
| (new) MODEL | `threat-modeler` | Writes `notes/app-book.md` + narrow hypotheses. In `--paranoid`, stop here so the human reads the book |
| HUNT | `hunter` × `--parallel` | One bug class + explicit rows per instance; claims rows, append-only results, op-ID on every request, **cannot finish with untested claimed rows** |
| (new) REFLECT | `reflector` | Labels each candidate gadget / lead / finding; gadgets → `notes.md` `## Gadgets` for the human to chain; fresh-eyes review of "impossible" dead ends |
| (new) ESCALATE | `escalator` | Tries to raise severity on the *same* finding before validation |
| VALIDATE | `validator` | Skeptical-triager replay, op-ID evidence rule, class proof bars, triage council, 7-Question Gate |
| REPORT | `poc-builder` → `report-writer` | Self-checking PoC (`curl -s … \| python3` or autorun HTML), then a PoC-first concise draft from the human's summary |
| (after) LEARN | `skill-distiller` | Proposes CLAUDE.md gadget/insight entries, skill edits and ledger updates for approval |

**Who orchestrates.** Subagents cannot launch other subagents or ask you questions mid-run, so the
**main session runs every step**: it dispatches each agent, reads the files it wrote (each ends its
output with `files written:` / `next:`), dispatches the next one, and relays any returned question to
you. Agents that need Burp/Caido/browser MCP (recon-ranker, threat-modeler, hunter, escalator,
validator, poc-builder) omit `tools:` so they inherit MCP tools.

**Coverage gate.** HUNT is not complete while any coverage row is `open` or `in_progress`. Rows end
as `tested-negative`, `candidate`, or `blocked:<reason>` — "blocked: auth broken" is surfaced at the
checkpoint, never silently counted as coverage.

**Keep-going rule.** If a hunter stops early, re-prompt it with the scope restated: *keep going if
you're on a good lead or have untested rows — only hosts in scope.md, no write/delete to prove
impact, all traffic through the proxy.* "Keep going" never means leaving scope. Optional unattended
version: `docs/templates/hooks/keep-going.md`.

## Safety Guarantees

- **Every URL** is checked against the scope allowlist before any request
- **Reports are NEVER auto-submitted** — always requires explicit approval
- **PUT/DELETE/PATCH** require human approval in --yolo mode (safe methods only)
- **Circuit breaker** stops hammering if 5 consecutive 403/429/timeout on same host
- **Rate limited** at 1 req/sec (testing) and 10 req/sec (recon)

## Checkpoint Modes

| Mode | When it stops | Best for |
|---|---|---|
| `--paranoid` | Every finding + partial signal | New targets, learning the surface |
| `--normal` | After validation batch | Systematic coverage |
| `--yolo` | After full surface exhausted | Familiar targets, experienced hunters |

## Ledger memory (token saving)

The autonomous loop can consult the **autopilot ledger** to skip provably-wasteful agent
calls — an item already **confirmed** on a prior run (carried forward, not re-tested) or a
vuln_class with strong **negative** history on this tech stack (`dead-class`).

- **`--paranoid` (default) / `--normal`:** memory is **off** — full coverage, no skips.
- **`--quick` / `--yolo`:** the command passes `--use-memory` to the engine — skips on.
- A vuln_class never hunted on this stack is **never** skipped, and every skip is logged
  (`hunt: skipped via ledger (...)`). Coverage is never silently reduced.
- Capture is automatic; the ledger fills itself as you run. Manage size with `/memory-gc`.

## After Autopilot

- Run `/remember` to log successful patterns to hunt memory
- Run `/pickup target.com` next time to pick up where you left off
- Check the ledger (`~/.claude/bughunter/memory/findings.jsonl`) or run `/memory-gc` for its size
