# Claude for Bug Bounty: lessons from the Critical Thinking podcast (Ep 170–199)

This playbook distils what the hosts and guests of the **Critical Thinking – Bug Bounty Podcast** said about using Claude (and LLM agents in general) for bug bounty. It covers the AI-relevant episodes from roughly May to November 2026:

170, 172, 173, 174, 175, 178, 179, 181, 182, 183, 184, 186, 187, 190, 191, 192, 194, 195, 196, 197, 198, 199.

The `agents/` folder in this repo puts it into practice.

> **Provenance and caveats.**
> - Transcripts come from the episode pages at criticalthinkingpodcast.io. They are auto-generated and were read through a summarizer, so quotes are near-verbatim, not exact.
> - Citations are `[Ep N hh:mm]`. Some episodes only have chapter-level timestamps.
> - **Model names, prices and "model X beats model Y" claims are the hosts' opinions at the time.** Models change monthly. Treat them as anecdotes, not configuration.
> - Speakers: Justin Gardner (Rhynorater), Joseph Thacker (rez0), Brandyn, Jonathan Dunn (JD, XSSDoctor), and guests including Ads Dawson (0xMoose), brutecat and Frans Rosen.

---

## 1. Core principles

1. **Code does the plumbing; the model does the judgment.**
   - Recon, endpoint extraction, auth injection, deduplication and coverage tracking belong in deterministic code. Spend model tokens only on the part that needs judgment. [Ep 178 ~00:43, Ep 197 00:06]
   - This repo's `engine/` already follows that design: deterministic first, with agents opt-in.

2. **Give narrow, deep tasks, not "find all vulns, make no mistakes".**
   - Vague tasks make agents "do much worse". [Ep 196 ~00:04]
   - Name the parameters and endpoints. [Ep 196 00:07]
   - Map each step of one auth flow rather than asking for "all bugs". [Ep 199 00:41]
   - Read on air: *"I want a deeper version of this task, not a bigger one."* [Ep 197 00:52]
   - Generic prompts produce generic duplicates, because everyone is running the same ones. [Ep 194 00:22, Ep 195 00:06, 00:33]

3. **Threat-model before you hunt.**
   - Spend about 20 minutes giving the model JS, docs and your account setup before expecting results. [Ep 194 00:43]
   - JD has the AI write a "book" about the app and reads it himself, then has it *rewrite* the book after bugs are found. [Ep 181 ~00:09–00:12]
   - Understanding the architecture is "massive". [Ep 199 00:42]

4. **Seed the model with what others don't have.**
   - Use your past reports and your notes on the program. [Ep 187 00:08]
   - Ask for "a bug of equal level" to one of your previous big finds. [Ep 186 00:39]
   - Feed it old reports and ask for bypasses. [Ep 195 00:31–00:32]
   - Give it payload cheat sheets: a markdown-image cheat sheet found a working path in about 10 minutes. [Ep 191 00:03]

5. **Proxy history is ground truth.**
   - Route every agent request through Caido or Burp.
   - Agents have reported "no bugs" when nothing had actually been sent. [Ep 196 00:09]

6. **Enforce coverage in code, not by asking politely.**
   - Track a sitemap or worklist, and don't let the agent finish until every endpoint has been tested. [Ep 178 ~00:44, Ep 182 00:25]
   - Agents *claim* rows and only *append* results. [Ep 197 01:21]
   - Small models quit early unless forced to go line by line. [Ep 197 00:46]

7. **Gadget → lead → finding.**
   - A primitive is not a bug. Log gadgets for chaining. [Ep 175 00:29–00:31]
   - Have a judge decide whether something is a lead or a finding. [Ep 184 01:01, Ep 197 00:57]
   - A reverse shell in a sandbox is "just a gadget". [Ep 198 00:09]

8. **Keep the human in the loop for understanding.**
   - *"You can outsource your thinking, but you cannot outsource your understanding."* [Ep 173 ~00:34]
   - *"Me and Opus are still better at a lot of things together than Opus is alone."* [Ep 181 ~00:51]
   - People remember bugs found by agents less well. [Ep 170 00:08]
   - Brandyn escalated one bug four times only after exploring the app himself. [Ep 179 00:15]

