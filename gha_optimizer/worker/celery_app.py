import os
from celery import Celery
from gha_optimizer.config import config

# Initialize Celery app
celery_app = Celery(
    "gha_optimizer",
    broker=config.redis_url,
    backend=config.redis_url,
    include=["gha_optimizer.worker.tasks"]
)

# Optional configuration, see the application user guide.
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
)
