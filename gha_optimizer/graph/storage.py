import networkx as nx
from sqlalchemy.orm import Session
from gha_optimizer.server.db import models
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class GraphStorage:
    """Handles serialization and persistence of MultiDiGraphs to PostgreSQL."""

    @staticmethod
    def serialize_to_json(g: nx.MultiDiGraph) -> dict:
        """Serialize graph to Node-Link JSON format."""
        return nx.node_link_data(g)

    @staticmethod
    def deserialize_from_json(data: dict) -> nx.MultiDiGraph:
        """Deserialize graph from Node-Link JSON format."""
        return nx.node_link_graph(data, directed=True, multigraph=True)

    @staticmethod
    def save_snapshot(db: Session, repo_id: int, commit_sha: str, g: nx.MultiDiGraph) -> models.GraphSnapshot:
        """Save a complete graph snapshot to the database."""
        json_data = GraphStorage.serialize_to_json(g)

        snapshot = models.GraphSnapshot(
            repo_id=repo_id,
            commit_sha=commit_sha,
            graph_json=json_data
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        logger.info("Saved GraphSnapshot %s for commit %s", snapshot.id, commit_sha)
        return snapshot

    @staticmethod
    def get_snapshot(db: Session, repo_id: int, commit_sha: str) -> Optional[nx.MultiDiGraph]:
        """Retrieve a graph snapshot from the database."""
        snapshot = db.query(models.GraphSnapshot).filter(
            models.GraphSnapshot.repo_id == repo_id,
            models.GraphSnapshot.commit_sha == commit_sha
        ).first()

        if not snapshot:
            return None

        return GraphStorage.deserialize_from_json(snapshot.graph_json)
