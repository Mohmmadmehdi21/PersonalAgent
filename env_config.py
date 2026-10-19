"""Load the repository's .env consistently without hiding deployment overrides."""
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_project_env(env_path=None):
    """Load a known env file; non-empty process values win over file values.

    Empty process variables are treated as unset so an exported empty value does
    not mask a value the owner saved in the project's .env file.
    """
    path = Path(env_path).expanduser().resolve() if env_path else ENV_FILE
    try:
        from dotenv import dotenv_values, load_dotenv
    except ImportError:
        return path

    values = dotenv_values(path)
    for name, value in values.items():
        if value is not None and not os.getenv(name, "").strip():
            os.environ.pop(name, None)

    load_dotenv(dotenv_path=path, override=False)
    return path
