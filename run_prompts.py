"""Run prompts from a text file, one per line, in serial order.

Usage:
  python run_prompts.py prompts.txt
  python run_prompts.py prompts.txt --timeout 600 --stop-on-error
  python run_prompts.py prompts.txt --log-dir runs

Each non-blank, non-`#` line is one prompt. Prompts run one at a
time, in file order, each as a fresh agent run:
  python supervisor.py --task "<prompt>"
A stale state.json is removed before each prompt, so a prompt
never resumes a previous task. Output goes to the terminal live
and to runs/<timestamp>/prompt-NN.log, with a summary.txt at the
end. Exit code is 0 only if every prompt succeeded.
"""
import argparse
import datetime
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")


def load_prompts(path):
    """Read prompts: one per line, skip blanks and # comments."""
    prompts = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            prompts.append(line)
    return prompts


def default_runner(prompt, log_path, timeout):
    """Run one prompt through the supervisor, tee output to log_path."""
    cmd = [sys.executable, "supervisor.py", "--task", prompt]
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd, cwd=HERE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
        proc.wait(timeout=timeout)
    return proc.returncode


def run_prompts_file(path, timeout=600, stop_on_error=False,
                     log_dir=None, runner=None):
    """Run all prompts in `path`, serially. Returns (results, exit_code)."""
    prompts = load_prompts(path)
    if not prompts:
        print(f"run_prompts: no prompts in {path} (blank lines and # lines are skipped)")
        return [], 2
    runner = runner or default_runner
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = log_dir or os.path.join(HERE, "runs", stamp)
    os.makedirs(run_dir, exist_ok=True)
    results = []
    for i, prompt in enumerate(prompts, start=1):
        log_path = os.path.join(run_dir, f"prompt-{i:02d}.log")
        print(f"\n=== prompt {i}/{len(prompts)}: {prompt} ===", flush=True)
        print(f"=== log: {log_path} ===", flush=True)
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        try:
            code = runner(prompt, log_path, timeout)
        except Exception as exc:  # noqa: BLE001 - record and move on / stop
            code = 1
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"\nrun_prompts: runner failed: {exc}\n")
            print(f"run_prompts: runner failed: {exc}")
        results.append({"index": i, "prompt": prompt,
                        "log": log_path, "exit_code": code})
        print(f"=== prompt {i}/{len(prompts)} finished, exit {code} ===", flush=True)
        if code != 0 and stop_on_error:
            print("run_prompts: stopping on first failure (--stop-on-error)")
            break
    summary_path = os.path.join(run_dir, "summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(f"{r['index']}. exit={r['exit_code']} log={r['log']} prompt={r['prompt']}\n")
    print(f"\nrun_prompts: {sum(1 for r in results if r['exit_code'] == 0)}/{len(results)} succeeded")
    print(f"run_prompts: logs in {run_dir}")
    return results, 0 if all(r["exit_code"] == 0 for r in results) else 1


def main():
    parser = argparse.ArgumentParser(
        description="Run prompts from a text file, one per line, in serial order.")
    parser.add_argument("file", nargs="?", default="prompts.txt",
                        help="Text file, one prompt per line (default: prompts.txt)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Seconds per prompt (default: 600)")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="Stop after the first prompt that fails")
    parser.add_argument("--log-dir", default=None,
                        help="Log directory (default: runs/<timestamp>)")
    args = parser.parse_args()
    if not os.path.exists(args.file):
        print(f"run_prompts: file not found: {args.file}")
        print("Create it first, one prompt per line, e.g.:")
        print("  echo 'Say hello with the hello tool.' > prompts.txt")
        return 2
    _, code = run_prompts_file(args.file, timeout=args.timeout,
                               stop_on_error=args.stop_on_error,
                               log_dir=args.log_dir)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
