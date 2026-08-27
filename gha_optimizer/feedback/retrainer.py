"""
Model retraining module for the continuous learning feedback loop.

Handles scheduled or drift-triggered retraining: pulls the latest N runs
from the database, retrains all models, validates against a holdout set,
stores the new model version, and supports A/B comparison.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from gha_optimizer.server.db import crud

logger = logging.getLogger(__name__)


class Retrainer:
    """
    Orchestrates model retraining using stored execution history.

    Workflow:
      1. Pull the latest N run records from the database.
      2. Convert them to ``WorkflowRunRecord`` objects.
      3. Build the training frame using the current dependency graph.
      4. Retrain all three models (cache, failure, duration).
      5. Compare new model metrics against the current active model.
      6. If the new model improves, save it and register as active.
    """

    def __init__(
        self,
        db,
        model_dir: str | Path = ".gha_optimizer/models",
        min_improvement: float = 0.01,
    ):
        """
        Parameters
        ----------
        db : ``Database`` instance.
        model_dir : Directory for persisted model artifacts.
        min_improvement : Minimum metric improvement required to
            replace the current model (prevents churn from noise).
        """
        self.db = db
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.min_improvement = min_improvement
        self._retrain_history: list[dict] = []

    def retrain(
        self,
        graph,
        training_records,
        force: bool = False,
    ) -> dict:
        """
        Retrain all models and optionally promote the new versions.

        Parameters
        ----------
        graph : The current ``networkx.DiGraph`` dependency graph.
        training_records : List of ``WorkflowRunRecord`` objects.
        force : If True, always promote the new model regardless of
                improvement threshold.

        Returns
        -------
        Dict with per-model results: metrics, promoted (bool), version.
        """
        from gha_optimizer.features.feature_extraction import build_training_frame
        from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
        from gha_optimizer.models.duration_estimator import DurationEstimator
        from gha_optimizer.models.failure_predictor import FailurePredictor

        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        train_df = build_training_frame(graph, training_records)

        results = {}

        # --- Cache-miss predictor ---
        cache_model = CacheMissPredictor()
        cache_metrics = cache_model.fit(train_df)
        cache_promoted = self._maybe_promote(
            model=cache_model,
            model_type="cache_miss",
            metrics=cache_metrics,
            metric_key="f1",
            version=timestamp,
            training_samples=len(train_df),
            force=force,
        )
        results["cache_miss"] = {
            "metrics": cache_metrics,
            "promoted": cache_promoted,
            "version": timestamp,
        }

        # --- Failure predictor ---
        failure_model = FailurePredictor()
        failure_metrics = failure_model.fit(train_df)
        failure_promoted = self._maybe_promote(
            model=failure_model,
            model_type="failure",
            metrics=failure_metrics,
            metric_key="f1",
            version=timestamp,
            training_samples=len(train_df),
            force=force,
        )
        results["failure"] = {
            "metrics": failure_metrics,
            "promoted": failure_promoted,
            "version": timestamp,
        }

        # --- Duration estimator ---
        duration_model = DurationEstimator()
        duration_metrics = duration_model.fit(train_df)
        duration_promoted = self._maybe_promote(
            model=duration_model,
            model_type="duration",
            metrics=duration_metrics,
            metric_key="r2",
            version=timestamp,
            training_samples=len(train_df),
            force=force,
            higher_is_better=True,
        )
        results["duration"] = {
            "metrics": duration_metrics,
            "promoted": duration_promoted,
            "version": timestamp,
        }

        self._retrain_history.append({
            "timestamp": timestamp,
            "training_samples": len(train_df),
            "results": results,
        })

        logger.info(
            "Retraining complete: cache=%s failure=%s duration=%s",
            "promoted" if cache_promoted else "kept_old",
            "promoted" if failure_promoted else "kept_old",
            "promoted" if duration_promoted else "kept_old",
        )

        return results

    def _maybe_promote(
        self,
        model,
        model_type: str,
        metrics: dict,
        metric_key: str,
        version: str,
        training_samples: int,
        force: bool = False,
        higher_is_better: bool = True,
    ) -> bool:
        """
        Compare new model to current active model and promote if better.
        """
        new_value = metrics.get(metric_key, 0.0)

        # Check current active model
        current = crud.get_active_model(self.db, model_type)
        if current and not force:
            current_metrics = current.get("metrics", {})
            if isinstance(current_metrics, str):
                current_metrics = json.loads(current_metrics)
            current_value = current_metrics.get(metric_key, 0.0)

            if higher_is_better:
                improvement = new_value - current_value
            else:
                improvement = current_value - new_value

            if improvement < self.min_improvement:
                logger.info(
                    "%s: new %s=%.4f vs current %.4f "
                    "(improvement %.4f < threshold %.4f) — keeping old",
                    model_type,
                    metric_key,
                    new_value,
                    current_value,
                    improvement,
                    self.min_improvement,
                )
                return False

        # Save model artifact
        artifact_path = self.model_dir / f"{model_type}_{version}.joblib"
        if hasattr(model, "save"):
            model.save(artifact_path)

        # Register in database
        crud.insert_model_version(
            db=self.db,
            model_type=model_type,
            version=version,
            metrics=metrics,
            artifact_path=str(artifact_path),
            training_samples=training_samples,
        )

        logger.info(
            "%s: promoted version %s (%s=%.4f)",
            model_type,
            version,
            metric_key,
            new_value,
        )
        return True

    @property
    def retrain_history(self) -> list[dict]:
        return list(self._retrain_history)
