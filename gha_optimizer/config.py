"""
Central configuration for the gha-optimizer system.

All tuneable knobs live here so they can be set via environment variables
in production or overridden in tests without touching source code.
Uses pydantic-settings for validation.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field


class GithubConfig(BaseModel):
    """GitHub API connection settings."""
    token: Optional[str] = Field(default_factory=lambda: os.environ.get("GITHUB_TOKEN"))
    repo: Optional[str] = Field(default_factory=lambda: os.environ.get("GITHUB_REPO"))
    api_base: str = "https://api.github.com"
    per_page: int = 100
    max_pages: int = 10
    request_timeout: int = 30


class ModelConfig(BaseModel):
    """ML model hyper-parameters and persistence paths."""
    gbt_n_estimators: int = 150
    gbt_max_depth: int = 3
    gbt_learning_rate: float = 0.08
    random_state: int = 13

    test_size: float = 0.25
    cv_folds: int = 5

    # Cold-start policy (a *policy* threshold, not a statistically derived one):
    # below this many ingested runs the system declines to predict.
    min_history_runs: int = 30
    # Labelled rows the cache-miss model needs (both classes present).
    min_cache_labels: int = 30
    # Fraction of runs (by time) held out as the temporal test set.
    temporal_test_fraction: float = 0.2

    model_dir: Path = Field(
        default_factory=lambda: Path(os.environ.get("GHA_MODEL_DIR", ".gha_optimizer/models"))
    )


class SchedulerConfig(BaseModel):
    """Scheduler / optimizer weights and thresholds."""
    alpha: float = 0.7
    beta: float = 0.25
    gamma: float = 0.05
    failure_skip_threshold: float = 0.85

    # ASSUMED, not measured: how much faster a runner that costs `m` times more is, as `m ** speedup_exponent`
    # applied to the parallelisable part of a job.  The ingested history contains one standard runner size per
    # OS, so this cannot be estimated from it (docs/SCHEDULER_ASSUMPTIONS.md).  0 = bigger runners never help.
    speedup_exponent: float = 0.6
    # Rewriting `runs-on` to a larger-runner label only works if the organisation has configured runners with
    # those labels, so it is opt-in.  Off: the plan keeps every job on its original runner.
    allow_runner_upsizing: bool = False

    runner_cost_per_minute: Dict[str, float] = Field(
        default_factory=lambda: {
            "ubuntu-latest": 0.008,
            "ubuntu-latest-4-cores": 0.016,
            "ubuntu-latest-8-cores": 0.032,
            "ubuntu-latest-16-cores": 0.064,
        }
    )

    job_overhead_seconds: float = 12.0

    carbon_intensity: Dict[str, float] = Field(
        default_factory=lambda: {
            "us-east": 380.0,
            "us-west": 210.0,
            "eu-west": 280.0,
            "default": 300.0,
        }
    )

    runner_power_watts: Dict[str, float] = Field(
        default_factory=lambda: {
            "ubuntu-latest": 15.0,
            "ubuntu-latest-4-cores": 30.0,
            "ubuntu-latest-8-cores": 55.0,
            "ubuntu-latest-16-cores": 100.0,
        }
    )


class ServerConfig(BaseModel):
    """FastAPI server configuration."""
    host: str = Field(default_factory=lambda: os.environ.get("GHA_HOST", "0.0.0.0"))
    port: int = Field(default_factory=lambda: int(os.environ.get("GHA_PORT", "8000")))
    webhook_secret: Optional[str] = Field(default_factory=lambda: os.environ.get("GITHUB_WEBHOOK_SECRET"))


class AppConfig(BaseSettings):
    """Top-level configuration aggregating all sub-configs."""

    # Environment configs that can override defaults
    database_url: str = Field(
        default="postgresql://postgres:postgres@db:5432/gha_optimizer",
        alias="DATABASE_URL",
        env="DATABASE_URL"
    )
    redis_url: str = Field(
        default="redis://redis:6379/0",
        alias="REDIS_URL",
        env="REDIS_URL"
    )

    github: GithubConfig = Field(default_factory=GithubConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


# Singleton
config = AppConfig()
