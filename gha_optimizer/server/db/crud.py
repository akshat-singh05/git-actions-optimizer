from sqlalchemy.orm import Session
from sqlalchemy import desc
from . import models

def insert_optimization_result(db: Session, result: dict):
    db_result = models.OptimizationResult(
        repo=result.get("repo"),
        workflow_name=result.get("workflow_name"),
        baseline_wall_clock=result.get("baseline_wall_clock"),
        optimized_wall_clock=result.get("optimized_wall_clock"),
        baseline_cost=result.get("baseline_cost"),
        optimized_cost=result.get("optimized_cost"),
        baseline_energy=result.get("baseline_energy"),
        optimized_energy=result.get("optimized_energy"),
        jobs_total=result.get("jobs_total"),
        jobs_skipped=result.get("jobs_skipped"),
        plan_json=result.get("plan", {}),
    )
    db.add(db_result)
    db.commit()

def get_optimization_history(db: Session, repo: str = None, limit: int = 50):
    query = db.query(models.OptimizationResult).order_by(desc(models.OptimizationResult.created_at))
    if repo:
        query = query.filter(models.OptimizationResult.repo == repo)
    return [
        {c.name: getattr(row, c.name) for c in row.__table__.columns}
        for row in query.limit(limit).all()
    ]

def get_prediction_accuracy(db: Session, limit: int = 100):
    rows = db.query(models.Prediction).filter(
        models.Prediction.actual_cache_miss.isnot(None)
    ).order_by(desc(models.Prediction.created_at)).limit(limit).all()

    if not rows:
        return {"cache_accuracy": 0.0, "failure_accuracy": 0.0, "duration_mae": 0.0, "sample_count": 0}

    cache_correct = 0
    failure_correct = 0
    duration_errors = []
    total = len(rows)

    for r in rows:
        if r.actual_cache_miss is not None:
            pred = 1 if r.predicted_cache_miss and r.predicted_cache_miss >= 0.5 else 0
            if pred == r.actual_cache_miss:
                cache_correct += 1
        if r.actual_failure is not None:
            pred = 1 if r.predicted_failure and r.predicted_failure >= 0.5 else 0
            if pred == r.actual_failure:
                failure_correct += 1
        if r.actual_duration is not None and r.predicted_duration is not None:
            duration_errors.append(abs(r.actual_duration - r.predicted_duration))

    return {
        "cache_accuracy": cache_correct / total if total > 0 else 0.0,
        "failure_accuracy": failure_correct / total if total > 0 else 0.0,
        "duration_mae": sum(duration_errors) / len(duration_errors) if duration_errors else 0.0,
        "sample_count": total,
    }
