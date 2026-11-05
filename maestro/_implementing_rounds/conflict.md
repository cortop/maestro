## Round: conflict — resolve and hand back

If a PR is already open (snapshot `pr_number` is set) and its Acceptance criteria are already
implemented, you are here **only to resolve the conflict**: sync as above, run the tests (core,
"Tests are the proof"), commit, push, and hand back. Keep the prior attestations and QA verdicts
as-is (the spec, and so their content hashes, didn't change) — do not re-implement the feature,
re-run `verify-ac`, or route the ticket through `qa`.
```bash
git -C <WT> push -q -u origin "<PREFIX>$KEY"
maestro handoff "$KEY"
```
`handoff` picks `awaiting-ci` for this round. If it refuses, fix that per its error; `--force` does not override the QA
gates. If you truly cannot reconcile the two intents yourself, escalate and exit:
`maestro ask "$KEY" "PR #<n> conflict I couldn't auto-resolve: <detail>" --qid "conflict-$KEY-<n>"`.
No PR yet (`pr_number` unset): after the sync continue with `maestro brief "$KEY" --round fresh`.
