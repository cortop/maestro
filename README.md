# maestro

**A per-ticket reconciler over append-only event streams.** A project-agnostic,
concurrent successor to wave-based markdown orchestrators.

Where a wave orchestrator moves *every* ticket in lockstep through a single run held
under a global lock, maestro makes the **ticket** the unit of execution. Each ticket has
its own append-only event log; a cheap, level-triggered dispatcher fans out one
independent background reconciler per *due* ticket. Tickets move concurrently and
independently — ticket A can be implementing while B waits on your answer while C
re-checks CI — and **you can create or edit work at any time without ever colliding with
a running agent.**

## The one idea

> Deterministic plumbing in **Python**; intelligence in **Claude**. The `maestro` package
> owns everything that must be correct-by-construction — the fencing-gated event log,
> atomic writes, snapshot fold, idempotent step-ids, the dispatcher, leases, projections,
> dead-letter. The *thinking* (triage, implement, QA) lives in `claude --bg` sessions that
> drive state **only** through the `maestro` CLI, so an agent can never clobber a file or
> write a torn log.

## Why it's safe to edit while it runs

| Who | Writes only to | How |
|-----|----------------|-----|
| **You** | `tickets/<KEY>/spec.md` (edit) · `inbox/<KEY>.jsonl` (append via `maestro ans`) | human-owned files; append-only |
| **Agents** | `events/<KEY>.jsonl` (append, fencing-gated) · `derived/*` (atomic replace) | single-writer per key |

Human bytes and agent bytes never share a read-modify-write window, so the lost-update
bug that made v1 unsafe to touch **cannot occur**. Dashboards (`derived/WORKSTATE.md`,
`derived/NEEDS-YOU.md`) are generated projections you never hand-edit.

## How a ticket flows

```
triaging ──tier0──▶ ready ──slot+deps──▶ implementing ──QA+review PASS──▶ in-review ──merged──▶ done
   │                  ▲                       │                                              
 tier1/2              │                  stall/maxturns                                      
   ▼                  │                       ▼                                              
awaiting-human ─answer┘                   degraded (dead-letter; you decide)                 
```

`awaiting-human` and `awaiting-ci` are **sleeping** states — no process is held. The
reconciler records the gate and exits; the dispatcher re-wakes the ticket on a signal
(an inbox command, a requeue timer, or a spec edit) and a fresh reconciler resumes by
replaying the event log.

## Architecture

| Component | What | Runs as |
|-----------|------|---------|
| **dispatcher** (`maestro dispatch`) | level-triggered sweep: mint → find due → spawn → exit. The only fan-out point. No LLM. | launchd `StartInterval` (5 min) |
| **reconciler** (`/maestro-reconcile <KEY>`) | one idempotent step per ticket, then exit | `claude --bg` session (top-level, so it can spawn the Impl/QA subagents) |
| **maestro CLI** | correct-by-construction state verbs the agent calls | Python (this package) |
| **projector** | snapshots → dashboards, atomic | a phase of `maestro dispatch` |
| **providers** | Jira / GitHub / custom import, pluggable | `config.toml` |

## Quickstart

```bash
pip install -e .
maestro init                       # scaffold ~/.maestro + config.toml
maestro create                     # guided interactive flow (title → tier → priority → $EDITOR)
maestro create "Add retry to X" --tier 0  # flag-based (scripts / CI)
maestro dispatch --dry-run         # see what it WOULD spawn (no sessions launched)
maestro status                     # ticket counts by phase
maestro show T-1                   # snapshot + event log for one ticket
# go live:
maestro fleet up                   # launchd-pinned, auto-healing fleet (see daemon/README.md)
maestro fleet status               # is the dispatcher loaded? heartbeat age?
maestro fleet down                 # stop it
```

Answer questions it asks you:
```bash
maestro answer                     # interactive: walks through every open question
maestro answer T-1                 # scope to one ticket
maestro ans T-1 "yes, go ahead"    # non-interactive: answer by key+text directly
```

## Guarantees (all covered by tests)

- **Idempotent:** two reconcilers racing on the same log produce one event (content-derived step-ids).
- **Fenced:** a stale-tail append is rejected (optimistic concurrency).
- **Crash-safe:** a reconciler that dies mid-step is re-spawned and repeats as a no-op.
- **Independent:** distinct keys never share a lock; the cap bounds concurrency, not coupling.
- **Bounded reads:** a decision reads a ~1-2KB snapshot, never the history.

```bash
pip install -e ".[dev]" && pytest -q     # 22 passing
```

## Project-agnostic

Nothing about any tracker, repo, or workflow is hardcoded. Pick adapters by name in
`config.toml` (`tracker = "jira_cli"`, `vcs = "github_cli"`, `fetcher = "command"`), or
run pure-local with the `none` providers. Bring your own import command (e.g. an existing
fetch script) via the `command` fetcher.

## Status

Core engine + dispatcher + CLI + tests are complete and green. The reconciler skill
(`skills/maestro-reconcile.md`) is the integration point with your existing
Implementer↔QA implementation loop. See the design doc for the full migration path from a
wave orchestrator.
