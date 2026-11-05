## Round: fresh — implement the spec

No PR exists yet. Implement the spec's Acceptance criteria:
1. Read the spec's Intent + AC and the relevant code. **Before editing a file, read the
   `AGENTS.md`/`CLAUDE.md` nearest each directory you edit** (walk up from that directory to the
   repo root, reading each one you find): not every runner loads a nested one on its own.
   **Scope check:** the brief's `allowed_paths` lists globs (`**` crosses directories) this diff
   may touch; empty means no restriction. If non-empty and the change needs a file outside it,
   ask and exit **before** editing there. Stay inside `allowed_paths` yourself:
   ```bash
   maestro ask "$KEY" "<files> are outside allowed_paths (<list>) — answer `approve`, `allow`, `allow: <glob>`, or say what to avoid. [files: <f1>, <f2>]" --qid "scope-$KEY-<n>"
   ```
2. Run the tests (core, "Tests are the proof"), then attest each AC (core, "Self-review gate").
3. Commit your work:
   ```bash
   git -C <WT> add -A
   git -C <WT> commit -q -m "$KEY: <subject>"
   ```
4. Measure the diff and act on `exceeds` before opening the first PR:
   ```bash
   maestro pr-size "$KEY"   # -> {lines_changed, threshold, exceeds, split_viable, tree}
   ```
   **`exceeds: false`, or `exceeds: true` with `split_viable: false`** (no balanced file-level
   split exists, so a proposal would only cost a human round-trip; threshold `0` disables the
   check) — open one PR without a `maestro ask`. You write the title and body (motivation and
   changes, then an AC-to-evidence table from the `verify-ac` calls or `maestro snapshot "$KEY"` ->
   `ac_verified`); the verb pushes, creates the draft PR or adopts the one that already exists,
   and records it once, so a re-run opens nothing twice:
   ```bash
   maestro open-pr "$KEY" --title "<DISPLAY_KEY>: <subject>" --body "<motivation/changes> ## AC-to-evidence

| AC | Evidence |
|----|----------|
| <ac 1 text> | <what/where/result 1> |
| <ac 2 text> | <what/where/result 2> |"
   maestro handoff "$KEY"
   ```
   **`exceeds: true` and `split_viable: true`** — do not push or open the PR yet. Split the diff
   into an ordered stack of smaller PRs by file: each entry names its files, the spec ACs it
   covers, and the entry it is based on (entry 1 on `<BASE>`; entry *n* on entry *n-1*). Every
   entry's line count must be at least ⅓ of the largest entry's — never propose a lopsided
   stack. `--qid` binds the proposal to this exact tree state, so an answered proposal passes
   straight through on a later pass and a new tree re-evaluates from scratch:
   ```bash
   maestro ask "$KEY" "$KEY: this diff is <lines_changed> lines changed (threshold <threshold>) -- proposing a stack instead of one PR -- 1) <files> (AC <n>) based on <BASE> | 2) <files> (AC <n>) based on <PREFIX>$KEY-1 | more entries as needed -- approve to open the stack, or say what to keep as one PR instead." --qid "split-$KEY-<tree>"
   ```
   Then exit (`maestro release "$KEY"`) without pushing. The dispatcher wakes `awaiting-human` on
   the answer and routes back here as a split-decision round.

Push normally — never force-push. Let hooks run — never skip them. Test the real behavior,
never a mock. Then exit; the next sweep spawns the independent `qa` reconciler — this session
never judges its own diff and does not poll CI.
