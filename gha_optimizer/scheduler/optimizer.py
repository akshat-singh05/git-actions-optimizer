"""
Layer 4 — Optimization / Scheduler.

This is the system's core novel contribution: it converts per-job
predictions (cache-miss probability, failure probability, expected
duration) into a concrete, dependency-respecting execution plan.

Responsibilities:
  1. Priority-weighted topological scheduling (fail-fast ordering)
  2. Cost-aware runner tier allocation
  3. Early-exit recommendation with probabilistic skip propagation
  4. Parallel execution modeling (wall-clock = max of parallel groups)
  5. Energy / CO₂ cost term (γ weight in the objective)
  6. Matrix reduction integration
"""
from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from gha_optimizer.config import config


# ---------------------------------------------------------------------------
# Runner cost/energy tables (from config, with fallback defaults)
# ---------------------------------------------------------------------------

def _runner_cost_per_minute() -> dict[str, float]:
    return dict(config.scheduler.runner_cost_per_minute)


def _runner_power_watts() -> dict[str, float]:
    return dict(config.scheduler.runner_power_watts)


def _carbon_intensity(region: str = "default") -> float:
    ci = config.scheduler.carbon_intensity
    return ci.get(region, ci.get("default", 300.0))


JOB_OVERHEAD_SECONDS = config.scheduler.job_overhead_seconds


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class JobPlan:
    """Per-job execution plan produced by the scheduler."""

    job_id: str
    order: int
    runner: str
    dag_level: int  # 0-indexed parallelism level in the DAG
    predicted_failure_prob: float
    predicted_cache_miss_prob: float
    predicted_duration_seconds: float
    estimated_duration_seconds: float
    estimated_cost_usd: float
    estimated_energy_wh: float
    estimated_co2_grams: float
    skip_recommended: bool
    skip_probability: float = 0.0
    skip_reason: str | None = None


@dataclass
class SchedulePlan:
    """Complete optimized execution plan for the workflow."""

    jobs: list[JobPlan]
    estimated_wall_clock_seconds: float
    estimated_cost_usd: float
    estimated_energy_wh: float
    estimated_co2_grams: float
    baseline_wall_clock_seconds: float
    baseline_cost_usd: float
    baseline_energy_wh: float
    baseline_co2_grams: float

    @property
    def time_saved_pct(self) -> float:
        if self.baseline_wall_clock_seconds == 0:
            return 0.0
        return (
            (self.baseline_wall_clock_seconds - self.estimated_wall_clock_seconds)
            / self.baseline_wall_clock_seconds
            * 100
        )

    @property
    def cost_saved_pct(self) -> float:
        if self.baseline_cost_usd == 0:
            return 0.0
        return (
            (self.baseline_cost_usd - self.estimated_cost_usd)
            / self.baseline_cost_usd
            * 100
        )

    @property
    def energy_saved_pct(self) -> float:
        if self.baseline_energy_wh == 0:
            return 0.0
        return (
            (self.baseline_energy_wh - self.estimated_energy_wh)
            / self.baseline_energy_wh
            * 100
        )

    @property
    def co2_saved_pct(self) -> float:
        if self.baseline_co2_grams == 0:
            return 0.0
        return (
            (self.baseline_co2_grams - self.estimated_co2_grams)
            / self.baseline_co2_grams
            * 100
        )

    def summary_dict(self) -> dict:
        """Compact summary for API responses / dashboard."""
        return {
            "jobs_total": len(self.jobs),
            "jobs_skipped": sum(1 for j in self.jobs if j.skip_recommended),
            "wall_clock": {
                "baseline_s": round(self.baseline_wall_clock_seconds, 1),
                "optimized_s": round(self.estimated_wall_clock_seconds, 1),
                "saved_pct": round(self.time_saved_pct, 1),
            },
            "cost": {
                "baseline_usd": round(self.baseline_cost_usd, 5),
                "optimized_usd": round(self.estimated_cost_usd, 5),
                "saved_pct": round(self.cost_saved_pct, 1),
            },
            "energy": {
                "baseline_wh": round(self.baseline_energy_wh, 2),
                "optimized_wh": round(self.estimated_energy_wh, 2),
                "saved_pct": round(self.energy_saved_pct, 1),
            },
            "co2": {
                "baseline_g": round(self.baseline_co2_grams, 2),
                "optimized_g": round(self.estimated_co2_grams, 2),
                "saved_pct": round(self.co2_saved_pct, 1),
            },
        }


