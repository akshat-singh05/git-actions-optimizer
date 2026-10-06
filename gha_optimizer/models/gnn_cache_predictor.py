"""
Model D — GNN-based Cache-Miss Predictor.

**This is the primary patent-differentiating component.**

Unlike the tabular baseline (Model A) which uses flat features, this model
reasons directly over the *structural* dependency graph using a heterogeneous
Graph Neural Network (GNN).  It can learn multi-hop patterns like:

    "when ``requirements.txt`` (file node) changes, and it has an import
    edge to ``requests`` (package node), and that package is cached by
    the ``pip-cache`` key (cachekey node), predict cache miss for the
    downstream ``unit_tests`` job (job node)"

This structural reasoning over the file→package→cachekey→job graph is
what existing CI/CD tools (which only hash lockfiles) cannot do, making
it the defensible, non-obvious mechanism in the patent claims.

Architecture:
  - Input: ``torch_geometric.data.HeteroData`` from ``graph_utils.py``
  - Layers: 2–3 ``HeteroConv`` with ``SAGEConv`` per edge type
  - Readout: per-job-node binary classification (cache miss / cache hit)
  - Training: supervised on historical (graph, changed_files, outcome) tuples

Requires ``torch`` and ``torch_geometric``.
"""
from __future__ import annotations

import logging
from pathlib import Path

import networkx as nx

logger = logging.getLogger(__name__)

# Guard imports — the rest of the package works without torch
_HAS_TORCH = False
try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch_geometric.nn import HeteroConv, SAGEConv, Linear

    _HAS_TORCH = True
except ImportError:
    pass

from gha_optimizer.models.graph_utils import (  # noqa: E402  (after the guarded torch import)
    NODE_FEATURE_DIM,
    networkx_to_heterodata,
)


def _require_torch():
    if not _HAS_TORCH:
        raise ImportError(
            "GNN cache predictor requires torch and torch-geometric. "
            "Install with:  pip install torch torch-geometric"
        )


# ---------------------------------------------------------------------------
# Model definition
# ---------------------------------------------------------------------------

