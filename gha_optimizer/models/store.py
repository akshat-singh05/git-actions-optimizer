"""
Persisted models for the pipeline path - valid only for the exact data they were trained on.

``run_pipeline`` used to refit three models on every call.  Loading a saved model is only correct if it was
trained on *the same history, features and hyper-parameters*, so every artifact set carries a fingerprint of
those inputs and :meth:`ModelStore.try_load` returns nothing (-> refit and save) when anything differs:

* every history record (labels, durations, outcomes, timestamps, changed files, graph features ...),
* the feature-column contract and the graph-feature definition version,
* the estimators' hyper-parameters / targets (their ``repr``) and the scikit-learn version,
* :data:`SCHEMA_VERSION` (bump it when model code changes meaning without changing any of the above).

A stale or unreadable artifact is never used; the cost of a miss is one refit, which is what happened every time
before.  The holdout metrics shown in explanations are stored with the models so a loaded run reports the same
validation figures it was trained with.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sklearn

from gha_optimizer.features.feature_extraction import FEATURE_COLUMNS
from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION
from gha_optimizer.ingestion.github_client import WorkflowRunRecord
from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
from gha_optimizer.models.duration_estimator import DurationEstimator
from gha_optimizer.models.failure_predictor import FailurePredictor

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# artifact name -> (file name, factory of an empty model)
ARTIFACTS: dict[str, tuple[str, Any]] = {
    "cache_miss": ("cache_miss.joblib", CacheMissPredictor),
    "failure": ("failure.joblib", FailurePredictor),
    "duration": ("duration.joblib", DurationEstimator),
}


def fingerprint(history: list[WorkflowRunRecord]) -> str:
    """SHA-256 over everything a model's training depends on (see the module docstring)."""
    h = hashlib.sha256()
    h.update(json.dumps({
        "schema": SCHEMA_VERSION,
        "features": list(FEATURE_COLUMNS),
        "graph_feature_version": GRAPH_FEATURE_VERSION,
        "sklearn": sklearn.__version__,
        "estimators": {name: repr(factory().model) for name, (_, factory) in ARTIFACTS.items()},
        "targets": {"cache": CacheMissPredictor().target, "duration": DurationEstimator().target},
    }, sort_keys=True).encode())
    for rec in sorted(history, key=lambda r: (str(r.run_id), r.job_id)):
        h.update(json.dumps(dataclasses.asdict(rec), sort_keys=True, default=str).encode())
    return h.hexdigest()


def _slug(workflow_key: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", workflow_key or "default").strip("_") or "default"


class ModelStore:
    """Directory of ``<dir>/<workflow slug>/{cache_miss,failure,duration}.joblib`` plus ``manifest.json``."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def _dir(self, workflow_key: str | None) -> Path:
        return self.directory / _slug(workflow_key)

    def try_load(self, fp: str, workflow_key: str | None) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """``(models, metrics)`` if a complete, readable artifact set matches ``fp``; otherwise None."""
        d = self._dir(workflow_key)
        manifest_path = d / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("fingerprint") != fp:
                logger.info("saved models in %s are for different data; refitting", d)
                return None
            models: dict[str, Any] = {}
            for name, (fname, factory) in ARTIFACTS.items():
                model = factory()
                model.load(d / fname)
                models[name] = model
            return models, manifest["metrics"]
        except (OSError, ValueError, KeyError, EOFError) as exc:  # missing / corrupt / incompatible -> refit
            logger.info("no usable saved models in %s (%s); refitting", d, type(exc).__name__)
            return None

    def save(self, fp: str, workflow_key: str | None, models: dict[str, Any], metrics: dict[str, Any]) -> Path:
        d = self._dir(workflow_key)
        d.mkdir(parents=True, exist_ok=True)
        for name, (fname, _) in ARTIFACTS.items():
            models[name].save(d / fname)
        (d / "manifest.json").write_text(json.dumps({
            "fingerprint": fp,
            "metrics": metrics,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "workflow": workflow_key,
        }, indent=2, default=str), encoding="utf-8")  # written last: a half-written set has no matching manifest
        return d