9. **Push back when you have a reason to.**
   - Insisting "there's definitely a bug here" eventually produced real crits. [Ep 186 00:40, Ep 187 00:19]
   - It costs tokens, so use it on exclusive or deep scope.
   - When an agent says something is impossible, have it write up *why*, then give that write-up to a fresh agent to critique. [Ep 199 00:45]
   - Claude repeatedly calling an exploit "impossible" cost JD two weeks. [Ep 199 00:38]

## 2. What AI is good and bad at (per the hosts)

| Good at | Bad at |
|---|---|
| Source and code review; joining a sink to a missing check [Ep 184 00:14, Ep 198 00:47] | **Chaining primitives** [Ep 178 ~01:08, Ep 172, Ep 183 00:34] |
| Breadth over unauthenticated surface; rare-tech checks across many hosts [Ep 181] | Race conditions, even with source available [Ep 195 00:41] |
| IDOR and enumeration across large APIs; GraphQL per-operation auth gaps ("hit every query and mutation") [Ep 178, Ep 182 00:18] | Blind or delayed effects, blind XSS [Ep 195 00:43–00:44] |
| Parser differentials and desyncs; Unicode in JWT flows [Ep 186, Ep 195 00:37] | Authenticated multi-role logic [Ep 181 ~00:46]; WebSocket logic [Ep 195 00:55] |
| Hidden headers in JS (e.g. bypassing persisted GraphQL queries) [Ep 195 00:45, Ep 198 01:16] | SSRF without program context: it defaults to probing IMDS [Ep 195 00:43] |
| Crypto checks (length extension, bit flipping, webhook signatures) [Ep 170 00:31] | Telling rate limits from dead ends ("spidey sense") [Ep 181 ~00:21] |
| Firmware and log triage, binaries via a Ghidra MCP [Ep 172, Ep 198 01:06, Ep 199 00:40] | Giving up too early on hardware root paths [Ep 199 00:44] |
| Hacking AI features [Ep 183 00:09] | Knowing when something is *really* impact (overclaims SSRF and ATO) [Ep 197 00:57–00:59] |

Workaround for chaining: give the model one goal at a time and do the joins yourself. [Ep 172, Ep 183 00:34]

## 3. Safety rules learned from hackbot incidents

These come from the Halloween episode and others [Ep 198 01:16–01:23, Ep 181, Ep 178, Ep 175, Ep 197]. Every agent in `agents/` repeats them.

- **Never write or delete data to "prove impact".**
  - An agent deleted about 10M log records it believed were a production financial database, intending to recreate them.
  - Another agent overwrote a website's bucket-hosted index page.
  - Report the *capability* to write, and don't use it.
- **Verify that an asset is actually owned by the target, not just named like it.** One RCE landed on an unrelated scam site that used the brand name as a "subdomain".
- **Check scope on every host.**
  - *"Claude will just go out of scope to get impact."* [Ep 175 00:28]
  - Agents follow in-scope references to out-of-scope hosts. [Ep 181 ~00:41]
  - A "keep going" stop hook must restate the scope rules. [Ep 175 00:32]
- **Don't create billable resources or enable paid features.** One run produced about $3K in GCP charges, and billing limits only send notifications. [Ep 178 01:12]
- **Don't `rm -rf` anything you haven't read.** [Ep 181 ~00:13]
- **Check the program filter before overnight runs.** One bad filter sent agents at VDPs. [Ep 197 ~00:10]
- **Inspect the evidence.** "Other users' chats" turned out to be a test message hardcoded in a JS file, and the report was closed N/A. [Ep 198 01:18]
- **Don't give literal goals like "steal money" without guardrails.** [Ep 179 00:23]

## 4. Pipeline and per-agent guidelines

```
recon-ranker ─▶ threat-modeler ─▶ hunter × N (one bug class + explicit rows each, coverage-gated)
                                      │ candidate
                                      ▼
                                  reflector ──gadget──▶ notes.md ## Gadgets (human chains)
                                      │ finding         ▲ fresh-eyes review of "impossible"
                                      ▼
                                  escalator ─▶ validator (+ triage council) ─▶ poc-builder ─▶ report-writer
                                                    │ KILL reasons logged                       │ human edits/submits
                                                    ▼                                           ▼
                                                           skill-distiller (proposes CLAUDE.md / skill / ledger updates)
```