# ---------------------------------------------------------------------------
# DAG analysis helpers
# ---------------------------------------------------------------------------

def _compute_dag_levels(dag: nx.DiGraph) -> dict[str, int]:
    """
    Assign each node a 'level' = its longest path from any root.

    Nodes at the same level can execute in parallel.  This models
    GitHub Actions' actual scheduling: all jobs at the same level
    whose ``needs:`` are satisfied run concurrently.
    """
    levels: dict[str, int] = {}
    for node in nx.topological_sort(dag):
        preds = list(dag.predecessors(node))
        if not preds:
            levels[node] = 0
        else:
            levels[node] = max(levels[p] for p in preds) + 1
    return levels


def _parallel_wall_clock(
    durations: dict[str, float],
    dag: nx.DiGraph,
    levels: dict[str, int],
    skip_set: set[str],
) -> float:
    """
    Compute wall-clock time accounting for parallelism.

    At each DAG level, the wall-clock is the MAX of all jobs at that level
    (since they run concurrently).  The total is the SUM of level maxima.
    """
    level_max: dict[int, float] = {}
    for node in dag.nodes:
        if node in skip_set:
            continue
        lvl = levels.get(node, 0)
        dur = durations.get(node, 0.0)
        level_max[lvl] = max(level_max.get(lvl, 0.0), dur)
    return sum(level_max.values())


# ---------------------------------------------------------------------------
# Priority topological sort (fail-fast)
# ---------------------------------------------------------------------------

def _priority_topological_order(
    dag: nx.DiGraph, failure_prob: dict[str, float]
) -> list[str]:
    """
    Standard topological sort, but at every step where multiple jobs are
    simultaneously eligible (all their dependencies are already scheduled),
    prefer the job with the HIGHEST predicted failure probability.

    This implements fail-fast scheduling: if something is going to break
    the build, we want to know as early as the DAG allows.
    """
    in_degree = {n: dag.in_degree(n) for n in dag.nodes}
    ready = [n for n, d in in_degree.items() if d == 0]
    order: list[str] = []

    while ready:
        ready.sort(key=lambda n: failure_prob.get(n, 0.0), reverse=True)
        current = ready.pop(0)
        order.append(current)
        for _, nxt in dag.out_edges(current):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                ready.append(nxt)

    return order


# ---------------------------------------------------------------------------
# Runner tier evaluation
# ---------------------------------------------------------------------------

def _evaluate_tier(
    duration_seconds: float,
    cost_per_min: float,
    base_cost_per_min: float,
    power_watts: float,
) -> tuple[float, float, float]:
    """
    Return (estimated_duration, estimated_cost, estimated_energy_wh)
    for one runner tier.
    """
    overhead = min(JOB_OVERHEAD_SECONDS, duration_seconds)
    parallelizable = max(duration_seconds - overhead, 0.0)
    cost_multiplier = cost_per_min / base_cost_per_min if base_cost_per_min > 0 else 1.0
    speedup = cost_multiplier ** 0.6  # diminishing returns
    est_duration = overhead + parallelizable / speedup
    est_cost = (est_duration / 60) * cost_per_min
    est_energy_wh = (est_duration / 3600) * power_watts  # Watt-seconds → Wh
    return est_duration, est_cost, est_energy_wh


