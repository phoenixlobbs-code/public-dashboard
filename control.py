"""
control.py  —  Start / stop / status the public dashboard proxy as a
               background Windows process (no console window required).

Usage:
  python control.py start
  python control.py stop
  python control.py restart
  python control.py status
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT    = Path(__file__).resolve().parent
PID_FILE = ROOT / "proxy.pid"
LOG_FILE = ROOT / "proxy.log"
PORT    = 8100


# ── Process helpers ────────────────────────────────────────────────────────

def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _write_pid(pid: int) -> None:
    PID_FILE.write_text(str(pid))


def _clear_pid() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


IS_WINDOWS = sys.platform == "win32"


def _is_running(pid: int) -> bool:
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return False


def _kill(pid: int) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        print(f"  [warn] kill failed: {e}")


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_status() -> bool:
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"  RUNNING  (PID {pid})  ->  http://localhost:{PORT}")
        return True
    else:
        print("  STOPPED")
        if pid:
            _clear_pid()
        return False


def cmd_start() -> None:
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"  Already running (PID {pid})  ->  http://localhost:{PORT}")
        return

    log = open(LOG_FILE, "a", encoding="utf-8")
    log.write(f"\n{'='*60}\n[control] Starting proxy — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    kwargs = dict(
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "proxy:app",
         "--host", "0.0.0.0", "--port", str(PORT)],
        **kwargs,
    )
    _write_pid(proc.pid)

    # Brief wait to confirm it started
    time.sleep(2)
    if _is_running(proc.pid):
        print(f"  STARTED  (PID {proc.pid})  ->  http://localhost:{PORT}")
        print(f"  Log:  {LOG_FILE}")
    else:
        print("  FAILED to start — check proxy.log for details")
        _clear_pid()


def cmd_stop() -> None:
    pid = _read_pid()
    if not pid:
        print("  Not running (no PID file)")
        return
    if not _is_running(pid):
        print("  Not running (process already gone)")
        _clear_pid()
        return
    _kill(pid)
    time.sleep(1)
    if _is_running(pid):
        print(f"  [warn] Process {pid} still alive after kill")
    else:
        print(f"  STOPPED  (PID {pid})")
        _clear_pid()


def cmd_restart() -> None:
    print("  Stopping…")
    cmd_stop()
    time.sleep(1)
    print("  Starting…")
    cmd_start()


# ── Entry point ────────────────────────────────────────────────────────────

_COMMANDS = {
    "start":   cmd_start,
    "stop":    cmd_stop,
    "restart": cmd_restart,
    "status":  cmd_status,
}

if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    fn = _COMMANDS.get(cmd)
    if fn is None:
        print(f"Unknown command: {cmd}")
        print(f"Usage: python control.py [{' | '.join(_COMMANDS)}]")
        sys.exit(1)
    print(f"\n  LoftAlgoTrades Dashboard — {cmd.upper()}")
    fn()
    print()
