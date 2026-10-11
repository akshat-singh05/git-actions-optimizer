from pathlib import Path

from setuptools import find_packages, setup

# The Docker images copy only setup.py and the package, so the README may be absent at install time.
_readme = Path(__file__).with_name("README.md")

setup(
    name="gha-optimizer",
    version="0.2.0",
    description="ML-based predictive scheduler for GitHub Actions CI/CD pipelines",
    long_description=_readme.read_text(encoding="utf-8") if _readme.exists() else "",
    long_description_content_type="text/markdown",
    author="Akshat Singh",
    python_requires=">=3.10",
    packages=find_packages(include=["gha_optimizer", "gha_optimizer.*"]),
    install_requires=[
        "networkx>=3.2",
        "PyYAML>=6.0",
        "pandas>=2.0",
        "scikit-learn>=1.4",
        "joblib>=1.3",
        "numpy>=1.24",
        "scipy>=1.12",
        "requests>=2.31",
        # imported by the core pipeline and CLI (config, ingestion history, graph)
        "pydantic>=2.0",
        "pydantic-settings>=2.2",
        "python-dotenv>=1.0",
        "sqlalchemy>=2.0",
        "tree-sitter>=0.21",
        "tree-sitter-python>=0.21",
        "tree-sitter-javascript>=0.21",
        "tree-sitter-java>=0.21",
        "tree-sitter-go>=0.21",
    ],
    extras_require={
        "server": [
            "fastapi>=0.110", "uvicorn>=0.29", "celery>=5.3", "redis>=5.0", "alembic>=1.13", "psycopg2-binary>=2.9",
        ],
        "ml": ["xgboost>=2.0"],
        "dev": ["pytest>=8.0", "httpx>=0.27", "ruff", "mypy"],
        "all": [
            "fastapi>=0.110",
            "uvicorn>=0.29",
            "celery>=5.3",
            "redis>=5.0",
            "alembic>=1.13",
            "psycopg2-binary>=2.9",
            "xgboost>=2.0",
            "pytest>=8.0",
            "httpx>=0.27",
            "ruff",
            "mypy",
        ],
    },
    entry_points={
        "console_scripts": [
            "gha-optimizer=gha_optimizer.cli:main",
        ],
    },
)
