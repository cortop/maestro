---
description: Reconcile a `researching` maestro ticket — explore, cite primary sources, propose, then ask. (maestro self-dev)
argument-hint: <TICKET-KEY>
---

# maestro: reconcile `$1` — researching (self-development)

You are the reconciler for ticket **`$1`** of the maestro project, spawned because it is
currently in the `researching` phase. Take **exactly ONE** step toward its desired state,
record it only through the `maestro` CLI, then exit. The dispatcher re-spawns you next sweep,
routing to whichever phase file matches the ticket's phase at that time — this file only ever
handles `researching`.

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

## `researching`: explore, cite primary sources, propose
You are exploring to produce a research proposal. You are a top-level session, so you may use
the `Agent` tool to fan out exploration. Do not create a git worktree — this phase never edits
code.

1. **Explore the codebase** (Read/Grep/Glob/Agent) — understand relevant code, patterns, and
   constraints. Focus on the spec's Intent to know what to research. Anything discoverable this
   way belongs here, never in the question you ask at the end (rule (b) above).
2. **Follow every claim back to the source that owns it.** A claim about how a library, API, or
   protocol behaves is only as credible as the primary source that defines that behavior — its
   own docs, source code, RFC/spec, or changelog — not a blog post's secondhand summary of it.
   Use WebSearch/WebFetch or the `/deep-research` skill to locate those primary sources for
   state-of-the-art approaches and prior art; the proposal's Sources section (below) cites each
   one with a URL or `file:line`, not a summary of a summary. If web tools are unavailable in
   this session, say so explicitly and fall back to codebase-only research — a `file:line`
   citation into this repo is still a primary source.
3. **Write the proposal** at `$MHOME/tickets/$KEY/proposal.md`:
   ```markdown
   # Proposal: <title>

   ## Recommended
   <concise description of the best approach and rationale>

   ## Alternative 1
   <description of first alternative>

   ## Alternative 2
   <description of second alternative (add more as needed)>

   ## Sources
   - <file:line> — <why relevant>
   - <https://url-to-the-primary-source> — <why relevant>
   ```
4. **Record and ask** — the proposal-approval question plus any other genuinely open question
   (rule (b): nothing discoverable belongs here) that surfaced during research, all in ONE round
   (rule (a)). The approval question keeps its fixed `research-approval-<key>` qid — the
   `awaiting-human` reconciler routes on that prefix — any extra questions auto-derive theirs:
   ```bash
   PROP_PATH="tickets/$KEY/proposal.md"
   maestro append "$KEY" --type ResearchProposed \
     --payload "{\"proposal_path\":\"$PROP_PATH\",\"alternatives\":[\"Alternative 1\",\"Alternative 2\"]}" \
     --step-id "research-proposed-$KEY"
   maestro ask "$KEY" \
     --question "Proposal for $KEY is ready at $PROP_PATH. Approve the recommended approach, reply 'alternative N' to select an alternative, or 'needs more' to continue." \
       "Approve — <one-line why Recommended is the right pick>" "research-approval-$KEY"
     # add more --question "<text>" "<recommendation or \"\">" "" triples here for any other
     # genuinely open question that surfaced during research
   ```
   Then exit — the dispatcher re-wakes you when the human answers.

**Done when:** `$MHOME/tickets/$KEY/proposal.md` exists with a Recommended section, at least one
Alternative, and a Sources section citing primary sources; `ResearchProposed` has been appended;
`maestro ask` has recorded a `research-approval-$KEY` question (plus any other settled-frontier
questions in the same round); and `maestro release "$KEY"` has run.
