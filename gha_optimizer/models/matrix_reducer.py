"""
Matrix Reduction Engine — clusters historically-redundant build-matrix
cells and recommends a reduced matrix that maintains failure-mode coverage
while eliminating redundant compute.

For example, if (ubuntu-22.04, Python 3.10) and (ubuntu-22.04, Python 3.11)
have historically identical pass/fail outcomes, the reducer will suggest
dropping one and keeping only the representative.

Status: NOT WIRED INTO THE PIPELINE.  Only its unit tests call it: ``run_pipeline`` never builds a
``reduced_matrix`` for ``generator.workflow_writer`` (which accepts one), and the pipeline's history is aggregated
per job key, not per matrix cell.  Dropping matrix cells also changes which configurations CI tests, so wiring it
in is a decision about coverage, not just plumbing.  It has not been evaluated on real data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import pdist


@dataclass
class MatrixCell:
    """One concrete combination of matrix axes."""

    cell_id: str
    params: dict[str, Any]
    outcomes: list[int] = field(default_factory=list)  # 0=pass, 1=fail per run

    @property
    def failure_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(self.outcomes) / len(self.outcomes)


@dataclass
class ReductionResult:
    """Output of the matrix reduction engine."""

    original_cells: list[MatrixCell]
    kept_cells: list[MatrixCell]
    dropped_cells: list[MatrixCell]
    clusters: dict[str, int]  # cell_id → cluster_id
    coverage: float  # estimated failure-mode coverage (0–1)

    @property
    def reduction_pct(self) -> float:
        if not self.original_cells:
            return 0.0
        return (
            (len(self.original_cells) - len(self.kept_cells))
            / len(self.original_cells)
            * 100
        )

    def to_matrix_include(self) -> list[dict[str, Any]]:
        """Convert kept cells back into a ``strategy.matrix.include`` list."""
        return [cell.params for cell in self.kept_cells]


class MatrixReducer:
    """
    Cluster matrix cells by historical outcome similarity and drop
    redundant cells while maintaining configurable coverage.

    Algorithm:
      1. Encode each cell's historical outcomes as a binary vector
         (one entry per run: 0=pass, 1=fail).
      2. Compute pairwise Hamming distance between cells.
      3. Hierarchical clustering (Ward's method on the distance matrix).
      4. Cut the dendrogram at a threshold that achieves the desired
         coverage.
      5. From each cluster, keep the cell with the highest failure rate
         (most informative — if it passes, the cluster likely passes too).
    """

    def __init__(
        self,
        coverage_threshold: float = 0.95,
        min_runs_required: int = 10,
    ):
        """
        Parameters
        ----------
        coverage_threshold : float
            Target proportion of unique failure modes to preserve (0–1).
            Higher = more conservative (keep more cells).
        min_runs_required : int
            Minimum number of historical runs before we attempt reduction.
            With too few runs, the outcome vectors are too short to cluster
            reliably.
        """
        self.coverage_threshold = coverage_threshold
        self.min_runs_required = min_runs_required

    def reduce(self, cells: list[MatrixCell]) -> ReductionResult:
        """
        Analyze historical outcomes and recommend a reduced matrix.

        Returns a ``ReductionResult`` with kept and dropped cells.
        """
        if len(cells) <= 1:
            return ReductionResult(
                original_cells=cells,
                kept_cells=list(cells),
                dropped_cells=[],
                clusters={c.cell_id: 0 for c in cells},
                coverage=1.0,
            )

        # Check minimum data requirement
        min_outcomes = min(len(c.outcomes) for c in cells)
        if min_outcomes < self.min_runs_required:
            return ReductionResult(
                original_cells=cells,
                kept_cells=list(cells),
                dropped_cells=[],
                clusters={c.cell_id: i for i, c in enumerate(cells)},
                coverage=1.0,
            )

        # Truncate outcome vectors to the same length (most recent N runs)
        n = min(len(c.outcomes) for c in cells)
        outcome_matrix = np.array(
            [c.outcomes[-n:] for c in cells], dtype=np.float64
        )

        # Handle degenerate case: all outcomes identical
        if outcome_matrix.std() == 0:
            # All cells behave identically → keep only one
            return ReductionResult(
                original_cells=cells,
                kept_cells=[cells[0]],
                dropped_cells=cells[1:],
                clusters={c.cell_id: 0 for c in cells},
                coverage=1.0,
            )

        # Compute pairwise Hamming distances
        distances = pdist(outcome_matrix, metric="hamming")

        # Handle case where all distances are zero (shouldn't happen
        # after the std() check, but defensive)
        if np.all(distances == 0):
            return ReductionResult(
                original_cells=cells,
                kept_cells=[cells[0]],
                dropped_cells=cells[1:],
                clusters={c.cell_id: 0 for c in cells},
                coverage=1.0,
            )

        # Hierarchical clustering
        Z = linkage(distances, method="average")

        # Find the right cut threshold to meet coverage
        # Start with aggressive reduction and relax until coverage is met
        best_result = None
        for t in np.linspace(0.01, 1.0, 50):
            cluster_labels = fcluster(Z, t=t, criterion="distance")
            result = self._select_representatives(cells, cluster_labels, outcome_matrix)
            if result.coverage >= self.coverage_threshold:
                best_result = result
                break

        if best_result is None:
            # Could not meet coverage — keep all cells
            best_result = ReductionResult(
                original_cells=cells,
                kept_cells=list(cells),
                dropped_cells=[],
                clusters={c.cell_id: i for i, c in enumerate(cells)},
                coverage=1.0,
            )

        return best_result

    def _select_representatives(
        self,
        cells: list[MatrixCell],
        cluster_labels: np.ndarray,
        outcome_matrix: np.ndarray,
    ) -> ReductionResult:
        """
        From each cluster, keep the cell with the highest failure rate
        (most informative for detecting breakages).  Estimate coverage
        as the proportion of unique failure patterns preserved.
        """
        clusters: dict[str, int] = {
            cells[i].cell_id: int(cluster_labels[i])
            for i in range(len(cells))
        }

        # Group cells by cluster
        cluster_groups: dict[int, list[int]] = {}
        for idx, label in enumerate(cluster_labels):
            cluster_groups.setdefault(int(label), []).append(idx)

        kept_indices: list[int] = []
        for _, indices in cluster_groups.items():
            # Pick the cell with highest failure rate as representative
            best_idx = max(indices, key=lambda i: cells[i].failure_rate)
            kept_indices.append(best_idx)

        kept_set = set(kept_indices)
        kept_cells = [cells[i] for i in sorted(kept_indices)]
        dropped_cells = [cells[i] for i in range(len(cells)) if i not in kept_set]

        # Estimate coverage: what fraction of unique failure "patterns"
        # in the original matrix are captured by the kept cells?
        # A failure pattern = a run index where at least one cell failed.
        n_runs = outcome_matrix.shape[1]
        original_failure_runs = set()
        for run_idx in range(n_runs):
            if any(outcome_matrix[i, run_idx] == 1 for i in range(len(cells))):
                original_failure_runs.add(run_idx)

        if not original_failure_runs:
            coverage = 1.0
        else:
            kept_failure_runs = set()
            for run_idx in range(n_runs):
                if any(outcome_matrix[i, run_idx] == 1 for i in kept_indices):
                    kept_failure_runs.add(run_idx)
            coverage = len(kept_failure_runs) / len(original_failure_runs)

        return ReductionResult(
            original_cells=cells,
            kept_cells=kept_cells,
            dropped_cells=dropped_cells,
            clusters=clusters,
            coverage=coverage,
        )
