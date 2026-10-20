"""Load the repository's .env consistently without hiding deployment overrides."""
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"


def _parse_env_fallback(path):
    """Fallback parser for basic KEY=VALUE lines when python-dotenv is absent."""
    result = {}
    p = Path(path)
    if not p.is_file():
        return result
    try:
        for raw_line in p.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            result[key] = val
    except Exception:
        pass
    return result


def load_project_env(env_path=None):
    """Load a known env file; non-empty process values win over file values.

    Empty process variables are treated as unset so an exported empty value does
    not mask a value the owner saved in the project's .env file.
    """
    path = Path(env_path).expanduser().resolve() if env_path else ENV_FILE
    try:
        from dotenv import dotenv_values, load_dotenv
        values = dotenv_values(path)
        for name, value in values.items():
            if value is not None and not os.getenv(name, "").strip():
                os.environ.pop(name, None)
        load_dotenv(dotenv_path=path, override=False)
    except ImportError:
        values = _parse_env_fallback(path)
        for name, value in values.items():
            if value is not None and not os.getenv(name, "").strip():
                os.environ[name] = value

    return path
