# Optional: "keep going" Stop hook (scope-restating)

Justin Gardner keeps his hackbot running with a Stop hook that tells Claude to keep going whenever
it stops [CTBB Ep 175 00:31]. The failure mode is that "keep going" pushes the agent toward
**leaving scope** to find impact, so the nudge must restate the rules [Ep 175 00:32; Ep 181 ~00:41].

**This is not enabled by default.** Only use it for an unattended run on a target whose scope you
have already encoded in the engagement's `scope.md`, and check proxy history afterwards.

## Hook script — `~/.claude/hooks/keep-going.sh`

```bash
#!/usr/bin/env bash
# Stop hook: block a stop once and re-inject a scope-restating nudge (the next stop is allowed).
# Reads the hook JSON on stdin; exits 0 to allow stopping.
input="$(cat)"
# Don't loop forever: if we already nudged on this stop, let it stop.
if printf '%s' "$input" | grep -q '"stop_hook_active": *true'; then exit 0; fi
# Respect a kill switch in the engagement folder.
[ -f ./STOP ] && exit 0
cat <<'JSON'
{"decision":"block","reason":"Keep going if you are on a good lead or have untested coverage rows. Remember the rules: only hosts in scope.md, no write/delete to prove impact, no paid features, all traffic through the proxy. If every claimed row has a result and there are no open leads, write a summary to notes.md and stop."}
JSON
```

## Settings — `.claude/settings.json` in the engagement folder

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "bash ~/.claude/hooks/keep-going.sh" } ] }
    ]
  }
}
```

Stop the loop at any time with `touch STOP` in the engagement folder.
