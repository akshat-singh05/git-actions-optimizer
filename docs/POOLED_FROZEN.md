# Frozen: the pooled failure model, and the fresh-data test that will judge it (frozen 2026-10-12T14:58:29Z)

**What is frozen.** `FailurePredictor(variant="v2_hgb")` (regularised boosted trees; settings in
`gha_optimizer/models/failure_predictor.py`) trained for each repository on that repository's own earlier runs **plus** the most
recent 5,000 labelled past rows of every other repository (`gha_optimizer/models/pooled_eval.py`, `OTHER_ROWS_CAP = 5000`, no
graph features, protocol v2 features `FAILURE_V2_COLUMNS`). No setting changes after this file is committed.

**Why a fresh test.** The idea was tried after the pre-registered v2 evaluation had been read
(`docs/IMPROVE_EVAL_TEST.md`); on the exploratory rolling-origin run (`docs/POOLED_EVAL.md`) it beat the baseline in 3 of 9
repositories (prometheus, pydantic, ruff) and lost in none, and scored higher than the same model trained on a repository alone in 6 of the 8
repositories where that could be scored (significantly only in ruff). Because the repositories were no longer unseen, that is a lead, not a result.

**The test.** Cutoff **2026-10-12T14:58:29Z**. Every run that starts after the cutoff did not exist when the model was fixed.

1. Ingest the new runs with bounded `--max-runs` and `--no-logs` for each of the nine repositories (workflows as in
   `docs/FROZEN_CHOICE.md`; ruff `ci.yaml`, uv `ci.yml`, pydantic `ci.yml`).
2. `gha-optimizer build-dataset --repo R` for each.
3. `gha-optimizer eval-pooled --repo ... (all nine) --after 2026-10-12T14:58:29Z --out docs/POOLED_EVAL_FRESH.md`.

**Pass / fail, stated in advance.** Applies only to repositories with **at least 20 failures in the fresh test set**
("judged"); the others are reported, not judged. Wait until at least two weeks of runs exist after the cutoff (about
2026-10-26), then run it once; if fewer than three repositories are judged by then, say so and extend the wait rather than lower
the bar.

* **CONFIRMED** if the pooled model beats the baseline (paired-bootstrap 95% interval of PR-AUC excludes 0) in **at least 3**
  judged repositories and is significantly worse than the baseline in none.
* **REJECTED** if it is significantly worse than the baseline in any judged repository, or beats it in fewer than 3 of at least 3
  judged ones.
* Otherwise **INCONCLUSIVE** (fewer than 3 judged repositories).

Results are recorded in this file under "Result", whichever way they fall.

## Result
Final run: not yet run (due on or after 2026-10-26).

### Interim run, 2026-10-14 (report: `docs/POOLED_EVAL_FRESH_INTERIM_2026-10-14.md`, run by `scripts/interim_verdicts.sh`)
Prepared for a review on 2026-10-16. Same procedure and cutoff (2026-10-12T14:58:29Z); only two days of fresh runs exist, so this is **not the verdict**.

| repo | fresh positives | judged (>= 20)? | pooled - baseline (PR-AUC) |
|---|---|---|---|
| prometheus | 43 | **yes** | +0.4601 [+0.2708, +0.6394] **win** |
| ruff | 125 | **yes** | +0.0198 [-0.0551, +0.1086] inconclusive |
| uv | 72 | **yes** | -0.0725 [-0.1761, +0.0283] inconclusive |
| dubbo | 8 | no | +0.0974 [-0.1159, +0.3867] |
| quarkus | 11 | no | -0.0050 [-0.0643, +0.1620] |
| pydantic | 2 | no | +0.1378 [+0.0174, +0.4634] |
| poetry | 1 | no | +0.6002 [+0.1912, +0.9474] |
| fastapi, cli/cli | - | no | no usable fold / one outcome class |

Three repositories can already be judged, with **1 win and 0 losses**. The rule requires at least 3 wins; **if the final run looked like this interim one, the
verdict would be REJECTED** (beats the baseline in fewer than 3 of at least 3 judged repositories). It is not recorded as the verdict because the rule asks for about two
weeks of runs, and more repositories may reach 20 failures by then; the rule and the bar are unchanged. The exploratory rolling-origin result (3 of 9) did not repeat on fresh data in ruff and uv.