Each agent's full instructions are in `agents/<name>.md`. Summaries:

### recon-ranker: get the JS, build the worklist
- *"Your number one job is to get the JS files."* [Ep 182 00:00, 00:24]
- Collect bundles, source maps and lazy-loaded chunks. Extract every endpoint, GraphQL operation and hidden header.
- Put endpoints into a coverage queue and work through it. This produced a *second wave* of bugs after the agent had stalled. [Ep 182]
- Diff the JS between runs and re-hunt what changed. [Ep 179 00:19, Ep 184 01:04–01:05]
- Use deterministic tooling (`engine/recon.py`). The model only ranks.

### threat-modeler: write the app book
- Covers features, roles, trust boundaries, auth flow, API families, dev and debug surface, and ranked *narrow* hypotheses. [Ep 181, Ep 194]
- Asks the human about the account setup.
- For custom protocols, lists where the implementation deviates from the spec. [Ep 199 00:46]

### hunter: one bug class, explicit rows, no early exit
- Per-class agents (auth agent, XSS agent, offline source agent, and so on), each with full recon context. [Ep 197 00:49]
- Auth is injected by tooling, not by the model:
  - Letting the model handle auth raises hallucination. [Ep 178 ~00:16]
  - JD found 80% of tokens went on logins and CAPTCHAs. [Ep 181 ~00:37]
- Tell it to look for guest or partial auth and unexpected `Set-Cookie` values. [Ep 182 00:14–00:18]
- Every request gets an operation ID and is saved to `evidence/requests/<op-id>.txt`, so the validator has a concrete store to check. [Ep 178 ~00:21]
- Interactive shells run in a tmux pane driven with `send-keys`. [Ep 170 00:07, Ep 182 00:37]
- Can't finish while claimed rows are untested. [Ep 178]
- Load a `hunt-*` skill only for niche or framework classes. This repo's `eval/` measured roughly zero gain on standard classes, at a cost of about 12–15k tokens per agent.

### reflector: phone a friend
- The hunter calls an *assess confidence* step. A stronger reflector walks through the CIA triad and labels the result gadget, lead or report-worthy finding. [Ep 184 00:34, 01:01–01:03]
- It reads the history of what was already tried, so it can reject paths other agents already failed on.
- **Fresh-eyes mode:** critique a hunter's "impossible" summary. [Ep 199 00:45]

### escalator: push severity before validation
- Runs between finding and validator and tries low→med→high. Escalations are logged on the *same* finding. [Ep 181 ~00:46]
- Without it, escalations got filed as new reports and killed as dupes. [Ep 182 00:21]

### validator: skeptical triager plus triage council
- *"Replicate this in a skeptical way as if you were a triager for the program."* [Ep 181 ~00:46]
- Evidence without an operation ID is rejected. That rule cut about 90% of noise. [Ep 178 ~00:21]
- Impact-focused: a null-byte trick that turns 403 into 401 with no data is not a bug. [Ep 182 00:19]
- Class proof bars [Ep 197 00:57–00:59, Ep 199 00:18]:
  - SSRF must hit an *internal* service.
  - XSS must execute (set and read a window property).
  - ATO must act as the victim.
  - Auth: test every role permutation.
  - RCE: the callback actually arrives.
- Adversarial "triage council" personas try to reject each finding. [Ep 197 00:55]
- Calibration matters:
  - Justin's validator was "a little bit too much of a hard ass". [Ep 175 00:28]
  - Strict validators get roughly 80–90% signal but drop real bugs. [Ep 182 00:21]
  - Log every rejection reason.

### poc-builder: the PoC is a unit test (Ep 192)
- **Two formats:**
  - A standard-library Python script run as `curl -s <url> | python3`, with Unix and Windows commands.
  - A self-narrating HTML page with `?autorun=1` and IDs passed as query parameters.
- **Script requirements:**
  - Proxy is mandatory: read from an environment variable, or prompt for one. TLS verification off.
  - Every request sends `X-POC-Actor: attacker|victim`.
  - Credentials are constants at the top of the script.
  - Objects are created dynamically.
  - Order: setup → baseline (the attacker is denied) → exploit → proof → cleanup.
  - Prints a `VULNERABLE` / `NOT VULNERABLE` verdict, so it doubles as a retest.
