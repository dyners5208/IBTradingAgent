import sys
import os
import glob
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGS_DIR = os.path.join(_PROJECT_ROOT, "logs")
_LOG_KEEP_DAYS = 30


class _TeeLogger:
    """Writes to both the original stdout and a log file with per-line timestamps."""

    def __init__(self, log_path: str):
        self._stdout = sys.stdout
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self._file = open(log_path, "a", encoding="utf-8", buffering=1)
        self._at_line_start = True

    def write(self, text: str) -> None:
        self._stdout.write(text)
        if not text:
            return
        for char in text:
            if self._at_line_start and char != "\n":
                ts = datetime.now().strftime("[%H:%M:%S] ")
                self._file.write(ts)
                self._at_line_start = False
            self._file.write(char)
            if char == "\n":
                self._at_line_start = True

    def flush(self) -> None:
        self._stdout.flush()
        self._file.flush()

    def fileno(self) -> int:
        return self._stdout.fileno()

    def close(self) -> None:
        sys.stdout = self._stdout
        try:
            self._file.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stdout, name)


def setup_session_log() -> "_TeeLogger | None":
    """Redirect sys.stdout to a daily tee log with per-line timestamps.

    Returns the logger so the caller can close it on exit. Returns None on
    failure — the agent continues normally without logging to disk.
    """
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = os.path.join(_LOGS_DIR, f"agent_{today}.log")
        logger = _TeeLogger(log_path)
        sys.stdout = logger

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'=' * 60}")
        print(f"  Session started: {ts}")
        print(f"  Log file: {log_path}")
        print(f"{'=' * 60}")

        _rotate_logs()
        return logger
    except Exception as exc:
        print(f"  [session-log] Setup failed (non-fatal): {exc}")
        return None


def _rotate_logs() -> None:
    """Delete log files older than _LOG_KEEP_DAYS days."""
    try:
        cutoff = datetime.now().timestamp() - _LOG_KEEP_DAYS * 86400
        for path in glob.glob(os.path.join(_LOGS_DIR, "agent_*.log")):
            if os.path.getmtime(path) < cutoff:
                try:
                    os.remove(path)
                except Exception:
                    pass
    except Exception:
        pass
