import networkx as nx

from gha_optimizer.scheduler.optimizer import build_schedule


def _diamond_dag():
    """lint -> {unit, integration} -> build"""
    dag = nx.DiGraph()
    dag.add_edges_from(
        [
            ("lint", "unit_tests"),
            ("lint", "integration_tests"),
            ("unit_tests", "build"),
            ("integration_tests", "build"),
        ]
    )
    return dag


def test_schedule_respects_dependency_order():
    dag = _diamond_dag()
    failure_prob = {n: 0.1 for n in dag.nodes}
    cache_miss = {n: 0.1 for n in dag.nodes}
    duration = {n: 60.0 for n in dag.nodes}

    plan = build_schedule(dag, failure_prob, cache_miss, duration)
    order_by_id = {jp.job_id: jp.order for jp in plan.jobs}

    assert order_by_id["lint"] < order_by_id["unit_tests"]
    assert order_by_id["lint"] < order_by_id["integration_tests"]
    assert order_by_id["unit_tests"] < order_by_id["build"]
    assert order_by_id["integration_tests"] < order_by_id["build"]


def test_high_risk_job_promoted_earlier_among_ready_set():
    dag = _diamond_dag()
    # unit_tests and integration_tests become ready at the same time (both
    # only depend on lint) - integration_tests has much higher failure risk,
    # so fail-fast scheduling should place it before unit_tests.
    failure_prob = {"lint": 0.05, "unit_tests": 0.05, "integration_tests": 0.9, "build": 0.05}
    cache_miss = {n: 0.1 for n in dag.nodes}
    duration = {n: 60.0 for n in dag.nodes}

    plan = build_schedule(dag, failure_prob, cache_miss, duration)
    order_by_id = {jp.job_id: jp.order for jp in plan.jobs}

    assert order_by_id["integration_tests"] < order_by_id["unit_tests"]


def test_downstream_of_high_risk_job_flagged_skip():
    dag = _diamond_dag()
    failure_prob = {"lint": 0.99, "unit_tests": 0.05, "integration_tests": 0.05, "build": 0.05}
    cache_miss = {n: 0.1 for n in dag.nodes}
    duration = {n: 60.0 for n in dag.nodes}

    plan = build_schedule(dag, failure_prob, cache_miss, duration, failure_skip_threshold=0.85)
    skip_by_id = {jp.job_id: jp.skip_recommended for jp in plan.jobs}

    assert skip_by_id["unit_tests"] is True
    assert skip_by_id["integration_tests"] is True
    assert skip_by_id["build"] is True
    assert skip_by_id["lint"] is False  # lint itself still runs; it IS the risk


def test_optimized_wall_clock_less_than_or_equal_baseline_when_skips_occur():
    dag = _diamond_dag()
    failure_prob = {"lint": 0.99, "unit_tests": 0.05, "integration_tests": 0.05, "build": 0.05}
    cache_miss = {n: 0.1 for n in dag.nodes}
    duration = {n: 60.0 for n in dag.nodes}

    plan = build_schedule(dag, failure_prob, cache_miss, duration, failure_skip_threshold=0.85)
    assert plan.estimated_wall_clock_seconds < plan.baseline_wall_clock_seconds
    assert plan.time_saved_pct > 0


def test_dag_levels_assigned():
    """Each job should have a dag_level reflecting its parallelism group."""
    dag = _diamond_dag()
    failure_prob = {n: 0.1 for n in dag.nodes}
    cache_miss = {n: 0.1 for n in dag.nodes}
    duration = {n: 60.0 for n in dag.nodes}

    plan = build_schedule(dag, failure_prob, cache_miss, duration)
    levels = {jp.job_id: jp.dag_level for jp in plan.jobs}

    # lint is at level 0, unit/integration at level 1, build at level 2
    assert levels["lint"] == 0
    assert levels["unit_tests"] == 1
    assert levels["integration_tests"] == 1
    assert levels["build"] == 2


def test_energy_and_co2_computed():
    """Energy and CO₂ estimates should be positive for running jobs."""
    dag = _diamond_dag()
    failure_prob = {n: 0.1 for n in dag.nodes}
    cache_miss = {n: 0.1 for n in dag.nodes}
    duration = {n: 60.0 for n in dag.nodes}

    plan = build_schedule(dag, failure_prob, cache_miss, duration)

    assert plan.estimated_energy_wh > 0
    assert plan.estimated_co2_grams > 0
    assert plan.baseline_energy_wh > 0
    assert plan.baseline_co2_grams > 0


def test_skip_probability_propagation():
    """Skip probability should increase for jobs deeper in the DAG."""
    dag = _diamond_dag()
    # lint has moderate failure risk
    failure_prob = {"lint": 0.5, "unit_tests": 0.1, "integration_tests": 0.1, "build": 0.1}
    cache_miss = {n: 0.1 for n in dag.nodes}
    duration = {n: 60.0 for n in dag.nodes}

    plan = build_schedule(dag, failure_prob, cache_miss, duration, failure_skip_threshold=0.95)
    skip_probs = {jp.job_id: jp.skip_probability for jp in plan.jobs}

    # lint has 0 skip prob (root), its descendants inherit risk
    assert skip_probs["lint"] == 0.0
    assert skip_probs["unit_tests"] > 0
    assert skip_probs["build"] > skip_probs["unit_tests"]


def test_summary_dict():
    """schedule plan should produce a compact summary dict."""
    dag = _diamond_dag()
    failure_prob = {n: 0.1 for n in dag.nodes}
    cache_miss = {n: 0.1 for n in dag.nodes}
    duration = {n: 60.0 for n in dag.nodes}

    plan = build_schedule(dag, failure_prob, cache_miss, duration)
    summary = plan.summary_dict()

    assert "jobs_total" in summary
    assert "wall_clock" in summary
    assert "cost" in summary
    assert "energy" in summary
    assert "co2" in summary
