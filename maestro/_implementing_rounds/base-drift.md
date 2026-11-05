## Round: base-drift — origin/<BASE> advanced

The drift policy rerouted a worktree that fell behind base. Sync as above, then:
- **PR open** (snapshot `pr_number` set): run the tests (core, "Tests are the proof"), push, and
  hand off. Keep the prior attestations and QA verdicts as-is — do not re-implement or route
  through `qa`. If the hand-off refuses, fix that per its error; `--force` does not override the
  QA gates.
  ```bash
  git -C <WT> push -q -u origin "<PREFIX>$KEY"
  maestro handoff "$KEY"
  ```
- **No PR yet**: after the sync continue with `maestro brief "$KEY" --round fresh`.
