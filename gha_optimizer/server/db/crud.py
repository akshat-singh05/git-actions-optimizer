import json
from sqlalchemy.orm import Session
from sqlalchemy import desc
from . import models

def insert_run(db: Session, record: dict):
    """Insert a workflow run record, splitting data between WorkflowRun and WorkflowJob tables."""
    # Upsert the run-level record (deduplicates on run_id)
    existing_run = db.query(models.WorkflowRun).filter(
        models.WorkflowRun.run_id == record["run_id"]
    ).first()

    if not existing_run:
        db_run = models.WorkflowRun(
            run_id=record["run_id"],
            commit_sha=record.get("commit_sha"),
            repo=record.get("repo"),
            conclusion=record.get("conclusion"),
            timestamp=record.get("timestamp"),
        )
        db.add(db_run)
        db.flush()  # ensure run_id is available for the job FK

    # Upsert the per-job record
    existing_job = db.query(models.WorkflowJob).filter(
        models.WorkflowJob.run_id == record["run_id"],
        models.WorkflowJob.job_id == record.get("job_id", "unknown"),
    ).first()

    if existing_job:
        existing_job.conclusion = record.get("conclusion")
        existing_job.duration_seconds = record.get("duration_seconds")
        existing_job.cache_hit = bool(record.get("cache_hit", False))
        existing_job.runner_label = record.get("runner_label")
    else:
        db_job = models.WorkflowJob(
            run_id=record["run_id"],
            job_id=record.get("job_id", "unknown"),
            conclusion=record.get("conclusion"),
            duration_seconds=record.get("duration_seconds"),
            cache_hit=bool(record.get("cache_hit", False)),
            runner_label=record.get("runner_label"),
        )
        db.add(db_job)

    db.commit()

def get_recent_runs(db: Session, repo: str = None, limit: int = 500):
    """Retrieve recent run-job records by joining WorkflowRun and WorkflowJob."""
    query = (
        db.query(models.WorkflowRun, models.WorkflowJob)
        .join(models.WorkflowJob, models.WorkflowRun.run_id == models.WorkflowJob.run_id)
        .order_by(desc(models.WorkflowRun.timestamp))
    )
    if repo:
        query = query.filter(models.WorkflowRun.repo == repo)

    results = []
    for run, job in query.limit(limit).all():
        results.append({
            "run_id": run.run_id,
            "commit_sha": run.commit_sha,
            "repo": run.repo,
            "job_id": job.job_id,
            "conclusion": job.conclusion,
            "duration_seconds": job.duration_seconds,
            "cache_hit": job.cache_hit,
            "runner_label": job.runner_label,
            "timestamp": run.timestamp,
            # Fields not in normalized schema — return defaults
            "files_changed": [],
            "author": "unknown",
            "branch": "main",
            "loc_added": 0,
            "loc_deleted": 0,
        })
    return results

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

def insert_model_version(db: Session, model_type: str, version: str, metrics: dict, artifact_path: str, training_samples: int):
    # Deactivate previous versions
    db.query(models.ModelVersion).filter(models.ModelVersion.model_type == model_type).update({"is_active": False})
    
    new_version = models.ModelVersion(
        model_type=model_type,
        version=version,
        metrics=metrics,
        artifact_path=artifact_path,
        training_samples=training_samples,
        is_active=True
    )
    db.add(new_version)
    db.commit()

def get_active_model(db: Session, model_type: str):
    row = db.query(models.ModelVersion).filter(
        models.ModelVersion.model_type == model_type,
        models.ModelVersion.is_active == True
    ).order_by(desc(models.ModelVersion.created_at)).first()
    
    if row:
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}
    return None
