"""Run the Comet backend and Vite frontend as one development command."""

import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    processes = [
        subprocess.Popen([sys.executable, "-m", "comet.main"], cwd=ROOT),
        subprocess.Popen(["npm", "run", "dev"], cwd=ROOT / "frontend"),
    ]

    def stop(signum=signal.SIGTERM, _frame=None):
        for process in processes:
            if process.poll() is None:
                process.send_signal(signum)

    previous_handlers = {
        signum: signal.signal(signum, stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(0.2)
    finally:
        stop()
        for process in processes:
            process.wait()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    return next((code for process in processes if (code := process.returncode)), 0)


if __name__ == "__main__":
    raise SystemExit(main())
