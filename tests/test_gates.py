"""Safe relaxation of `needs` gates: every safety condition, the planner, and the validator."""
from __future__ import annotations

import pytest

from gha_optimizer.gates import (
    apply_relaxation,
    classify_gates,
    critical_path_seconds,
    plan_relaxation,
    relaxable_edges,
    validate_relaxation,
)
from gha_optimizer.ingestion.workflow_parser import parse_workflow_text

CI_SHAPE = """
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps: [{run: ruff check .}]
  test:
    needs: lint
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
  typecheck:
    needs: lint
    runs-on: ubuntu-latest
    steps: [{run: mypy .}]
"""


def decisions(text):
    return {(g.upstream, g.downstream): g for g in classify_gates(parse_workflow_text(text))}


def job_workflow(down_body: str, up_body: str = "steps: [{run: build}]") -> str:
    return f"""
name: W
on: [push]
jobs:
  up:
    runs-on: ubuntu-latest
    {up_body}
  down:
    needs: up
    runs-on: ubuntu-latest
    {down_body}
"""


def test_a_plain_check_gated_by_another_check_is_relaxable():
    d = decisions(CI_SHAPE)
    assert d[("lint", "test")].relaxable and d[("lint", "typecheck")].relaxable


@pytest.mark.parametrize("down_body,reason", [
    ("environment: production\n    steps: [{run: ./deploy.sh}]", "environment"),
    ("concurrency: {group: deploy}\n    steps: [{run: ls}]", "concurrency"),
    ("steps: [{run: ls, env: {T: '${{ secrets.NPM_TOKEN }}'}}]", "secret"),
    ("steps: [{uses: pypa/gh-action-pypi-publish@release/v1}]", "publish action"),
    ("steps: [{run: twine upload dist/*}]", "publish command"),
    ("steps: [{run: docker push img}]", "publish command"),
    ("steps: [{run: git push origin HEAD}]", "publish command"),
    ("steps: [{run: terraform apply -auto-approve}]", "publish command"),
    ("steps: [{run: ls}]\n    if: always()", "status"),
    ("steps: [{run: ls}]\n    if: ${{ needs.up.result == 'success' }}", "reads needs.up"),
    ("steps: [{run: 'echo ${{ needs.up.outputs.value }}'}]", "reads needs.up"),
    ("steps: [{run: 'echo ${{ toJSON(needs) }}'}]", "needs context"),
    ("steps: [{uses: actions/download-artifact@v4}]", "artifacts"),
])
def test_each_safety_condition_blocks_relaxation(down_body, reason):
    up = "steps: [{uses: actions/upload-artifact@v4}]" if "artifact" in down_body else "steps: [{run: build}]"
    g = decisions(job_workflow(down_body, up))[("up", "down")]
    assert not g.relaxable and any(reason in r for r in g.reasons), g.reasons


def test_github_token_alone_is_not_a_secret_and_dry_run_publish_is_safe():
    text = job_workflow("steps: [{run: 'cargo publish --dry-run', env: {GH_TOKEN: '${{ secrets.GITHUB_TOKEN }}'}}]")
    assert decisions(text)[("up", "down")].relaxable


def test_shared_cache_key_and_reusable_workflow_calls_are_kept():
    cache = "steps: [{uses: actions/cache@v4, with: {path: p, key: k1}}]"
    assert not decisions(job_workflow(cache, cache))[("up", "down")].relaxable
    reusable = """
name: W
on: [push]
jobs:
  up:
    runs-on: ubuntu-latest
    steps: [{run: build}]
  down:
    needs: up
    uses: ./.github/workflows/other.yml
"""
    assert not decisions(reusable)[("up", "down")].relaxable


def test_aggregator_jobs_never_qualify():
    text = """
name: W
on: [push]
jobs:
  a: {runs-on: ubuntu-latest, steps: [{run: a}]}
  b: {runs-on: ubuntu-latest, steps: [{run: b}]}
  status:
    needs: [a, b]
    if: always()
    runs-on: ubuntu-latest
    steps: [{run: 'echo ${{ needs.a.result }} ${{ needs.b.result }}'}]
"""
    assert relaxable_edges(parse_workflow_text(text)) == set()


def test_critical_path_is_the_longest_chain():
    edges = [("lint", "test"), ("lint", "typecheck")]
    d = {"lint": 36.0, "test": 138.0, "typecheck": 49.0}
    assert critical_path_seconds(edges, d) == 174.0
    assert critical_path_seconds([("lint", "typecheck")], d) == 138.0          # test no longer waits for lint
    assert critical_path_seconds([], d) == 138.0
    assert critical_path_seconds([("a", "b"), ("b", "a")], {"a": 1.0, "b": 2.0}) >= 2.0   # a cycle must not hang


