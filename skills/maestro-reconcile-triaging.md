---
description: Reconcile a `triaging` maestro ticket — classify tier, route to approval or ready. (maestro self-dev)
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
`repo_path`/`branch_prefix` config, so this is unchanged there) — plus MHOME, which is board-wide
and comes from the key-less `maestro env`:
```bash
KEY="$1"
eval "$(maestro env --key "$KEY" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("REPO="+(d["repo_path"] or "")+"\nSLUG="+(d["slug"] or "")+"\nBASE="+d["base_branch"]+"\nPREFIX="+d["branch_prefix"]+"\nMODE="+d["mode"])')"
eval "$(maestro env | python3 -c 'import sys,json;print("MHOME="+json.load(sys.stdin)["home"])')"
maestro observe-spec "$KEY"
maestro snapshot "$KEY"                     # -> phase, pr, ci, failure_count, open_questions
sed -n '1,200p' "$MHOME/tickets/$KEY/spec.md"   # desired state (you never edit this)
cat "$MHOME/derived/context/$KEY.md" 2>/dev/null   # folded log: verbatim Q&A, phase reasons,
                                                    # failures, CI history, recent impl steps,
                                                    # dependsOn phases — read this before acting,
                                                    # it saves re-deriving context from raw events
```
If the snapshot shows pending inbox commands, fold them before deciding:
`maestro fold-inbox "$KEY"`. Finish every exit path with `maestro release "$KEY"` (drop your claim).

## Asking the human: frontier rounds, never one at a time
Two rules apply whenever you reach for `maestro ask`:

**(a) Ask the whole settled frontier in one round.** If you have more than one question whose
prerequisites are already met, post them together in a single `maestro ask` call via the
repeatable `--question TEXT RECOMMENDED QID` flag (one triple per question; pass `""` for
RECOMMENDED when you have no recommendation, and `""` for QID to auto-derive it — only pin an
explicit QID when a later step routes on its prefix, e.g. `research-approval-<key>`):
```bash
maestro ask "$KEY" \
  --question "<question 1>" "<your recommended answer, or \"\">" "" \
  --question "<question 2>" "<your recommended answer, or \"\">" ""
```
One question per round is the most expensive schedule available here: each round costs a
dispatcher wake, an hours-long human round-trip, and a full reconciler spawn — pay that once
per round, not once per question. A single settled question is still fine as one `--question`
(or the plain `maestro ask "$KEY" "<text>"` form).

**(b) Never ask something a sub-agent could find in the codebase.** Before asking the human
anything, check: is this greppable, readable from existing code/docs, or otherwise discoverable
without a judgment call? If so, dispatch an `Agent`-tool sub-agent to find it — do not spend a
human round-trip on it. Only put a question in the round if a sub-agent genuinely cannot resolve
it: a product/scope decision, an ambiguous intent, or an explicit approval gate.

## `triaging`: classify tier, then route
Read `approval_tier` from the spec's frontmatter:
- **tier 0** → auto-approve: `maestro set-phase "$KEY" ready --reason "tier-0 auto-approved"`
- **tier ≥1** → resolve anything discoverable yourself first (rule (b) above — dispatch a
  sub-agent rather than asking), then ask the whole settled frontier in one round (rule (a)):
  the pickup/plan approval question, plus any other genuinely open design questions, each
  numbered with your recommended answer:
  ```bash
  maestro ask "$KEY" \
    --question "Pick up $KEY — <one-line plan>. AC: <bulleted>. OK?" "<your recommendation>" "" \
    --question "<other settled question, if any>" "<your recommendation>" ""
  ```

**Done when:** exactly one of the two `maestro` calls above has appended its event, and
`maestro release "$KEY"` has run — that is the whole step, nothing else to check.
