# Engagement engine

A **decision-support engine**, not an autonomous robot. It maps a target's attack surface
deterministically, **categorizes it against the skill arsenal** (which `hunt-*` skill applies
where), and **shows you each stage live** — so you spend your expert 20% where the 80%
automation points. It does **not** try to test everything itself; active hunting is opt-in.

## Design principles
1. **Deterministic-first.** Breadth (recon, parameter discovery, secret scanning, service
   fingerprint, categorization) is plain Python + passive tooling — **`$0`, no agents, no
   rate-limit burn**. The LLM is reserved for the few judgment calls in the opt-in hunt.
2. **Map-first, hunting opt-in.** A default run stops at the **arsenal map** and hands off to
   you. Spend agents only when you choose (`--hunt`).
3. **Show every stage.** Each phase logs what it found, live, so you always know where to focus.
4. **Curl-first.** Active testing uses `curl`; Burp MCP is optional (OOB/blind/fuzzing only).
5. **Safe by construction.** Scope is a deterministic allowlist; agents run **read-only by
   default** with hard rules-of-engagement; a deterministic scope-audit flags any out-of-scope PoC.

```
            DEFAULT (deterministic, $0, no agents)        │  OPT-IN  (--hunt)
 recon ───▶ rank ───▶ map ──────────────────────────────▶│  hunt ─▶ validate ─▶ report
   │          │         └ surface → skill arsenal +       │   │         └ adversarial verifier (read-only)
   │          │           curl-first probes (arsenal.md)  │   └ curl-first agent per ranked (url,param,class), skills-off
   │          └ class-weight priority (secrets boosted)   │
   └ service/tech + JS endpoints + JS secrets + ALL params (gau + katana), categorized
```
> **OSINT (subdomain/asset enum) is a *separate* concern** — handled by Claude-OSINT, not the
> engine. The engine takes the in-scope seed target(s) and goes straight to deterministic recon.

