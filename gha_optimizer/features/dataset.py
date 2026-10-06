"""
Real training dataset: one row per (run, workflow job key) built from ingested
history, with a temporal ``split`` column.

* Structural features come from ``run_graph_features`` (graph at the run's commit);
  runs without them get NaN — never a value from a different commit.
* Historical features use strictly earlier runs only (see ``feature_extraction``).
* The split is **temporal at run level**: the last ``temporal_test_fraction`` of runs
  by creation time is the test set; all jobs of a run stay on one side.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import networkx as nx
import pandas as pd
from sqlalchemy.orm import Session

from gha_optimizer.config import config
from gha_optimizer.features.feature_extraction import build_training_frame
from gha_optimizer.ingestion.history import load_history
from gha_optimizer.models.evaluation import temporal_split


def build_dataset(
    db: Session,
    repo: str,
    workflow_path: str | None = None,
    since: datetime | None = None,
    events: Iterable[str] | None = None,
    test_fraction: float | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """``(frame, info)``; ``frame['split']`` is ``'train'`` / ``'test'``; ``info`` has the boundary."""
    history = load_history(db, repo, workflow_path, since, events)
    df = build_training_frame(nx.DiGraph(), history)
    info: dict[str, Any] = {"repo": repo, "workflow_path": workflow_path, "rows": int(len(df))}
    if df.empty:
        df["split"] = []
        return df, info
    frac = test_fraction if test_fraction is not None else config.model.temporal_test_fraction
    train, test, split_info = temporal_split(df, frac)
    df = df.copy()
    df["split"] = "train"
    df.loc[test.index, "split"] = "test"
    info.update(split_info)
    info["train_rows"] = int((df.split == "train").sum())
    info["test_rows"] = int((df.split == "test").sum())
    return df, info
