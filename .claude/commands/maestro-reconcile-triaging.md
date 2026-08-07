---
description: Reconcile a `triaging` maestro ticket — classify tier, route to approval or ready. (maestro self-dev)
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — triaging (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in the `triaging` phase. Take **exactly ONE** step toward its desired state, record
it only through the `maestro` CLI, then exit. The dispatcher re-spawns you next sweep, routing
to whichever phase file matches the ticket's phase at that time — this file only ever handles
`triaging`.

## Always: load state first
Resolve this ticket's bound repo — REPO/SLUG/BASE/PREFIX/MODE come from `maestro env --key`, which
can differ per ticket in a multi-repo home (single-repo homes fall back to the legacy
`repo_path`/`branch_prefix` config, so this is unchanged there) — plus HOME, which is board-wide
and comes from the key-less `maestro env`:
```bash
KEY="$1"
eval "$(maestro env --key "$KEY" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("REPO="+(d["repo_path"] or "")+"\nSLUG="+(d["slug"] or "")+"\nBASE="+d["base_branch"]+"\nPREFIX="+d["branch_prefix"]+"\nMODE="+d["mode"])')"
eval "$(maestro env | python3 -c 'import sys,json;print("HOME="+json.load(sys.stdin)["home"])')"
maestro observe-spec "$KEY"
maestro snapshot "$KEY"                     # -> phase, pr, ci, failure_count, open_questions
sed -n '1,200p' "$HOME/tickets/$KEY/spec.md"   # desired state (you never edit this)
cat "$HOME/derived/context/$KEY.md" 2>/dev/null   # folded log: verbatim Q&A, phase reasons,
                                                    # failures, CI history, recent impl steps,
                                                    # dependsOn phases — read this before acting,
                                                    # it saves re-deriving context from raw events
```
If the snapshot shows pending inbox commands, fold them before deciding:
`maestro fold-inbox "$KEY"`. Finish every exit path with `maestro release "$KEY"` (drop your claim).

## `triaging`: classify tier, then route
Read `approval_tier` from the spec's frontmatter:
- **tier 0** → auto-approve: `maestro set-phase "$KEY" ready --reason "tier-0 auto-approved"`
- **tier ≥1** → ask for pickup approval, then sleep:
  `maestro ask "$KEY" "Pick up $KEY — <one-line plan>. AC: <bulleted>. OK?"`

**Done when:** exactly one of the two `maestro` calls above has appended its event, and
`maestro release "$KEY"` has run — that is the whole step, nothing else to check.