if _HAS_TORCH:

    class GNNCacheMissNet(nn.Module):
        """
        Heterogeneous GNN for per-job cache-miss prediction.

        Uses ``HeteroConv`` wrapping ``SAGEConv`` so each edge type
        (touched_by, declares, imports, needs, has_cell) gets its own
        learnable message-passing function, while all node types share
        the same dimensional hidden space for aggregation.
        """

        def __init__(
            self,
            in_channels: int = NODE_FEATURE_DIM,
            hidden_channels: int = 64,
            num_layers: int = 3,
            dropout: float = 0.2,
        ):
            super().__init__()
            self.num_layers = num_layers
            self.dropout = dropout

            # Node-type-specific input projections
            node_types = ["file", "job", "cachekey", "package", "matrix_cell"]
            self.input_projections = nn.ModuleDict({
                nt: Linear(in_channels, hidden_channels) for nt in node_types
            })

            # Heterogeneous convolution layers
            self.convs = nn.ModuleList()
            self.norms = nn.ModuleList()

            edge_types = [
                ("file", "touched_by", "job"),
                ("job", "declares", "cachekey"),
                ("file", "imports", "package"),
                ("job", "needs", "job"),
                ("job", "has_cell", "matrix_cell"),
            ]

            for _ in range(num_layers):
                conv_dict = {}
                for et in edge_types:
                    conv_dict[et] = SAGEConv(
                        (hidden_channels, hidden_channels),
                        hidden_channels,
                    )
                self.convs.append(HeteroConv(conv_dict, aggr="mean"))
                self.norms.append(
                    nn.ModuleDict({
                        nt: nn.LayerNorm(hidden_channels) for nt in node_types
                    })
                )

            # Readout head: per-job binary classification
            self.classifier = nn.Sequential(
                nn.Linear(hidden_channels, hidden_channels // 2),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(hidden_channels // 2, 2),  # 2 classes: hit, miss
            )

        def forward(self, data) -> torch.Tensor:
            """
            Forward pass.

            Parameters
            ----------
            data : HeteroData with node features and edge indices.

            Returns
            -------
            Logits for job nodes, shape ``(num_job_nodes, 2)``.
            """
            # Project each node type to hidden space
            x_dict = {}
            for ntype in ["file", "job", "cachekey", "package", "matrix_cell"]:
                if ntype in data.node_types and hasattr(data[ntype], "x"):
                    x_dict[ntype] = self.input_projections[ntype](data[ntype].x)

            if not x_dict:
                raise ValueError("No node features found in HeteroData.")

            # Message passing
            for i, conv in enumerate(self.convs):
                # Build edge_index_dict from available edges
                edge_index_dict = {}
                for edge_type in data.edge_types:
                    src_type, rel, dst_type = edge_type
                    if src_type in x_dict and dst_type in x_dict:
                        edge_index_dict[edge_type] = data[edge_type].edge_index

                if edge_index_dict:
                    out = conv(x_dict, edge_index_dict)
                    # Apply normalization, activation, dropout
                    for ntype in out:
                        if ntype in self.norms[i]:
                            out[ntype] = self.norms[i][ntype](out[ntype])
                        out[ntype] = F.relu(out[ntype])
                        out[ntype] = F.dropout(
                            out[ntype], p=self.dropout, training=self.training
                        )
                    # Residual connection for existing node types
                    for ntype in out:
                        if ntype in x_dict:
                            x_dict[ntype] = x_dict[ntype] + out[ntype]
                        else:
                            x_dict[ntype] = out[ntype]

            # Readout: classify job nodes
            if "job" not in x_dict:
                raise ValueError("No 'job' nodes in the graph.")

            job_embeddings = x_dict["job"]
            logits = self.classifier(job_embeddings)
            return logits

else:
    # Placeholder when torch is not available
    GNNCacheMissNet = None  # type: ignore[misc, assignment]


# ---------------------------------------------------------------------------
# Predictor wrapper (matches CacheMissPredictor interface)
# ---------------------------------------------------------------------------

class GNNCachePredictor:
    """
    High-level predictor wrapping the GNN model.

    Provides the same ``fit()`` / ``predict_proba()`` / ``save()`` /
    ``load()`` interface as ``CacheMissPredictor`` so the pipeline can
    swap between tabular and GNN backends transparently.
    """

    def __init__(
        self,
        hidden_channels: int = 64,
        num_layers: int = 3,
        lr: float = 1e-3,
        epochs: int = 50,
        dropout: float = 0.2,
    ):
        _require_torch()
        self.hidden_channels = hidden_channels
        self.num_layers = num_layers
        self.lr = lr
        self.epochs = epochs
        self.dropout = dropout

        self.model = GNNCacheMissNet(
            hidden_channels=hidden_channels,
            num_layers=num_layers,
            dropout=dropout,
        )
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)
        self._fitted = False

    def fit(
        self,
        graphs: list[nx.DiGraph],
        changed_files_list: list[list[str]],
        job_labels_list: list[dict[str, int]],
    ) -> dict:
        """
        Train the GNN on historical graph snapshots.

        Parameters
        ----------
        graphs : List of dependency graphs (one per historical run).
        changed_files_list : Per-run list of changed file paths.
        job_labels_list : Per-run ``{job_id: 0|1}`` cache-miss labels.

        Returns
        -------
        Training metrics dict.
        """
        _require_torch()

        # Convert to HeteroData
        data_list = []
        for g, changed, labels in zip(
            graphs, changed_files_list, job_labels_list
        ):
            hetero = networkx_to_heterodata(g, changed, labels)
            data_list.append(hetero)

        self.model.train()
        losses = []

        for epoch in range(self.epochs):
            epoch_loss = 0.0
            n_samples = 0

            for data in data_list:
                if "job" not in data.node_types:
                    continue
                if not hasattr(data["job"], "y"):
                    continue

                labels = data["job"].y
                mask = labels >= 0  # -1 = unlabeled
                if not mask.any():
                    continue

                self.optimizer.zero_grad()
                logits = self.model(data)
                loss = F.cross_entropy(logits[mask], labels[mask])
                loss.backward()
                self.optimizer.step()

                epoch_loss += loss.item() * mask.sum().item()
                n_samples += mask.sum().item()

            if n_samples > 0:
                losses.append(epoch_loss / n_samples)

        self._fitted = True

        # Compute training accuracy
        self.model.eval()
        correct = 0
        total = 0
        with torch.no_grad():
            for data in data_list:
                if "job" not in data.node_types or not hasattr(data["job"], "y"):
                    continue
                labels = data["job"].y
                mask = labels >= 0
                if not mask.any():
                    continue
                logits = self.model(data)
                preds = logits[mask].argmax(dim=-1)
                correct += (preds == labels[mask]).sum().item()
                total += mask.sum().item()

        return {
            "backend": "gnn",
            "epochs": self.epochs,
            "final_loss": round(losses[-1], 4) if losses else 0.0,
            "train_accuracy": round(correct / total, 4) if total > 0 else 0.0,
            "num_graphs": len(data_list),
        }

    def predict_proba_graph(
        self,
        g: nx.DiGraph,
        changed_files: list[str],
    ) -> dict[str, float]:
        """
        Predict P(cache_miss) per job node on a single graph.

        Returns ``{job_id: probability}``.
        """
        _require_torch()
        if not self._fitted:
            raise RuntimeError("Call fit() before predict_proba_graph().")

        data = networkx_to_heterodata(g, changed_files)

        self.model.eval()
        with torch.no_grad():
            logits = self.model(data)
            probs = F.softmax(logits, dim=-1)[:, 1]  # P(miss)

        # Map back to job IDs
        job_nodes = [
            n for n, d in g.nodes(data=True) if d.get("type") == "job"
        ]
        result = {}
        for idx, node in enumerate(job_nodes):
            job_id = node.split("::", 1)[1] if "::" in node else node
            if idx < len(probs):
                result[job_id] = float(probs[idx])

        return result

    def predict_proba(self, df, g: nx.DiGraph = None, changed_files: list[str] = None):
        """
        Compatibility shim: if called with a DataFrame (tabular interface),
        fall back to the graph-based prediction using the provided graph.
        """
        if g is not None and changed_files is not None:
            probs_dict = self.predict_proba_graph(g, changed_files)
            import pandas as pd
            return pd.Series(
                [probs_dict.get(jid, 0.5) for jid in df["job_id"]],
                index=df.index,
            )
        raise ValueError(
            "GNN predictor requires 'g' (graph) and 'changed_files' arguments."
        )

    def save(self, path: str | Path) -> None:
        _require_torch()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "model_state_dict": self.model.state_dict(),
                "config": {
                    "hidden_channels": self.hidden_channels,
                    "num_layers": self.num_layers,
                    "dropout": self.dropout,
                },
            },
            path,
        )

    def load(self, path: str | Path) -> None:
        _require_torch()
        checkpoint = torch.load(path, map_location="cpu")
        cfg = checkpoint.get("config", {})
        self.model = GNNCacheMissNet(
            hidden_channels=cfg.get("hidden_channels", 64),
            num_layers=cfg.get("num_layers", 3),
            dropout=cfg.get("dropout", 0.2),
        )
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self._fitted = True