- **Run it exactly as the triager will.** Save the output to a txt file. Take screenshots for client-side bugs.
- Common failures: scripts that only print and never send requests, third-party dependencies, and "user 1" naming.
- Video PoCs are now expected. [Ep 170 00:16, Ep 173 00:45, Ep 179 00:28, Ep 194 00:08] In the harness, the `video` phase films one automatically from a poc-builder storyboard (`engine/video/record.js`).

### report-writer: short, human, PoC-first
- The human writes a short summary first, and the model fits it to the platform format. [Ep 183 00:02]
- The PoC command goes at the *top* of Steps to Reproduce. [Ep 192]
- Ask for concise output repeatedly if needed: Justin asked "like 15 times". [Ep 170 00:15, Ep 184 00:39]
- Write colloquially, with no emoji. [Ep 196 ~00:39, Ep 184 00:38]
- Use a per-program rubric with a regenerate loop. [Ep 184 00:41–00:42]
- Has an NMI-reply mode for "Needs More Info" requests. [Ep 187 00:24]
- Google asked researchers to hand-write reports unless their AI writer is well tuned. [Ep 170 00:14]

### skill-distiller: close the loop
- **Researcher-builder loop:** every capability you solve becomes a skill, so the next run starts from a higher baseline. [Ep 184 00:30]
- Anything the model tried many times before it worked becomes an insight, recorded both as a specific tip and as a meta lesson. [Ep 184 00:58]
- Diff the AI's report draft against your final edited version and feed the differences back. [Ep 184 00:41]
- Have the model analyse its own logs for efficiencies. Joseph adopted 4 of the 11 it proposed. [Ep 187 00:28]
- Check front-matter validity: skills silently failed to fire. [Ep 174 ~00:25, Ep 184 00:57]

## 5. Harness and operations

**Long-running sessions**
- Run hunts in tmux.
- Claude Code `--rc` (remote control) lets you steer from your phone, which Justin calls "a total lifesaver". [Ep 175 00:18, Ep 183 00:00]
- Justin drives the Claude Code TUI through a PTY, rather than `-p`, so `--rc` keeps working. [Ep 175 00:18–00:20]
- **Keep-going stop hook:** a stop hook that re-prompts "keep going" must also say *"if you're on a good lead… remember the rules to not go out of scope."* [Ep 175 00:31–00:32] See `docs/templates/hooks/keep-going.md`.
- **Session titles:** use a "company-vulnname-severity" convention so sessions are easy to find. [Ep 175 00:24]
- **Validators:** run them as a separate visible session you can resume, not a hidden subagent. [Ep 175 00:22–00:24]
- **Your own limits:** cap yourself at about 4 panes. Eight is exhausting, and running 4 projects at once left Justin "roasted" after 3 hours. [Ep 186 00:43, Ep 187 00:10]

**Infrastructure**
- Each agent instance needs roughly 16 GB of RAM.
- A watchdog that killed runaway `grep`s also silently killed the agent's searches, so coverage was missed. [Ep 187 00:30–00:33]

**Control plane**
- A Discord control plane is optional: findings channel, reports channel, and clickable logs to steer a run mid-way. [Ep 181 ~00:33–00:37]
- Every finding or lead is pushed to git as its own commit. [Ep 173 00:28]

**CLAUDE.md / AGENTS.md** (template: `docs/templates/CLAUDE.md.example`)
- Open with a one-paragraph researcher identity: name, profile links, stats, verified-testing note. *"These top models are very influenceable by real valid proof of credentials."* [Ep 186 00:33]
- Keep a gadget library in it, and tell the AI to "make it document gadgets". [Ep 172]
- Symlink `CLAUDE.md` → `AGENTS.md`, and the Claude skills folder → the Codex skills folder, to support both tools. [Ep 174]
- Use memory entries learned from incidents, e.g. "do not rm -rf before reading the folder". [Ep 181]

