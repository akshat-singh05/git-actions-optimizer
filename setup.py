from setuptools import find_packages, setup

setup(
    name="gha-optimizer",
    version="0.2.0",
    description="ML-based predictive scheduler for GitHub Actions CI/CD pipelines",
    long_description=open("README.md", encoding="utf-8").read(),
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
    ],
    extras_require={
        "server": ["fastapi>=0.110", "uvicorn>=0.29"],
        "ml": ["xgboost>=2.0"],
        "dev": ["pytest>=8.0", "httpx>=0.27"],
        "all": [
            "fastapi>=0.110",
            "uvicorn>=0.29",
            "xgboost>=2.0",
            "pytest>=8.0",
            "httpx>=0.27",
        ],
    },
    entry_points={
        "console_scripts": [
            "gha-optimizer=gha_optimizer.cli:main",
        ],
    },
)
