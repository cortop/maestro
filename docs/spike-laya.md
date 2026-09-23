# Spike: Laya as a decision/ranking model for maestro

**Date:** 2026-09-22
**Ticket:** none (interactive research session); follow-up tickets T-121..T-125
**Verdict:** No-go for Laya. Every job it could do for maestro is done better today by a
rule, a mention-match, or the existing Claude spawn, and the board has no labeled history
to fine-tune it on. The measurements do point at four model-free wins, filed as tickets.

Published report (same content, with tables rendered): https://claude.ai/artifact/NTiQ5r9NubNWgb9frLV9NG

---

## Question

Can [convaiinnovations/laya](https://huggingface.co/convaiinnovations/laya) — a 421M,
non-generative "System 1" decision model (JSON state + typed `choice`/`score`/`noul`
questions → label + calibrated probability, one ModernBERT-large forward pass) — accelerate
maestro's implementation steps (e.g. finding the files a ticket touches) or its routing
decisions?

Measured against the real dogfood board (`~/.maestro/maestro-dev`: 179 tickets, 12,187
events, 106 surviving session transcripts) with Laya 0.3.5 running locally (M5 Max, MPS).

## 1. What Laya is, verified from source

- **Context is tiny for spec-shaped input.** 512 tokens total on the English checkpoint
  (1,024 on the others; overridable up to 8k via `agent.cfg`). All options share a
  192-token head budget; each option is capped at 48 tokens and squeezed to as few as 4
  when there are many; the state is silently right-truncated. Median maestro spec ≈ 1,250
  tokens → 99.4% of specs are cut at the default, 31% still at 2,048.
- **Zero-shot is below majority-class on the authors' own benchmark** (0.362 vs 0.461).
  README: "a fast base to specialise, not a zero-shot decision engine." The fine-tuned
  0.766 measures agreement with an unnamed ~4B-class LLM teacher (self-agreement 0.735).
- **Calibration ships broken.** Temperatures are per (type, option-count) bucket; the
  `choice:11+` bucket is a placeholder every load clamps with a `RuntimeWarning`; the
  multilingual checkpoint ships none; no fit helper is in the package; README ECE 0.466
  before refit. Fitting refuses buckets with < 25 samples.
- **Known defects reproduced.** `noul` hardcodes `false:`/`true:` option labels (issue
  #156, partially confirmed in `common.render_options`); answers flip with option order
  in 65–76% of cases on a catalog task (issue #171) and in 21–86% of cases here.
- **Runtime shape is wrong for the core.** torch 2.14 + transformers 5.17; 804 MiB bf16
  weights upcast to ~1.7 GB fp32 (2.1 GB RSS); 22–42 s load per process (95% of it
  random-initialising the encoder before overwriting it); a network `repo_info` call on
  every construction unless `HF_HUB_OFFLINE=1`; no revision pin; `print()`s to stdout on
  device fallback; rewrites `tokenizer_config.json` inside the HF cache. The dispatcher is
  a fresh process every 60 s that lives ~1.7 s, so anything with a 20 s load must be a
  sidecar behind an optional extra.
- **Fine-tuning is notebook-only** (2×T4 on Kaggle, full-model RLCD, 6,000 decisions,
  4 epochs, 372 updates). A rewritten single-device loop runs on MPS at 1.5–9.5 s per
  micro-batch of 8; nothing published says how accuracy scales with dataset size.

## 2. Where maestro actually spends (106 transcripts, T-110..T-120, all Sonnet, $30.06)

| Phase | Sessions | Median turns | Median $ | Total $ | Where the calls go |
|---|---:|---:|---:|---:|---|
| implementing | 27 (10 edit code) | 14 | 0.31 | 21.01 | discovery 52% of calls; pre-edit block 41% of calls (86% of it discovery); all discovery incl. post-edit re-reads = 46% of $ |
| qa | 28 (8 live) | 9 | 0.24 | 2.92 | qa-brief + targeted greps; 20 zero-cost API-error shells |
| triaging | 9 | 5 | 0.11 | 3.06 | discovery 72% (sub-agents confirming scope) |
| awaiting-human | 15 | 4 | 0.075 | 1.09 | 90% maestro verbs; 6 "no answer yet" wakes |
| ready | 9 | 7 | 0.09 | 0.81 | worktree ensure + set-phase |
| degraded/terminating | 18 | 4 | 0.06 | 1.17 | 16 identical checked+release loops on T-110 |

**The file-finding hypothesis, measured.** In the 10 implementing sessions that edited
code, the first read of a file that was eventually edited happens at a median tool call
5.5, after ~3 discovery calls. The remaining 20–50 pre-edit calls confirm and expand
context: `dispatcher.py`, `ops.py`, `cli.py`, `tui/app.py` are each re-read 2–6 times at
different offsets. Discovery precision is 0.50 (7 files read vs 6 edited; 0.83 excluding
the mandatory spec/context reads). T-116's spec named files and line numbers and needed 8
pre-edit calls; T-115's did not and needed 44.

**No-op sessions are 57% of sessions and 20% of dollars** ($5.94), but they are two
structural classes already closed deterministically (30 API-error shells from one outage,
16 degraded check-in loops). The one still open: 7 implementing sessions that started the
test suite in the background and exited to wait for it ($3.97, incl. one 72-turn $1.73
session), after which the watchdog dead-lettered the ticket.

## 3. Experiments on the real board

Laya 0.3.5, `max_len` 2,048 unless stated, `choice` questions with neutral labels (to
sidestep the `noul` defect). Baselines are stdlib-only.

### 3a. File relevance — which files will this ticket touch?

44 tickets sampled across history; truth = files the squash-merged commit changed;
candidates = ~200 repo files at the parent commit, content taken from the parent (no
leakage). Scored on pre-existing files (n = 42).

| Method | recall@5 | recall@10 | MRR | s/ticket |
|---|---:|---:|---:|---:|
| **MENTION** (spec names path, stem or symbol) | **0.483** | **0.581** | **0.817** | 0.36 |
| TF-IDF over parent-commit content | 0.406 | 0.552 | 0.750 | 0.03 |
| BM25 | 0.375 | 0.500 | 0.669 | 0.21 |
| git churn prior | 0.240 | 0.389 | 0.524 | 0.02 |
| Laya two-level choice (module, then file) | 0.173 | 0.217 | 0.484 | 2.3 |
| Laya choice over embedding shortlist (k=20) | 0.128 | 0.166 | 0.374 | 9.7 |
| Laya pairwise relevant/irrelevant per file | 0.099 | 0.160 | 0.258 | 69.9 |
| Laya mean-pooled encoder embeddings | 0.041 | 0.095 | 0.154 | 12.7 |

Laya timings were taken on a GPU shared with sibling runs (upper bounds); the 3–10×
ranking gap is the finding. Literature agrees: SWE-bench-Lite file Acc@5 is 62% for BM25,
85% for a purpose-trained 137M code embedder (CodeRankEmbed), 90–94% for agentic search;
BM25 recall@10 is 74% when the issue names the path and 8% when it does not.

### 3b. Spec triage (n = 178 specs)

| Question | Laya | Majority | Keyword | Order flips | Coverage @ conf ≥ 0.8 |
|---|---:|---:|---:|---:|---:|
| kind: implementation vs research | 0.978 | 0.983 | 0.983 | 2% | 0% |
| priority (score, 5 levels) | 0.494 | 0.500 | 0.433 | 86% | 0% |
| complexity vs files touched (1 / 2–4 / 5+) | 0.713 | 0.713 | 0.581 | 10% | 0% |
| needs a human question beyond pickup | 0.809 | 0.809 | 0.590 | 0% | 0% |
| built-in router preset: domain | 0.916 | 1.000 | 1.000 | 7% | 0% |

Results at 512 and 4,096 are within ±0.02. The labels themselves are near-constant (97%
implementation, 97% pickup approved): there is nothing to learn.

### 3c. Teacher-labeled classification (the distillation path)

A Claude agent hand-labeled three streams with rationales (reusable JSONL), then Laya was
scored zero-shot against them. The board has zero real review comments (0
`ReviewFeedbackReceived`; 0 GitHub reviews across 243 PRs), so that task used a proxy
corpus (QA-fail critiques, human `msg`s, a QA-pass sample).

| Task | n | Laya | Majority | Keyword | Order flips | Failure mode |
|---|---:|---:|---:|---:|---:|---|
| Review-comment routing (proxy) | 84 | 0.452 | 0.500 | 0.679 | 21% | "code change required" for 76/84 |
| CI failure triage (reconstructed excerpts) | 41 | 0.220 | 0.634 | 0.390 | 5% | constant "real failure"; 26/41 were flaky |
| Human-answer interpretation | 244 | 0.816 | 0.967 | 0.963 | 48% | labels 39 "ok"s as "approve with changes" |

Coverage at confidence ≥ 0.8 was 0% on all three. The CI stream also shows why a
classifier is the wrong first tool: `CiObserved` carries only check names, and 19 of 33
flaky episodes cost a $0.57 implementing spawn that a "re-run once per head" rule avoids.

### 3d. Session-outcome detection (Laya's "agent-trace observability" case)

Truth per session: did any non-dispatcher event land in its window? 106 sessions, 59
no-ops worth $8.64.

| Detector | Accuracy | Order flips |
|---|---:|---:|
| **Rule: no maestro write verb in transcript ⇒ no-op** | **0.990** | n/a |
| Laya over a structured digest (phase, turns, edits, verbs, last texts) | 0.867 | 46% (reversed order → 0.58) |
| Laya over the last 600 tokens of assistant text | 0.695 | 24% |
| Laya typed-decisions checkpoint, same digest | 0.438 | 10% |
| Majority class | 0.562 | |

80% of no-op dollars ($6.95) sit in eight "started pytest in the background and exited to
wait" sessions — a skill-text fix, not a detection problem.

### 3e. Latency and process model

| Measurement | Value |
|---|---:|
| `import laya` | 0.46 s |
| load, cold and warm (CPU-bound random init) | 21.6–21.8 s (up to 42 s under contention) |
| RSS after load | 2.1 GB |
| MPS, 1 question, 300–1,200-token state | 360–440 ms (contended) |
| MPS, 10–50 questions batched | 200–270 ms per question |
| CPU, 6 questions at max_len 2,048 | 1.8 s median |
| MPS vs CPU probabilities | max diff 0.0001 |
| dispatcher process lifetime | ~1.7 s alive per 62 s sweep |

Per-question latency is fine; the process model is not. The only viable shape would be an
optional extra plus a persistent unix-socket sidecar with one batched call per sweep.

## 4. Decision-point audit (42 points)

Five rated "strong" fit for a calibrated classifier, 14 "plausible", 15 must stay exact
(due-checking, spawn attempts, watchdogs, phase gates, spend brakes — the postmortems
require them enumerable and diagrammable). For every strong candidate the blocker is data
or volume, not model quality:

| Decision point | Today | Labels on the board | Cheapest fix |
|---|---|---|---|
| DP-06 route a human's answer | full Claude spawn per answer ($0.075, 8.5 s, one slot) | 244 answers, 82.4% literally ok/yes, 1 rejection | exact-literal allowlist in the dispatcher (T-122) |
| DP-09 route a PR review comment | any non-empty comment ⇒ implementing spawn ($0.57) | 0 events | regex/bot-author noise filter shipped dark (T-125) |
| DP-11 CI failing ⇒ implementing | always routes; 19/33 flaky | only check names captured | re-run once per head, capture a log tail (T-123) |
| DP-15 model/effort tier | config default; every ticket gets Sonnet | n=1 override | log spec features + outcome per spawn first |
| DP-37 which files to open first | the session's own greps | 178 tickets with merged-commit truth | mention + symbol index into the dossier (T-124) |
| DP-36 tracker import gating | all-or-nothing `auto_import` | no tracker configured | revisit when a tracker exists |

## 5. Recommendation

Three designs (minimal/ships-dark, Laya-maximalist, alternative stack) were scored by two
independent judges who re-derived every number read-only against the board. Both ranked
the minimal, model-free design first (31 and 34 of 40) and the Laya sidecar last (12 and
12): by its own arithmetic the Laya-attributable saving is ≈ $1 lifetime on the answer
residual, and every other consumer has no data to gate on. Filed as tickets:

1. **T-121 — implementing skill runs the suite in the foreground** with a timeout, never a
   background run plus poll loop. Attacks the largest open no-op class at zero risk.
2. **T-122 — exact-literal answer fast path in the dispatcher**, shipped dark, then shadow.
   Content-hash qids only (excludes `conflict-`, `research-approval-` and named park qids);
   fall through to today's spawn on anything else. ≈ 0.83 × 199 × $0.075 ≈ $12 lifetime;
   the real win is 8.5 s + a spawn-floor wait + a freed slot per approval.
3. **T-123 — CI re-run once per head before routing, capture a ≤ 2 KB failure tail.**
   Upper bound 19 × $0.57 ≈ $11 lifetime; also starts a labeled CI stream.
4. **T-124 — file hints + symbol map in `derived/context/<KEY>.md`**, gated by a replay eval
   over the merged tickets that must beat MENTION's 0.48 recall@5.
5. **T-125 — log decisions as events:** answer→route scorecard fold (the gate for T-122),
   review-noise config shipped dark, and an import-graph test asserting no `maestro.*`
   module pulls torch/transformers.

**When to revisit a small classifier, and which one.** Only when a stream has ≥ 25
examples per class per bucket and a rule leaves a measurable residual. Even then the
2025–26 survey favours SetFit (22–109M, trains in minutes, usable probabilities) or
GLiClass/GLiNER2 (single-pass zero-shot, ~130 ms CPU) for short-text routing, and
CodeRankEmbed or an Ollama-served Qwen3-Embedding-0.6B for the not-mentioned residual of
file ranking. Laya's distinguishing features (multi-question single pass, RL-trained
calibration) do not matter at a few hundred decisions per month; if they ever do, a
provider-agnostic `Decider`/`NullDecider` seam beside `NullTracker`/`NullVCS` is the right
shape, with Laya behind a socket sidecar, pinned by snapshot hash, consulted in shadow
mode first.

## 6. Method, verification, caveats

- **What ran.** Four agent workflows: a map of decision points and seams; an event-log
  miner (ten candidate datasets); a transcript parser attributing tokens/$ per tool call;
  a source read of the `laya` package; five experiments on the board; a fine-tuning read
  (notebook, dataset card, research scripts); an alternatives survey; three designs, two
  judges, and an adversarial refutation pass.
- **Verification status.** The 16 refuters were cut off by the account's session limit
  before returning verdicts. In their place both judges independently re-derived the
  load-bearing counts read-only (244 answers, 182 "ok" + 19 "yes", 186 `approved:` vs 1
  `rejected:` route, 37 CI-failing routes, 225 content-hash qids) and caught one error in
  the evidence brief, corrected here: literal approvals are 82.4% of answers, not 93%.
- **Caveats.** Session accounting rests on 106 transcripts from 11 tickets and one model;
  implementing medians are over 10 sessions with edits. The board's labels are
  near-constant, which limits every classifier, not only Laya. The review task used a
  proxy corpus. Laya timings were contended. File-relevance truth counts tests and docs as
  targets, which is why even MENTION sits far below SWE-bench numbers; the relative
  ordering is the finding. Dollar totals are small ($30 per two-week window); latency and
  freed concurrency slots are the larger practical wins.
- **Side finding.** CLAUDE.md states there is no `~/.maestro/maestro-dev` home, but
  `~/.zshrc` scopes `MAESTRO_HOME` to that path inside the repo, the launchd plist uses
  it, and it holds the populated board; the bare `~/.maestro` holds one event.
- **Artefacts.** Scripts, labeled JSONL, predictions and per-ticket ranks lived in the
  session scratchpad (not in the repo). The one worth keeping — the merged-commit file
  oracle (`git log main --format=%H%s`, `<KEY>: …` titles, `--name-only` at each parent) —
  is specified as `maestro locate --eval` in T-124.
