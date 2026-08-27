"""
Drift detection for the continuous learning feedback loop.

Compares predicted vs. actual outcomes over a rolling window and
raises alerts when model accuracy drops below configurable thresholds,
triggering the retrainer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class DriftReport:
    """Result of a drift detection check."""

    cache_accuracy: float
    failure_accuracy: float
    duration_mae: float
    sample_count: int
    cache_drifted: bool
    failure_drifted: bool
    duration_drifted: bool
    timestamp: str

    @property
    def any_drift(self) -> bool:
        return self.cache_drifted or self.failure_drifted or self.duration_drifted

    @property
    def summary(self) -> str:
        flags = []
        if self.cache_drifted:
            flags.append(f"cache ({self.cache_accuracy:.2%})")
        if self.failure_drifted:
            flags.append(f"failure ({self.failure_accuracy:.2%})")
        if self.duration_drifted:
            flags.append(f"duration (MAE={self.duration_mae:.1f}s)")
        if not flags:
            return "No drift detected"
        return "Drift detected in: " + ", ".join(flags)


class DriftDetector:
    """
    Monitor model prediction accuracy and detect drift.

    Drift is declared when accuracy drops below a configurable
    threshold over the most recent N predictions.
    """

    def __init__(
        self,
        cache_accuracy_threshold: float = 0.70,
        failure_accuracy_threshold: float = 0.75,
        duration_mae_threshold: float = 60.0,  # seconds
        min_samples: int = 20,
    ):
        self.cache_threshold = cache_accuracy_threshold
        self.failure_threshold = failure_accuracy_threshold
        self.duration_threshold = duration_mae_threshold
        self.min_samples = min_samples
        self._history: list[DriftReport] = []

    def check(self, accuracy_stats: dict[str, float]) -> DriftReport:
        """
        Check for drift given current prediction accuracy stats.

        Parameters
        ----------
        accuracy_stats : Dict with keys ``cache_accuracy``,
            ``failure_accuracy``, ``duration_mae``, ``sample_count``
            from ``Database.get_prediction_accuracy()``.

        Returns
        -------
        DriftReport with per-model drift flags.
        """
        sample_count = accuracy_stats.get("sample_count", 0)

        cache_acc = accuracy_stats.get("cache_accuracy", 1.0)
        failure_acc = accuracy_stats.get("failure_accuracy", 1.0)
        duration_mae = accuracy_stats.get("duration_mae", 0.0)

        # Don't flag drift if we don't have enough data
        has_enough = sample_count >= self.min_samples

        report = DriftReport(
            cache_accuracy=cache_acc,
            failure_accuracy=failure_acc,
            duration_mae=duration_mae,
            sample_count=sample_count,
            cache_drifted=has_enough and cache_acc < self.cache_threshold,
            failure_drifted=has_enough and failure_acc < self.failure_threshold,
            duration_drifted=has_enough and duration_mae > self.duration_threshold,
            timestamp=datetime.utcnow().isoformat(),
        )

        self._history.append(report)

        if report.any_drift:
            logger.warning("Model drift detected: %s", report.summary)
        else:
            logger.info(
                "No drift (cache=%.2f, failure=%.2f, dur_mae=%.1f, n=%d)",
                cache_acc,
                failure_acc,
                duration_mae,
                sample_count,
            )

        return report

    @property
    def drift_history(self) -> list[DriftReport]:
        return list(self._history)

    @property
    def last_report(self) -> DriftReport | None:
        return self._history[-1] if self._history else None
