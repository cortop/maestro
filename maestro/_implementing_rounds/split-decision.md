## Round: split-decision — the PR-split proposal was answered

The reason starts `pr split decision:` and carries the verbatim answer. The code was already
implemented and the tests were green before you asked, so there is nothing new to commit and no
PR exists yet. Act on the answer: an approving answer means open the approved stack (below);
anything else means push and open the single PR (`maestro brief "$KEY" --round fresh` prints the
`exceeds: false` steps; `pr-size` is not re-run).

Each stack entry's PR is its own review unit: its title subject and body describe only that
entry's diff (its base...its branch), never the ticket-wide subject reused under a new `(n/N)`.
Its AC-to-evidence table lists only the ACs the approved proposal gave that entry; one clause
pointing at the neighbouring PRs is enough.

**Approved split.** You write each entry's title and body and name its last commit; the verb
does the rest. Find each entry's last commit, then pass the whole stack, root first (the last
entry sits at HEAD, so it takes no `sha`):
```bash
git -C <WT> log --oneline "origin/<BASE>..HEAD"   # find each entry's last commit
maestro open-pr "$KEY" --stack '[{"title":"<DISPLAY_KEY>: <entry 1 subject> (1/<N>)","body":"<entry 1 body>","sha":"<sha of entry 1 last commit>"},{"title":"<DISPLAY_KEY>: <entry 2 subject> (2/<N>)","body":"<entry 2 body>"}]'
maestro handoff "$KEY" --summary "<what you did, what you addressed or declined, and why>"
```
The verb labels branches `<PREFIX>$KEY-1`.. with the last entry at `<PREFIX>$KEY`, pushes and
opens them oldest-first (each based on the previous entry, `gt` doing the same on a gt-managed
stack), and records them root-first, so a crash-and-respawn never double-opens a PR.

Push normally — never force-push. Then exit; the next sweep spawns the independent `qa` reconciler.