def _select_runner(
    predicted_duration_seconds: float,
    alpha: float,
    beta: float,
    gamma: float,
    region: str = "default",
) -> tuple[str, float, float, float, float]:
    """
    Cost-aware runner allocation: choose the tier minimizing

        α·(time_ratio) + β·(cost_ratio) + γ·(energy_ratio)

    where all ratios are relative to the baseline tier.

    Returns (tier, est_duration, est_cost_usd, est_energy_wh, est_co2_g).
    """
    tiers = list(_runner_cost_per_minute().items())
    power = _runner_power_watts()
    ci = _carbon_intensity(region)

    base_tier, base_cost_per_min = tiers[0]
    base_power = power.get(base_tier, 15.0)
    base_duration, base_cost, base_energy = _evaluate_tier(
        predicted_duration_seconds, base_cost_per_min, base_cost_per_min, base_power
    )

    best_tier = base_tier
    best_score = float("inf")
    best_duration = base_duration
    best_cost = base_cost
    best_energy = base_energy

    for tier, cpm in tiers:
        pw = power.get(tier, 15.0)
        est_duration, est_cost, est_energy = _evaluate_tier(
            predicted_duration_seconds, cpm, base_cost_per_min, pw
        )
        time_ratio = est_duration / base_duration if base_duration > 0 else 1.0
        cost_ratio = est_cost / base_cost if base_cost > 0 else 1.0
        energy_ratio = est_energy / base_energy if base_energy > 0 else 1.0
        score = alpha * time_ratio + beta * cost_ratio + gamma * energy_ratio

        if score < best_score:
            best_score = score
            best_tier = tier
            best_duration = est_duration
            best_cost = est_cost
            best_energy = est_energy

    best_co2 = (best_energy / 1000) * ci  # Wh→kWh, then gCO2/kWh
    return best_tier, best_duration, best_cost, best_energy, best_co2


# ---------------------------------------------------------------------------
# Probabilistic skip propagation
# ---------------------------------------------------------------------------

def _propagate_skip_probability(
    dag: nx.DiGraph,
    failure_prob: dict[str, float],
    threshold: float,
) -> dict[str, float]:
    """
    Compute per-job skip probability through the DAG.

    A job's skip probability is the probability that *any* of its upstream
    dependencies will fail (causing this job to be cancelled/skipped).

    For a job with parents p₁, p₂, ...:
        P(skip) = 1 - Π(1 - P(parent_fail_or_skip))

    This is more nuanced than the binary threshold in the baseline scaffold —
    it allows partial-confidence early-exit decisions.
    """
    skip_prob: dict[str, float] = {}

    for node in nx.topological_sort(dag):
        preds = list(dag.predecessors(node))
        if not preds:
            skip_prob[node] = 0.0
            continue

        # P(this job runs) = P(all parents succeed AND aren't skipped)
        p_all_ok = 1.0
        for p in preds:
            # P(parent causes skip) = P(parent fails) + P(parent is skipped) × (1 - P(parent fails))
            p_fail = failure_prob.get(p, 0.0)
            p_parent_skip = skip_prob.get(p, 0.0)
            p_parent_causes_skip = p_fail + p_parent_skip * (1 - p_fail)
            p_all_ok *= (1 - p_parent_causes_skip)

        skip_prob[node] = 1.0 - p_all_ok

    return skip_prob


# ---------------------------------------------------------------------------
# Main scheduler
# ---------------------------------------------------------------------------

