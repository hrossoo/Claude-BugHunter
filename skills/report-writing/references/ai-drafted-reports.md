# AI-drafted reports: what triagers and top hunters expect

These notes are distilled from the Critical Thinking Bug Bounty podcast, Ep 170–199. Full citations are in `docs/ctbb-claude-playbook.md`.

## Process
1. **The human writes a 2–5 line summary first, in their own words.** The model then fits it into the platform template [Ep 183].
   - This keeps the report tied to what you actually understand.
   - Google has asked researchers to hand-write reports unless their AI writer is well tuned [Ep 170].
2. **Finish the PoC before you write.** "By the time you're reporting it, you should have an exploit that's fully automated" [Ep 183].
3. **Proofread every report** [Ep 196]. Inspect the evidence itself.
   - Example: a hackbot reported "other users' chats" that turned out to be a test string hardcoded in a JS file [Ep 198].
4. **After submitting, diff the AI draft against your final edited version.** Turn each edit you keep repeating into a rule for the writer [Ep 184].

## Structure rules
- **Steps to Reproduce, step 1:** the PoC one-liner, followed by a link to its saved run output. Don't end the report with "run this script" [Ep 192].
  - `curl -s https://<poc-host>/<dir>/poc.py | python3` (Unix)
  - `curl.exe -s https://<poc-host>/<dir>/poc.py | python` (Windows)
- **Naming:** use attacker and victim, never "user 1 / user 2". The PoC sends an `X-POC-Actor` header, so the triager can follow which actor sent each request through their proxy [Ep 192].
- **PoC as a test:** the PoC prints `VULNERABLE` / `NOT VULNERABLE` and cleans up after itself, so the vendor can reuse it as a retest [Ep 192].
- **Video:** a short video (≤2 min) is increasingly expected, especially when an API-level PoC crosses a UI boundary [Ep 170, 173, 179, 194].

## Style rules
- **Be concise and technically precise, with impact first.** Models default to long output; you may need to ask several times [Ep 170, 183, 184].
- **Write colloquially.** No emoji, no filler, no restating [Ep 184, 196].
- **Separate proven from possible.** Speculative impact goes in a clearly labelled sentence, or is left out.
- **Follow per-program rubrics.** Some programs want vulnerable code snippets and some don't. Apply length limits and VRP-specific quality bars [Ep 184].

## Needs More Info (NMI) replies
- Draft the reply with the model and send it yourself. It should:
  - answer exactly what was asked,
  - link the PoC output or video,
  - sound like you [Ep 187, 194].