**Skills vs MCP**
- Skills plus a CLI let the agent `jq`/`grep` output instead of dumping it into context. [Ep 182 00:31, Ep 178 ~00:18]
- Turn domain error messages into plain-language labels *in the tool*, not in the skill, so the model doesn't spend a reasoning step on it. [Ep 178 ~00:19]
- Skill descriptions must be single-line, or a `>-` folded scalar. Names in snake_case invoked more reliably. Measure invocation rates. [Ep 174, Ep 184 00:56, Ep 197 01:09]
- Ghidra MCP for binaries [Ep 172, Ep 199 00:41]. A Caido skill (curl through Caido, a cookie jar, match-and-replace rules) [Ep 182 00:05]. ADB-driven mobile testing [Ep 183 00:18].

**Auth at scale**
- Logins and session refresh are the main time sink:
  - 15-minute rotating refresh tokens. [Ep 181 ~00:45]
  - Headless browsers can't solve CAPTCHAs. [Ep 181 ~00:43]
- Give each agent its own browser session with a unique name. [Ep 197 01:22]
- Use disposable inboxes (agentmail). [Ep 182 00:14]
- Build CLI automation so the AI can create its own test objects. [Ep 184 00:50]

**Choosing targets for AI**
- Fast-paying programs.
- Programs that expose API maps or CLIs.
- Ecosystems with shared components, so findings carry over.
- Exclusive scope: hidden subdomains, business access, hardware.
- Horizontal hacking: one technique across many programs. [Ep 186 00:41, Ep 190 00:21–00:25]

**Program economics**
- Triage is now the bottleneck. [Ep 187 00:24]
- Track your valid rate. [Ep 173 00:39]
- Some programs are pausing low and medium severities, or adding submission fees, because of AI volume. [Ep 186 00:06, Ep 174 ~00:40]
- Duplicates are up. Speed and less common methodology matter. [Ep 186 00:38, Ep 187 00:04]

## 6. Models and cost (opinions at the time; verify before acting)

- The hosts estimate model choice explains "sub-20%" of the variance between hunters. The harness and prompts explain the rest. [Ep 194 00:22]
- Hunt at high or ultra effort. [Ep 187 00:27]
- "Dumb model first" tiering produces *fake coverage*, because the weak model claims tests it didn't do. [Ep 191 00:19]
- Small or cheap models do work well as rankers and routers, and for a first pass over huge files that escalates to a bigger model. [Ep 194 00:12–00:17, Ep 197 00:42]
- Different models find different things. Mixing models across subagents surfaced new avenues. [Ep 197 01:06]
  - Claude was preferred for write-ups. [Ep 183 00:05]
  - Codex/GPT was described as more cautious about impact, and Claude as more willing to leave scope. [Ep 175 00:27–00:28]
- Keep a fallback provider or a local model for outages, and for clients who forbid external AI. [Ep 191 00:18, 00:24; Ep 196 00:20]
- Track "bounties per subscription". [Ep 184 00:29]
  - One $6K bug paid for about 6 months of compute. [Ep 187 00:34]
- Subsidized subscriptions may not last. [Ep 184 00:24, Ep 194 00:06]

## 7. Run it as a harness

`engine/harness.py` is the code-driven version of section 4, following the podcast hackbots' approach:
- **Code owns** control flow, scope (a `PreToolUse` hook), coverage (the claimed-row gate with keep-going nudges) and evidence (the op-ID store).
- **The model** is used only for judgment steps. Each one runs as a headless subagent with schema-checked JSON output and a resumable session ID.
- **Checkpoints** stop for you at review points. Re-running the same command resumes the pipeline from the saved phase.

Details are in [`engine/README.md`](../engine/README.md#hackbot-harness-harnesspy-code-driven-subagent-pipeline).

```bash
python3 engine/harness.py --scope my-engagement.json --proxy http://127.0.0.1:8080
```

## 8. Quick-start checklist

1. Scaffold the engagement with `hunt <target>`. Fill in `scope.md`, then add your researcher identity to `CLAUDE.md` using the template.
2. Point Claude Code at Caido or Burp, and confirm that proxy history shows its requests.
3. Get auth working once: one session per role, injected by tooling.
4. Run `/autopilot <target> --paranoid`. It runs recon-ranker → threat-modeler; read the app book.
5. Give hunters *narrow* tasks taken from the book's hypotheses, and let the coverage gate run.
6. Review the gadgets yourself, since chaining is your job.
7. Each finding goes reflector → escalator → validator → poc-builder → report-writer. You write the summary and submit.
8. Paste your final report back in, then run skill-distiller, then `/remember`.
