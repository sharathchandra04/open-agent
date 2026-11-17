"""Tests for the skeleton — no Grok key needed, no network.

Run:  python test_agent.py        (or: python -m pytest test_agent.py -v)
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import agent  # noqa: E402
import tools as tool_registry  # noqa: E402


def test_discovery_finds_hello_and_write_file():
    reg = tool_registry.discover_tools()
    assert "hello" in reg and "write_file" in reg
    assert reg["hello"][1](name="Rumi") == "Hello, Rumi!"


def test_write_file_refuses_outside_tools():
    from tools.write_file import run
    assert "refused" in run("../agent.py", "x")
    assert "refused" in run("/tmp/evil.py", "x")


def test_self_write_cycle_writes_tests_and_hot_loads():
    code = ('TOOL = {"name": "mul_numbers", "description": "Multiply.",\n'
            ' "parameters": {"type": "object", "properties": {\n'
            ' "a": {"type": "number"}, "b": {"type": "number"}},\n'
            ' "required": ["a", "b"]}}\n\n\n'
            'def run(a, b):\n    return a * b\n')
    ok, msg = agent.self_write_cycle("mul_numbers", code)
    assert ok, msg
    reg = tool_registry.discover_tools()
    assert reg["mul_numbers"][1](a=6, b=7) == 42
    os.remove(os.path.join(HERE, "tools", "mul_numbers.py"))


def test_new_tool_rejects_broken_code():
    from tools.write_file import run as write_file
    write_file("broken_tool.py", "def run(:\n")
    ok, out = agent.test_new_tool("broken_tool")
    assert not ok and out
    os.remove(os.path.join(HERE, "tools", "broken_tool.py"))


def test_terminal_remembers_state():
    term = agent.Terminal()
    try:
        assert term.run("cd /tmp && pwd").strip().endswith("/tmp")
        assert term.run("pwd").strip().endswith("/tmp")  # cd persisted
        assert "hello-pty" in term.run("echo hello-pty")
    finally:
        term.close()


def test_restart_saves_and_resumes_state():
    agent.save_state([{"role": "user", "content": "t"}], "demo task")
    state = agent.load_state()
    assert state["task"] == "demo task"
    os.remove(agent.STATE_FILE)


def test_supervisor_relaunches_on_42_then_stops():
    with tempfile.TemporaryDirectory() as tmp:
        fake = os.path.join(tmp, "agent.py")
        with open(fake, "w") as f:
            f.write("import os, sys\n"
                    "n = int(open('count.txt').read()) if os.path.exists('count.txt') else 0\n"
                    "open('count.txt', 'w').write(str(n + 1))\n"
                    "sys.exit(42 if n == 0 else 0)\n")
        sup = os.path.join(tmp, "supervisor.py")
        with open(sup, "w") as f:
            f.write(open(os.path.join(HERE, "supervisor.py")).read())
        proc = subprocess.run([sys.executable, "supervisor.py"],
                              cwd=tmp, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert open(os.path.join(tmp, "count.txt")).read() == "2"


def test_model_selection_prefers_groq_free():
    old = {k: os.environ.get(k) for k in ("MODEL", "GROQ_API_KEY", "XAI_API_KEY")}
    try:
        os.environ.pop("MODEL", None)
        os.environ["GROQ_API_KEY"] = "test-groq"
        os.environ["XAI_API_KEY"] = "test-xai"
        model, key = agent.resolve_model()
        assert model == "groq/openai/gpt-oss-120b" and key == "test-groq"
        os.environ.pop("GROQ_API_KEY", None)
        model, key = agent.resolve_model()
        assert model == "xai/grok-4" and key == "test-xai"
        os.environ.pop("XAI_API_KEY", None)
        try:
            agent.resolve_model()
            raise AssertionError("expected RuntimeError with no key")
        except RuntimeError as exc:
            assert "GROQ_API_KEY" in str(exc)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_demo_runs_offline():
    proc = subprocess.run([sys.executable, "agent.py", "--demo"],
                          cwd=HERE, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "add_numbers(2, 3) = 5" in proc.stdout


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
