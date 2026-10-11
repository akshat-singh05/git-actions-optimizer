"""GitHub Actions Performance Optimizer."""
from dotenv import find_dotenv, load_dotenv

# Make a ``.env`` file in the working directory (or above) take effect for every setting that is read from
# ``os.environ`` - GITHUB_TOKEN, GITHUB_REPO, GHA_MODEL_DIR, GHA_HOST, GHA_PORT, DATABASE_URL - not only the two
# fields pydantic-settings fills.  A variable already set in the real environment always wins (``override=False``).
_dotenv_path = find_dotenv(usecwd=True)
if _dotenv_path:
    load_dotenv(_dotenv_path, override=False)
# validation history marker 1
# validation history marker 4
# validation history marker 7
# validation history marker 10