def build_schedule(
    dag: nx.DiGraph,
    failure_prob: dict[str, float],
    cache_miss_prob: dict[str, float],
    duration_pred: dict[str, float],
    failure_skip_threshold: float | None = None,
    alpha: float | None = None,
    beta: float | None = None,
    gamma: float | None = None,
    region: str = "default",
) -> SchedulePlan:
    """
    Build an optimized execution schedule from ML predictions.

    Parameters
    ----------
    dag : Job-only DAG (nodes = job_ids, edges = needs-dependencies).
    failure_prob : {job_id: P(failure)} from FailurePredictor.
    cache_miss_prob : {job_id: P(cache_miss)} from CacheMissPredictor.
    duration_pred : {job_id: predicted_seconds} from DurationEstimator.
    failure_skip_threshold : Jobs with skip_probability above this are
        flagged ``skip_recommended``.
    alpha, beta, gamma : Objective weights for time, cost, energy.
    region : Runner region for carbon intensity calculation.
    """
    # Use config defaults if not explicitly provided
    if failure_skip_threshold is None:
        failure_skip_threshold = config.scheduler.failure_skip_threshold
    if alpha is None:
        alpha = config.scheduler.alpha
    if beta is None:
        beta = config.scheduler.beta
    if gamma is None:
        gamma = config.scheduler.gamma

    # Compute DAG levels for parallel execution modeling
    levels = _compute_dag_levels(dag)

    # Fail-fast topological order
    order = _priority_topological_order(dag, failure_prob)

    # Probabilistic skip propagation
    skip_probs = _propagate_skip_probability(dag, failure_prob, failure_skip_threshold)

    # Determine which jobs to skip
    high_risk_jobs = {
        j for j in order if failure_prob.get(j, 0.0) >= failure_skip_threshold
    }
    skip_downstream: set[str] = set()
    for hr in high_risk_jobs:
        skip_downstream |= nx.descendants(dag, hr)

    # --- Baseline calculation (all jobs on cheapest runner, parallel model) ---
    base_tiers = list(_runner_cost_per_minute().items())
    base_cost_per_min = base_tiers[0][1] if base_tiers else 0.008
    base_power = list(_runner_power_watts().values())[0] if _runner_power_watts() else 15.0
    ci = _carbon_intensity(region)

    baseline_durations: dict[str, float] = {}
    baseline_cost_total = 0.0
    baseline_energy_total = 0.0
    for j in dag.nodes:
        dur = duration_pred.get(j, 60.0)
        baseline_durations[j] = dur
        baseline_cost_total += (dur / 60) * base_cost_per_min
        baseline_energy_total += (dur / 3600) * base_power

    baseline_wall_clock = _parallel_wall_clock(baseline_durations, dag, levels, set())
    baseline_co2 = (baseline_energy_total / 1000) * ci

    # --- Optimized plan ---
    job_plans: list[JobPlan] = []
    optimized_durations: dict[str, float] = {}
    optimized_cost_total = 0.0
    optimized_energy_total = 0.0
    optimized_co2_total = 0.0

    for idx, job_id in enumerate(order):
        skip = job_id in skip_downstream
        raw_duration = duration_pred.get(job_id, 60.0)

        runner, est_duration, est_cost, est_energy, est_co2 = _select_runner(
            raw_duration, alpha, beta, gamma, region
        )

        optimized_durations[job_id] = est_duration

        skip_reason = None
        if skip:
            skip_reason = "downstream of high predicted-failure-probability job"
        elif skip_probs.get(job_id, 0.0) > failure_skip_threshold:
            skip = True
            skip_reason = (
                f"cumulative skip probability {skip_probs[job_id]:.2f} "
                f"exceeds threshold {failure_skip_threshold}"
            )

        job_plans.append(
            JobPlan(
                job_id=job_id,
                order=idx,
                runner=runner,
                dag_level=levels.get(job_id, 0),
                predicted_failure_prob=failure_prob.get(job_id, 0.0),
                predicted_cache_miss_prob=cache_miss_prob.get(job_id, 0.0),
                predicted_duration_seconds=raw_duration,
                estimated_duration_seconds=est_duration,
                estimated_cost_usd=est_cost,
                estimated_energy_wh=est_energy,
                estimated_co2_grams=est_co2,
                skip_recommended=skip,
                skip_probability=skip_probs.get(job_id, 0.0),
                skip_reason=skip_reason,
            )
        )

        if not skip:
            optimized_cost_total += est_cost
            optimized_energy_total += est_energy
            optimized_co2_total += est_co2

    skip_set = {jp.job_id for jp in job_plans if jp.skip_recommended}
    optimized_wall_clock = _parallel_wall_clock(
        optimized_durations, dag, levels, skip_set
    )

    return SchedulePlan(
        jobs=job_plans,
        estimated_wall_clock_seconds=optimized_wall_clock,
        estimated_cost_usd=optimized_cost_total,
        estimated_energy_wh=optimized_energy_total,
        estimated_co2_grams=optimized_co2_total,
        baseline_wall_clock_seconds=baseline_wall_clock,
        baseline_cost_usd=baseline_cost_total,
        baseline_energy_wh=baseline_energy_total,
        baseline_co2_grams=baseline_co2,
    )
