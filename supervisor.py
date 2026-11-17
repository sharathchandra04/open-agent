"""Supervisor — the one file the agent is never allowed to edit.

Its only job: run agent.py in a loop.
- exit code 42  -> agent asked for a restart (core changed): relaunch.
- any other non-zero exit -> crash: relaunch once, then stop, so a
  broken agent does not spin forever. (Raise MAX_CRASHES if you want.)
- exit code 0 -> agent finished its task: stop.

State survives a restart because agent.py saves state.json before
exiting 42, and loads it on startup.
"""
import subprocess
import sys
import time

RESTART_CODE = 42
MAX_CRASHES = 1


def main() -> int:
    crashes = 0
    while True:
        proc = subprocess.run([sys.executable, "agent.py"] + sys.argv[1:])
        if proc.returncode == RESTART_CODE:
            print("supervisor: agent asked for restart, relaunching...")
            time.sleep(1)
            crashes = 0
            continue
        if proc.returncode == 0:
            print("supervisor: agent finished, stopping.")
            return 0
        crashes += 1
        print(f"supervisor: agent crashed (exit {proc.returncode}), "
              f"crash {crashes}/{MAX_CRASHES}")
        if crashes > MAX_CRASHES:
            print("supervisor: too many crashes, stopping.")
            return proc.returncode
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
