"""Tests for run_prompts.py — no Grok key needed, no network.

Run:  python test_run_prompts.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import run_prompts  # noqa: E402


def test_load_prompts_skips_blanks_and_comments():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# a comment\n\nfirst prompt\n  \n# another\nsecond prompt\n")
        path = f.name
    try:
        assert run_prompts.load_prompts(path) == ["first prompt", "second prompt"]
    finally:
        os.remove(path)


def test_run_prompts_serial_order_and_summary():
    seen = []

    def fake_runner(prompt, log_path, timeout):
        seen.append(prompt)
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(f"ran: {prompt}\n")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        prompts_path = os.path.join(tmp, "prompts.txt")
        with open(prompts_path, "w", encoding="utf-8") as f:
            f.write("prompt one\nprompt two\nprompt three\n")
        results, code = run_prompts.run_prompts_file(
            prompts_path, log_dir=os.path.join(tmp, "runs"), runner=fake_runner)
        assert code == 0
        assert seen == ["prompt one", "prompt two", "prompt three"]
        assert [r["exit_code"] for r in results] == [0, 0, 0]
        assert os.path.exists(os.path.join(tmp, "runs", "summary.txt"))


def test_run_prompts_continues_after_failure_by_default():
    def fake_runner(prompt, log_path, timeout):
        with open(log_path, "w", encoding="utf-8") as log:
            log.write("x\n")
        return 1 if prompt == "bad prompt" else 0

    with tempfile.TemporaryDirectory() as tmp:
        prompts_path = os.path.join(tmp, "prompts.txt")
        with open(prompts_path, "w", encoding="utf-8") as f:
            f.write("bad prompt\ngood prompt\n")
        results, code = run_prompts.run_prompts_file(
            prompts_path, log_dir=os.path.join(tmp, "runs"), runner=fake_runner)
        assert code == 1
        assert len(results) == 2  # second prompt still ran


def test_run_prompts_empty_file():
    with tempfile.TemporaryDirectory() as tmp:
        prompts_path = os.path.join(tmp, "prompts.txt")
        with open(prompts_path, "w", encoding="utf-8") as f:
            f.write("# only a comment\n\n")
        results, code = run_prompts.run_prompts_file(
            prompts_path, log_dir=os.path.join(tmp, "runs"))
        assert code == 2 and results == []


if __name__ == "__main__":
    test_load_prompts_skips_blanks_and_comments()
    test_run_prompts_serial_order_and_summary()
    test_run_prompts_continues_after_failure_by_default()
    test_run_prompts_empty_file()
    print("test_run_prompts: 4/4 passed")