def test_planner_removes_only_edges_that_shorten_the_critical_path():
    wf = parse_workflow_text(CI_SHAPE)
    d = {"lint": 36.0, "test": 138.0, "typecheck": 49.0}
    plan = plan_relaxation(wf, d, {"lint": 0.1, "test": 0.1, "typecheck": 0.1})
    assert plan.relaxed == [("lint", "test")]                  # typecheck keeps its gate: removing it saves nothing
    assert plan.baseline_seconds == 174.0 and plan.relaxed_seconds == 138.0
    assert plan.saved_pct == pytest.approx(100 * 36 / 174)
    assert plan.expected_extra_seconds == pytest.approx(0.1 * 138.0)           # P(lint fails) x duration(test)
    assert [(g.upstream, g.downstream) for g in plan.kept] == [("lint", "typecheck")]


def test_planner_does_nothing_when_no_edge_is_relaxable_or_useful():
    wf = parse_workflow_text(job_workflow("environment: prod\n    steps: [{run: deploy}]"))
    plan = plan_relaxation(wf, {"up": 10.0, "down": 20.0})
    assert plan.relaxed == [] and plan.saved_pct == 0.0


def test_apply_removes_the_gate_and_the_needs_key_when_empty():
    wf = parse_workflow_text(CI_SHAPE)
    out = apply_relaxation(wf.raw, [("lint", "test")])
    assert "needs" not in out["jobs"]["test"] and out["jobs"]["typecheck"]["needs"] == "lint"
    assert wf.raw["jobs"]["test"]["needs"] == "lint"                           # the original dict is not modified


def test_validator_accepts_safe_relaxation_and_rejects_everything_else():
    wf = parse_workflow_text(CI_SHAPE)
    assert validate_relaxation(wf, apply_relaxation(wf.raw, [("lint", "test")])) == []
    assert validate_relaxation(wf, wf.raw) == []                               # no change is valid
    added = apply_relaxation(wf.raw, [])
    added["jobs"]["lint"]["needs"] = "test"
    assert any("edge added" in p for p in validate_relaxation(wf, added))
    dropped = apply_relaxation(wf.raw, [])
    del dropped["jobs"]["typecheck"]
    assert any("set of jobs changed" in p for p in validate_relaxation(wf, dropped))
    guarded = parse_workflow_text(job_workflow("environment: prod\n    steps: [{run: deploy}]"))
    bad = apply_relaxation(guarded.raw, [("up", "down")])                      # forcing an unsafe removal
    assert any("not provably safe" in p for p in validate_relaxation(guarded, bad))


def test_an_if_on_one_upstream_only_blocks_that_edge_not_the_others():
    text = """
name: W
on: [push]
jobs:
  changes: {runs-on: ubuntu-latest, steps: [{run: detect}], outputs: {src: x}}
  lint: {runs-on: ubuntu-latest, steps: [{run: ruff}]}
  test:
    needs: [changes, lint]
    if: ${{ needs.changes.outputs.src == 'true' }}
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
"""
    d = decisions(text)
    assert not d[("changes", "test")].relaxable and d[("lint", "test")].relaxable


@pytest.mark.parametrize("name", ["deploy-docs", "publish", "release", "push-image", "upload-coverage"])
def test_deploy_like_job_names_are_never_relaxed_even_without_other_signals(name):
    text = f"""
name: W
on: [push]
jobs:
  up: {{runs-on: ubuntu-latest, steps: [{{run: build}}]}}
  {name}:
    needs: up
    runs-on: ubuntu-latest
    steps: [{{run: ./script.sh}}]
"""
    g = decisions(text)[("up", name)]
    assert not g.relaxable and any("name suggests" in r for r in g.reasons)


def test_two_parallel_jobs_gated_by_one_upstream_need_both_gates_removed_for_any_saving():
    text = """
name: W
on: [push]
jobs:
  lint: {runs-on: ubuntu-latest, steps: [{run: ruff}]}
  unit: {needs: lint, runs-on: ubuntu-latest, steps: [{run: pytest unit}]}
  integration: {needs: lint, runs-on: ubuntu-latest, steps: [{run: pytest integration}]}
"""
    wf = parse_workflow_text(text)
    # lint 30, unit 100, integration 100: removing one gate alone leaves the other branch at 130
    plan = plan_relaxation(wf, {"lint": 30.0, "unit": 100.0, "integration": 100.0})
    assert sorted(plan.relaxed) == [("lint", "integration"), ("lint", "unit")]
    assert plan.baseline_seconds == 130.0 and plan.relaxed_seconds == 100.0
    # a shorter second branch keeps its gate: only the long branch has to stop waiting
    plan = plan_relaxation(wf, {"lint": 30.0, "unit": 100.0, "integration": 40.0})
    assert plan.relaxed == [("lint", "unit")] and plan.relaxed_seconds == 100.0     # lint -> integration is 70 s: no need
