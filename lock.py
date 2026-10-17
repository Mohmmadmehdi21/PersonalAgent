import os, sys, time, json, atexit, ctypes
from config import WORK
from logger import log, log_warn

LOCK_FILE = os.path.join(WORK, "run.lock").replace("\\", "/")
STALE_LOCK_SECONDS = 6 * 3600  # 6 hours

def is_pid_alive(pid):
    """Checks if a process with given PID is still running."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        try:
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
            if handle:
                exit_code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                ctypes.windll.kernel32.CloseHandle(handle)
                return exit_code.value == 259  # STILL_ACTIVE
        except Exception:
            pass
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

_lock_acquired = False

def acquire_lock():
    """Atomically acquires a single-instance lock; only dead-owner locks are recovered."""
    global _lock_acquired
    os.makedirs(WORK, exist_ok=True)
    now = time.time()
    lock_data = {
        "pid": os.getpid(),
        "created_at": now,
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(lock_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            break
        except FileExistsError:
            try:
                with open(LOCK_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                prev_pid = existing.get("pid")
                if prev_pid and is_pid_alive(prev_pid):
                    age = max(0, now - existing.get("created_at", now))
                    log_warn(f"[Lock] Another run is currently in progress (PID {prev_pid}, started {age/60:.1f}m ago).")
                    print(f"\n[Warning] Another PersonalAgent process is already running (PID {prev_pid}). Exiting to prevent overlap.\n")
                    sys.exit(0)
                # Recheck owner immediately before unlinking; don't clear a live lock because of age.
                if prev_pid and not is_pid_alive(prev_pid):
                    log_warn(f"[Lock] Dead process lock found (PID {prev_pid}). Recovering it.")
                    os.remove(LOCK_FILE)
                    continue
                log_warn("[Lock] Lock file has no verifiable owner; preserving it and refusing overlap.")
                sys.exit(1)
            except SystemExit:
                raise
            except Exception as e:
                log_warn(f"[Lock] Lock file could not be safely verified ({e}); preserving it.")
                sys.exit(1)
    else:
        print("[Warning] Could not acquire run lock safely; exiting.")
        sys.exit(1)

    _lock_acquired = True
    atexit.register(release_lock)
    log(f"[Lock] Run lock acquired for PID {os.getpid()}")

def release_lock():
    """Releases the run lock on exit."""
    global _lock_acquired
    if _lock_acquired and os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("pid") == os.getpid():
                os.remove(LOCK_FILE)
                log(f"[Lock] Run lock released for PID {os.getpid()}")
        except Exception:
            pass
    _lock_acquired = False
