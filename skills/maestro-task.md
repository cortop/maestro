---
description: Create a new maestro ticket with quality gates — asks clarifying questions before writing anything.
allowed-tools: Bash, Read, Write
argument-hint: <raw description of what you want to build>
---

# maestro: create a ticket

You help the user create a well-formed maestro ticket. Your job is to **enforce a quality
bar** before any file is written: the description must be clear and self-contained, and the
ticket must carry at least one concrete, verifiable acceptance criterion.

The dogfood home is `~/.maestro/maestro-dev`.
Always run: `export MAESTRO_HOME=~/.maestro/maestro-dev` before any `maestro` command.

---

## Step 1 — evaluate the raw ask

The user's raw ask is: `$ARGS`

Apply the quality rubric:

1. **Clarity**: does it describe the problem/goal *and* what "done" looks like — not just
   restate a title?
2. **Acceptance criteria**: is there at least one concrete, observable, verifiable behaviour?
   ("works well" fails; "the CLI exits 0 and the file exists" passes.)
3. **Scope**: is this a single ticket? If it's clearly several independent pieces of work,
   flag it and suggest splitting.

**If ANY criterion fails**: do NOT create a ticket. Instead, ask the user 1–3 targeted
questions to fill the gaps — the whole settled frontier in one round, not one question
at a time: present them together as a short numbered list, each with your recommended
answer attached, so the user can just say "go with your recommendations" instead of
answering each individually. Before asking, check whether you could resolve a gap
yourself — by reading the codebase, existing tickets, or this project's docs — instead
of spending the user's time on it; only ask what genuinely needs their judgment. Wait
for their reply, then re-evaluate. Loop until the rubric passes or the user explicitly
says "cancel" / "abort".

**If ALL criteria pass**: proceed to Step 2.

---

## Step 2 — draft the spec

Synthesise what you know into a structured spec:

- **title**: a short imperative phrase (≤ 70 chars)
- **intent**: 2–5 sentences — what problem this solves and what "done" looks like
- **notes** (optional): relevant context, surface areas, constraints
- **acceptance criteria**: each one starts with `- [ ]` and is observable/verifiable. An AC
  MAY end with an opt-in, machine-checkable annotation (T-79): `(test: <path>)`,
  `(test: <path>::<id>)`, or `(check: <shell command>)` — when the repo binding has a
  resolved `test_command`, this makes that one AC provable by a subprocess (the added test
  must actually land in the branch's diff) instead of a self-attestation. `check:`'s command
  just needs to exit 0 — it does NOT verify the check was added by this branch, so prefer
  `test:` whenever the AC is "add a test for X".
- **priority**: default `2` unless the user said otherwise
- **dependsOn**: omit unless the user named a dependency

Show the drafted spec to the user and ask: **"Does this look right? Reply yes to create
it, or tell me what to change."** Do not proceed until they confirm.

---

## Step 3 — create the ticket

Once confirmed:

```bash
export MAESTRO_HOME=~/.maestro/maestro-dev

# Find the next available T-N key
KEY=$(python3 -c "
import os, re
home = os.path.expanduser('~/.maestro/maestro-dev/tickets')
nums = [int(m.group(1)) for d in os.listdir(home) for m in [re.match(r'^T-(\d+)$', d)] if m]
print('T-' + str(max(nums, default=0) + 1))
")

# Or use a key the user specified (if they said e.g. "use key MY-3")
# KEY="<user-specified>"

echo "Minting $KEY"

# Write the full spec.md (human-owned — safe to write directly)
mkdir -p "$MAESTRO_HOME/tickets/$KEY"
```

Then write the spec.md to `$MAESTRO_HOME/tickets/$KEY/spec.md` using the Write tool
with this exact format:

```
# <KEY>: <title>

<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->

priority: <priority>
<dependsOn line only if there are deps: dependsOn: [X-1, X-2]>

## Intent
<intent text>

## Notes
<notes text — omit this section entirely if there are no notes>

## Acceptance criteria
- [ ] <criterion 1>
- [ ] <criterion 2>
...
```

Then queue the ticket so the dispatcher sees it:

```bash
export MAESTRO_HOME=~/.maestro/maestro-dev
maestro create --key "$KEY" --priority <priority> --no-nudge "<title>"
```

Finally, report success:

```
Created ticket $KEY at ~/.maestro/maestro-dev/tickets/$KEY/spec.md
The dispatcher will pick it up on the next sweep.
```

---

## Rules

- Never create a ticket whose spec fails the rubric. Loop on questions until it passes
  or the user cancels.
- Never invent acceptance criteria — derive them from what the user said.
- Never add front-matter fields beyond `priority`, `dependsOn`.
- The `dependsOn` line must be omitted (not left as `dependsOn: []`) unless there are
  real dependencies.
- Target the dogfood home (`~/.maestro/maestro-dev`), not the default `~/.maestro`.
- After writing spec.md, always run `maestro create --key ... --no-nudge` so the
  dispatcher event-log picks up the ticket.
