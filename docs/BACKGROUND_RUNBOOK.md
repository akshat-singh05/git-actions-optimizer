# Background automation runbook

Set up on 2026-10-14 at the owner's request: keep the prospective live test running for days, and run the three pending
verdicts on their due dates, reporting each one to the owner when it happens. The procedures below are the single source of
truth; the scheduled tasks only point here.

| what | when | how | produces |
|---|---|---|---|
| Live scorers (uv, ruff, prometheus) | continuously until 2026-10-19 | `scripts/live_scorers.ps1` (12 h windows) restarted by the watchdog task | ledgers in `.gha_optimizer/live/` |
| Watchdog | 08:17 and 20:17 local time every day | Claude scheduled task `gha-live-test-watchdog` | restarts a scorer whose window ended; notifies the owner when it starts one |
| Final live evaluation | 2026-10-19 09:11 local | Claude scheduled task `gha-live-test-final` | `docs/LIVE_TEST.md`, `docs/live/*.md`, section D of `docs/IMPROVEMENT_RESULTS.md` |
| Frozen-choice verdict (logistic default) | 2026-10-21 09:13 local | Claude scheduled task `gha-frozen-choice-verdict` | `docs/MODEL_EVAL_FRESH_FINAL.md`, result in `docs/FROZEN_CHOICE.md` |
| Pooled-model verdict | 2026-10-26 09:17 local | Claude scheduled task `gha-pooled-verdict` | `docs/POOLED_EVAL_FRESH.md`, result in `docs/POOLED_FROZEN.md` |

**Limits, stated plainly.** Scheduled tasks run only while the Claude desktop app is open (a missed one runs when the app is next
opened). The scorers are ordinary processes: they run only while the computer is on and awake. Windows Smart App Control sometimes
blocks scikit-learn at import; never disable it - if a run is blocked, say so in the report and retry later. Live samples are small
and weekend traffic is low: the live test is a demonstration, not a significance test, however many days it runs.

## Rules every automated run follows

* Token only from `gh auth token`, only in the environment of the processes that need it; never written to a file, a log or a commit.
* Commit results locally with the repository's attribution line; **never push**; never edit `README.md` (the owner decides).
* Never change a pre-registered rule, threshold, default or frozen setting. A verdict is the pre-stated rule applied to the data,
  whatever it says; a rejection is reported as a rejection.
* Real data only; no synthetic values; ESTIMATED and MEASURED stay separate.
* When finished, tell the owner (push notification and the run's final message): what ran, the verdict, what was committed.

## Live scorers

```powershell
powershell -NoProfile -File scripts/live_scorers.ps1            # start the scorers that are not running (idempotent)
powershell -NoProfile -File scripts/live_scorers.ps1 -Stop      # stop them
```

Each scorer fits its models once at start (history up to that moment), then every 30 s lists queued and in-progress runs of its
workflow and writes a probability per job to its hash-chained ledger **before any job of the run completes**. A run first seen with
a completed job is recorded as `skipped`, not scored. Logs: `.gha_optimizer/live/logs/`.

## Final live evaluation (due 2026-10-19)

1. `$env:GITHUB_TOKEN = (gh auth token)`; use a scratch database so the main one is not touched:
   `gha-optimizer live-evaluate --db sqlite:///.gha_optimizer/live/eval_scratch.db --repo <R> --workflow-path <W> --ledger .gha_optimizer/live/<owner>__<repo>.jsonl --out docs/live/<owner>__<repo>.md`
   for `astral-sh/uv` (`.github/workflows/ci.yml`), `astral-sh/ruff` (`.github/workflows/ci.yaml`) and `prometheus/prometheus` (`.github/workflows/ci.yml`).
2. Rewrite `docs/LIVE_TEST.md`: a header with the evaluation time, the number of scored runs and scoreable runs per repository, an honest
   one-paragraph reading (which repositories could be scored, whether either model beat the baseline and with what interval, the sample-size
   caveat), then the three per-repository reports.
3. Update section D of `docs/IMPROVEMENT_RESULTS.md` with the final numbers; stop the scorers; disable the watchdog task.
4. Commit locally; notify the owner.

## Frozen-choice verdict (due 2026-10-21) - `docs/FROZEN_CHOICE.md`

1. Ingest the runs after the cutoff, bounded: `gha-optimizer ingest --repo <R> --workflow <file> --since 2026-10-07 --no-logs --max-runs <N>`
   for poetry (`tests.yaml`, 150), fastapi (`test.yml`, 150), prometheus (`ci.yml`, 600), dubbo (`build-and-test-pr.yml`, 100).
2. `gha-optimizer build-dataset --repo <R>` for each.
3. `gha-optimizer eval-models --repo python-poetry/poetry --repo fastapi/fastapi --repo prometheus/prometheus --repo apache/dubbo --after 2026-10-07T07:51:04Z --target failure --target failure_tree --target failure_regularized_tree --out docs/MODEL_EVAL_FRESH_FINAL.md`
4. Apply the rule written in `docs/FROZEN_CHOICE.md` ("Pass / fail, stated in advance"): only repositories with at least 20 fresh failures are judged;
   confirmed if `logistic - baseline` has an upper bound above 0 in every judged repository, rejected if significantly worse in any.
   Append a "Final run" subsection with the table and the verdict. If fewer than two repositories can be judged, say the verdict is still open.
5. Commit locally; notify the owner.

## Pooled-model verdict (due 2026-10-26) - `docs/POOLED_FROZEN.md`

1. Ingest runs after the cutoff `2026-10-12T14:58:29Z` with `--no-logs` and bounded `--max-runs` for the nine repositories of the test
   (workflows as in `docs/FROZEN_CHOICE.md`; ruff `ci.yaml`, uv `ci.yml`, pydantic `ci.yml`, cli/cli `go.yml`, quarkus `ci-actions-incremental.yml`).
2. `gha-optimizer build-dataset --repo <R>` for each.
3. `gha-optimizer eval-pooled --repo <all nine> --after 2026-10-12T14:58:29Z --out docs/POOLED_EVAL_FRESH.md`
4. Apply the pre-stated rule in `docs/POOLED_FROZEN.md` (confirmed / rejected / inconclusive, at least 20 fresh failures per judged repository,
   wait and say so if fewer than three repositories can be judged). Append the result to that file.
5. Commit locally; notify the owner.

## Removing the automation

Disable or delete the four scheduled tasks in the Claude app's scheduled-tasks list (`gha-live-test-watchdog`, `gha-live-test-final`,
`gha-frozen-choice-verdict`, `gha-pooled-verdict`), and stop the scorers with `scripts/live_scorers.ps1 -Stop`.
