"""Small atomic runtime registry consumed by owner-only Telegram commands."""
import json
import os
import tempfile
import ctypes
from datetime import datetime, timezone
from pathlib import Path

from logger import mask_secrets

MONITOR_FILE = Path(os.getenv("PERSONALAGENT_MONITOR_FILE", "data/monitoring.json"))
LOG_DIR = Path("logs")
TASK_RESULTS_LIMIT = 40


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_registry(path=MONITOR_FILE):
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _write_registry(data, path=MONITOR_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="monitoring-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def begin_run(path=MONITOR_FILE, pid=None):
    data = read_registry(path)
    data.update({
        "running": True, "pid": int(pid or os.getpid()), "stage": "starting",
        "current_task": "initializing", "run_started_at": _now(), "run_failures": 0,
        "run_results": [], "updated_at": _now(),
    })
    _write_registry(data, path)
    return data


def set_progress(stage, task, path=MONITOR_FILE):
    data = read_registry(path)
    data.update({"running": True, "pid": os.getpid(), "stage": str(stage),
                 "current_task": str(task), "updated_at": _now()})
    _write_registry(data, path)
    return data


def record_event(event, fields, path=MONITOR_FILE):
    """Persist bounded task results; error text is redacted before it reaches disk."""
    data = read_registry(path)
    event = str(event)
    safe = {key: mask_secrets(value) if key in {"error", "reason", "stopped_reason"} else value
            for key, value in fields.items()}
    if event == "pipeline_progress":
        data.update({"running": True, "pid": os.getpid(), "stage": safe.get("stage", "unknown"),
                     "current_task": safe.get("task", "unknown"), "updated_at": _now()})
    elif event == "task_result":
        result = {"at": _now(), "task": str(safe.get("task", "unknown")),
                  "status": str(safe.get("status", "unknown"))}
        for key in ("error_category", "error", "mode"):
            if key in safe:
                result[key] = str(safe[key])[:300]
        results = list(data.get("task_results", []))
        results.append(result)
        data["task_results"] = results[-TASK_RESULTS_LIMIT:]
        if data.get("running"):
            run_results = list(data.get("run_results", []))
            run_results.append(result)
            data["run_results"] = run_results[-TASK_RESULTS_LIMIT:]
            if result["status"] in {"failure", "uncertain", "interrupted"}:
                data["run_failures"] = int(data.get("run_failures", 0)) + 1
        if any(word in result["task"].lower() for word in ("api", "upload", "comment insert")):
            data["last_api_response"] = {
                "at": result["at"], "task": result["task"],
                "status": result["status"], "category": result.get("error_category", ""),
            }
        if result["status"] in {"failure", "uncertain", "interrupted"}:
            data["last_error"] = {"at": result["at"], "task": result["task"],
                                  "category": result.get("error_category", "failure"),
                                  "message": result.get("error", "Task failed")[:300]}
    data["updated_at"] = _now()
    _write_registry(data, path)
    return data


def finish_run(outcome=None, error=None, path=MONITOR_FILE):
    data = read_registry(path)
    if outcome is None:
        outcome = "failed" if int(data.get("run_failures", 0)) else "completed"
    data.update({"running": False, "pid": None, "stage": "idle", "current_task": None,
                 "last_run_status": outcome, "last_run_finished_at": _now(), "updated_at": _now()})
    if error:
        safe = mask_secrets(error)
        data["last_error"] = {"at": _now(), "task": "main", "category": "unhandled",
                              "message": safe[:300]}
    _write_registry(data, path)
    return data


def recent_logs(limit=8):
    files = sorted(LOG_DIR.glob("run_*.log"), key=lambda file: file.stat().st_mtime, reverse=True)
    if not files:
        return ["No run logs found."]
    try:
        lines = files[0].read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ["Recent log file could not be read."]
    return [mask_secrets(line)[:350] for line in lines[-max(1, min(int(limit), 12)):]] or ["Log is empty."]


def integration_status():
    youtube = "configured" if Path("token.json").is_file() or Path("client_secret.json").is_file() else "missing credentials"
    growth_google = "configured" if Path("token_growth.json").is_file() else "not configured"
    adc = bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip() or
               (os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() and os.getenv("GOOGLE_CLOUD_LOCATION", "").strip()))
    return {
        "telegram": "online (this bot is polling)",
        "youtube": youtube,
        "google_growth": growth_google,
        "google_cloud_adc": "configured" if adc else "not configured",
        "note": "Credential presence only; external API connectivity/permissions are not probed.",
    }


def pid_is_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            handle = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return exit_code.value == 259
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False