| Piece | What it is |
|---|---|
| `scope.py` | deterministic allowlist (apex/wildcard/CIDR/regex; deny-wins; default-deny). Enforced at recon **and** hunt. |
| `recon.py` | **deterministic recon** — per target: service/tech (JS markers), JS-bundle endpoint mining, **JS secret scanning** (AWS/GCP/Anthropic/OpenAI/Slack/GitHub/Stripe/EmailJS… keys, redacted), and **all input parameters** from two independent sources — `gau` (passive historical) + `katana` (active live crawl). Noise-filtered, scope-filtered, multi-class categorized. No LLM in the find-step. |
| `skill_map.py` | **arsenal categorization** — maps each `(endpoint\|param, class)` → the specific `hunt-*` skill(s) **actually installed** in `~/.claude/skills`, plus a curl-first starter probe. Tech-stack skills (`hunt-nextjs`, `hunt-nodejs`…) mapped from the fingerprint. |
| `osint.py` | **separate/optional** — per-target service/tech probe (PD `httpx-toolkit`) reused by recon. Its subdomain-enum path is *not* in the engine flow (that's Claude-OSINT). |
| `state.py` | persistent, resumable engagement store (`state.json` + `evidence/` + `engine.log` + `arsenal.md` + `report.md`). |
| `agent.py` | headless `claude -p` dispatch + JSON extraction. **Skills OFF by default** (eval: ~0 capability gain, saves ~12–15k tokens/agent). Used only for the opt-in hunt/validate. |
| `engine.py` | the orchestrator: phases, scope enforcement, ranking, the map, parallel hunt/validate, candidate→confirm, report. |

## The map (the default deliverable)
`map` writes `arsenal.md` and logs every target live — for each endpoint/parameter: the attack
classes, the exact `hunt-*` skill(s) to open, and a ready-to-run curl. Example line:
```
map:   redirect_to   https://t/wp-login.php?redirect_to=FUZZ   ssrf→hunt-ssrf  lfi→hunt-lfi  open-redirect→hunt-open-redirect
```

## Run
```bash
# DEFAULT — deterministic map only ($0, no agents). Stops for you with arsenal.md:
python3 engine/engine.py --scope my-engagement.json
#   scope file = {name, in_scope, out_of_scope, seeds}; output in ~/.bughunter-engagements/<name>/

# OPT-IN — auto-test the mapped surface with agents (read-only, curl-first, parallel):
cp engine/burp-mcp.json.example engine/burp-mcp.json   # only if you want Burp for OOB/blind
python3 engine/engine.py --scope my-engagement.json --hunt --parallel 3 --max-hunts 12
python3 engine/engine.py --scope my-engagement.json --hunt --allow-intrusive   # permit state-changing PoCs (off by default)

# dry-run the whole wiring with canned output (no agents, no budget):
python3 engine/engine.py --scope engine/engagement.example.json --base /tmp/eng --mock --hunt

# standalone recon (deterministic, no engine state):
python3 engine/recon.py https://target/ target.com
```
Key flags: `--hunt` (opt into agents) · `--allow-intrusive` (default OFF = read-only) ·
`--parallel N` (concurrent agents, default 3) · `--phases a,b,c` (explicit override).

## Hackbot harness (`harness.py`): code-driven subagent pipeline

`harness.py` runs the nine `agents/*.md` subagents as a pipeline. Deterministic code owns control flow, scope, coverage and evidence. Each judgment step is one headless `claude -p` subagent. This is the pattern the Critical Thinking podcast hackbots converged on; see [`docs/ctbb-claude-playbook.md`](../docs/ctbb-claude-playbook.md).

```
recon + rank (this engine, deterministic) → recon-ranker → threat-modeler → hunter ×N
  → reflector → escalator → validator → poc-builder → video → report-writer → skill-distiller
```

```bash
python3 engine/harness.py --scope my-engagement.json --proxy http://127.0.0.1:8080        # paranoid (default)
python3 engine/harness.py --scope my-engagement.json --checkpoint normal --parallel 4
python3 engine/harness.py --scope my-engagement.json --status        # phase, coverage, findings by stage
python3 engine/harness.py --scope my-engagement.json --watch         # tmux: live log + status panes
python3 engine/harness.py --scope engine/engagement.example.json --mock --base /tmp/hb   # offline dry run
python3 engine/harness.py --scope my-engagement.json --from video --until video --only F02   # re-film one finding
```

### Enforced in code, not in prompts

| Gate | How |
|---|---|
| **Scope** | Every agent runs with a generated `--settings` that installs `hooks/scope_guard.py` as a `PreToolUse` hook on `Bash` and `WebFetch`. The hook blocks (exit 2) three things: requests to hosts outside the scope JSON (deny wins), `PUT`/`PATCH`/`DELETE` unless `--allow-intrusive` is set, and recursive deletes outside the engagement folder. The proxy host is always allowed. |
| **Evidence** | Agents must save each request to `evidence/requests/<op-id>.txt`. A candidate whose op IDs aren't in the store is rejected **before** any LLM judges it (`evidence.py`). The check runs again before validation. Off-scope hosts that appear in the evidence are flagged. |
| **Coverage** | Each hunter claims explicit rows. Each row must come back `tested-negative` or `candidate` with real op IDs, or `blocked:<reason>`. Uncovered rows are re-dispatched with a scope-restating "keep going" nudge, up to `--max-nudges` times. Rows still uncovered after that, and blocked rows, are surfaced at the checkpoint. They are never counted as tested. |
| **Structure** | Each agent's prompt is loaded from `agents/<name>.md` through `--agents`/`--agent`, so no install is needed and the prompt matches this checkout. Each agent returns schema-shaped JSON, enforced with `--json-schema`. Prose answers get one repair call: the harness resumes the same session and asks for the JSON only. |
| **PoC video** | The `video` phase asks the poc-builder for a storyboard: captioned steps in the target's own UI. `video/record.js` (Node + Playwright) then films it deterministically. Off-scope browser requests are aborted in code, and PUT/PATCH/DELETE are blocked in read-only runs. The video ends with a typed-out replay of the PoC run output and a summary card, so API-only bugs get one too. The result is converted to MP4 with ffmpeg and passed to the report-writer. Skip it with `--no-video`; point it at a Playwright install with `HARNESS_PLAYWRIGHT_NODE_PATH`. |
| **Checkpoints** | The run stops for review after the phases set by the mode: `paranoid` stops after threat-model, reflect and validate; `normal` after validate; `yolo` never. It also stops whenever it needs you: the threat-modeler's account-setup question, or `findings/<id>.summary.md` for each report. Re-run the same command to resume from the saved phase. `--from <phase>` re-runs from a given phase. |

### What you get in the engagement folder (`~/.bughunter-engagements/<name>/`)
- `notes/CHECKPOINT.md`: why the run stopped, coverage numbers, blocked and uncovered rows, findings by stage, and a `claude --resume <id>` line for every agent session, so you can open any agent's conversation with full context.
- `notes.md`: Gadgets (chaining them is your job), Dead ends, Validator rejections, Hidden headers, and Questions for the human.
- `notes/app-book.md`, `findings/`, `pocs/`, `evidence/requests/`, `logs/<session>.json` (raw agent output), `.harness/settings.json` (the hook config).
- Confirmed findings are also written to the ledger (`memory.py`), so `/pickup`, `/remember` and `/memory-gc` see them.

Defaults:
- Agents load no skills unless you pass `--skills`; the measured gain is about 0, at a cost of roughly 12–15k tokens per agent.
- The model is your Claude Code default unless you pass `--model`.
- Reports are never submitted.

## Safety
- **Read-only by default** — hunt/validate agents are told: in-scope hosts only (never
  third-party, even to prove a finding), and no state-changing actions (no emails/writes/
  deletes/cache-purge, don't exercise exposed creds). `--allow-intrusive` lifts this.
- **Deterministic scope-audit** — every confirmed finding is checked for out-of-scope hosts in
  its PoC; any hit is flagged (⚠) in the report for review.
- **Calibrated severity** — the adversarial validator's severity (not the hunt's raw guess) is
  what's stored.

## Why this and not more skills
The measured result (`eval/`): skills add ~0 capability on benchmarkable tasks — the base model
already exploits standard classes, and skills cost ~12–15k tokens/agent to load. The leverage is
here: turning that raw capability into a **safe, deterministic, skill-routing, operator-in-the-
loop** engine. Breadth is free and visible; you bring the judgment.
