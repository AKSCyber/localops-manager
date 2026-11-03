"""Read-only identity check before stopping the recorded LocalOps process."""
import sys
from pathlib import Path
import psutil


def verify(pid, root):
    root = Path(root).resolve()
    process = psutil.Process(pid)
    if process.name().lower() not in {"python.exe", "pythonw.exe"}:
        return False
    cwd = Path(process.cwd()).resolve()
    if cwd != root:
        return False
    if not any((cwd / argument).resolve() == root / "app.py" for argument in process.cmdline()[1:]):
        return False
    return any(connection.pid == pid and connection.status == psutil.CONN_LISTEN
               and connection.laddr.port == 8790 for connection in psutil.net_connections(kind="tcp"))


if __name__ == "__main__":
    try:
        raise SystemExit(0 if verify(int(sys.argv[1]), sys.argv[2]) else 1)
    except (psutil.Error, OSError, ValueError):
        raise SystemExit(1)
