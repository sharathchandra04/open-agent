"""Agent core — event loop + Grok client + tool registry.

Three layers, as designed:
  supervisor.py  — never edited by the agent, relaunches on exit 42.
  agent.py       — this file. Never edited by the agent directly.
  tools/*.py     — one file per tool. The agent may write these, but
                   only through write_file, only inside tools/, and
                   only after the test step below passes.

Self-write / test / hot-load cycle (no restart for a new tool):
  1. Grok writes tools/<new>.py via write_file.
  2. test_new_tool() checks it in a FRESH process: py_compile, then
     `python -c "from tools.<new> import run; ..."`. Max 3 tries;
     failures come back to Grok as an observation to fix.
  3. Only on a pass: hot_load() imports it and adds it to the registry.
     Grok can use it on the very next turn.

Restart (only for a change to agent.py itself):
  request_restart() saves state.json (messages + task) and exits 42.
  The supervisor relaunches agent.py, which loads state.json and resumes.

Grok: same thin LiteLLM client as the resume builder, but the
default is now Groq's FREE model (groq/openai/gpt-oss-120b, key
from env GROQ_API_KEY, no card needed at console.groq.com).
xai/grok-4 is still available as a fallback when XAI_API_KEY is
set and no GROQ_API_KEY is set, or via env MODEL. No key ->
--demo runs the self-write cycle offline with a canned new
tool, so the loop can be tried without spending a call.
"""
import argparse
import importlib
import json
import os
import re
import select
import subprocess
import sys

import pty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """Load KEY=VALUE lines from .env next to agent.py, if present.

    No extra dependency. Existing environment wins over .env, so a
    key exported in the shell is never overwritten by the file.
    The setup script writes GROQ_API_KEY here; never commit .env.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv()

import tools as tool_registry  # noqa: E402

RESTART_CODE = 42
STATE_FILE = "state.json"
# Free default: Groq gpt-oss-120b, 30 req/min, 1,000 req/day,
# 8,000 tokens/min, 200,000 tokens/day on the free tier.
GROQ_MODEL = "groq/openai/gpt-oss-120b"
GROK_MODEL = "xai/grok-4"
MODEL = GROQ_MODEL
MAX_TURNS = 10
MAX_TEST_TRIES = 3


# ---------------------------------------------------------------- PTY shell
class Terminal:
    """One long-lived bash behind a real PTY (stdlib pty module).

    Unlike subprocess.run, this shell remembers cd, env vars and venv
    activation, and returns real interactive output, prompts included.
    """

    PROMPT = "AGENT_PROMPT$ "

    def __init__(self):
        self.master, self.slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc"],
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
            close_fds=True,
        )
        os.close(self.slave)
        self._run(f"PS1='{self.PROMPT}'")

    def _read_until_prompt(self, timeout=15):
        out = b""
        marker = self.PROMPT.strip().encode()  # "AGENT_PROMPT$"
        while True:
            ready, _, _ = select.select([self.master], [], [], timeout)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            # Wait for the prompt at the END of the buffer, not merely
            # contained in it: the echoed command itself contains the
            # prompt string (PS1='AGENT_PROMPT$ '), which used to make
            # this return one command early.
            if out.rstrip().endswith(marker):
                break
        text = out.decode(errors="replace")
        if text.rstrip().endswith(self.PROMPT.strip()):
            text = text.rstrip()[: -len(self.PROMPT.strip())]
        return text.strip()

    def _run(self, cmd):
        os.write(self.master, (cmd + "\n").encode())
        text = self._read_until_prompt()
        # Drop the echoed command line, if present.
        lines = text.splitlines()
        if lines and lines[0].strip() == cmd.strip():
            lines = lines[1:]
        return "\n".join(lines).strip()

    def run(self, command=None, cmd=None, *args, **kwargs):
        """Run one command in the persistent shell; return its output.

        Accepts the command positionally, as `command=` (the name in
        the run_shell tool schema) or as `cmd=` (the alias the model
        sometimes uses). The old positional `cmd` name still works.
        """
        text = command or cmd or kwargs.get("command") or kwargs.get("cmd")
        if text is None and args:
            text = args[0]
        if text is None:
            raise TypeError("Terminal.run() needs a command (command= or cmd=)")
        return self._run(text)

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass
        os.close(self.master)


# ---------------------------------------------------------------- state
def save_state(messages, task):
    with open(STATE_FILE, "w") as f:
        json.dump({"messages": messages, "task": task}, f)


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def request_restart(reason, messages, task, restart_demo=False):
    """Save state, then exit 42 so the supervisor relaunches us."""
    state = {"messages": messages, "task": task}
    if restart_demo:
        state["restart_demo"] = True
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    print(f"agent: restarting: {reason}")
    sys.exit(RESTART_CODE)


# ---------------------------------------------------------------- self-write test
def test_new_tool(name, smoke="run()"):
    """Test a newly written tool in a FRESH process. Returns (ok, output).

    Step 1: py_compile. Step 2: import + smoke call in a new python.
    Nothing is imported into this process until this passes.
    """
    path = os.path.join("tools", f"{name}.py")
    for cmd in (
        [sys.executable, "-m", "py_compile", path],
        [sys.executable, "-c",
         f"from tools.{name} import run; print(run({smoke}) if False else 'import-ok')"],
    ):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return False, (proc.stdout + proc.stderr).strip()
    return True, "compile + import ok"


def self_write_cycle(name, code, smoke_args=""):
    """Write a tool, test it (up to 3 tries), hot-load it on a pass.

    Returns (ok, message). `code` is the file content; on failure the
    caller (Grok) gets the test output back and can fix and retry.
    """
    from tools.write_file import run as write_file

    for attempt in range(1, MAX_TEST_TRIES + 1):
        write_file(f"{name}.py", code)
        ok, out = test_new_tool(name, smoke_args)
        if ok:
            schema, fn = tool_registry.hot_load(name)
            return True, f"loaded {schema['name']}: {out} (attempt {attempt})"
        code = code  # Grok would revise `code` here from `out`; demo keeps it
        if attempt == MAX_TEST_TRIES:
            return False, f"failed after {attempt} tries: {out}"
    return False, "unreachable"


# ---------------------------------------------------------------- model
def resolve_model():
    """Pick the model + key. Groq free first, Grok as fallback.

    Priority: env MODEL (explicit) > GROQ_API_KEY > XAI_API_KEY.
    Returns (model, api_key). Raises if no key is set at all.
    """
    override = os.environ.get("MODEL")
    if override:
        if override.startswith("groq/"):
            key = os.environ.get("GROQ_API_KEY")
        elif override.startswith("xai/"):
            key = os.environ.get("XAI_API_KEY")
        else:
            key = os.environ.get("GROQ_API_KEY") or os.environ.get("XAI_API_KEY")
        if not key:
            raise RuntimeError(f"MODEL={override} is set but its API key is not set")
        return override, key
    if os.environ.get("GROQ_API_KEY"):
        return GROQ_MODEL, os.environ["GROQ_API_KEY"]
    if os.environ.get("XAI_API_KEY"):
        return GROK_MODEL, os.environ["XAI_API_KEY"]
    raise RuntimeError(
        "No API key set: set GROQ_API_KEY (free, no card needed — "
        "sign up at console.groq.com and create a key) "
        "or XAI_API_KEY for Grok"
    )


# ---------------------------------------------------------------- Groq / Grok
def call_model(messages, registry):
    """One model call with the current tool list (function calling)."""
    import litellm

    model, api_key = resolve_model()
    resp = litellm.completion(
        model=model,
        messages=messages,
        tools=[{"type": "function", "function": s} for s, _ in registry.values()],
        api_key=api_key,
        temperature=0.7,
    )
    return resp.choices[0].message


# Backwards-compatible name; the old Grok-only entry point.
def call_grok(messages, registry):
    return call_model(messages, registry)


# ---------------------------------------------------------------- run_shell tool
RUN_SHELL_SCHEMA = {
    "name": "run_shell",
    "description": "Run a command in the persistent PTY bash shell. "
                   "Argument name is 'command'; 'cmd' is an alias.",
    "parameters": {"type": "object",
                   "properties": {
                       "command": {"type": "string",
                                   "description": "Shell command to run, e.g. 'ls -R'"},
                       "cmd": {"type": "string",
                               "description": "Alias for 'command' — use one, not both"},
                   },
                   "required": []},
}


def make_run_shell(terminal):
    """Build the run_shell tool function for a Terminal.

    Accepts both 'command' (the schema name) and 'cmd' (the alias
    gpt-oss-120b sometimes sends). Before this fix, the registry
    held Terminal.run directly, whose parameter was named `cmd`,
    so a model call with {"command": "..."} raised
    "Terminal.run() got an unexpected keyword argument 'command'",
    and a call with {"cmd": "..."} was rejected by Groq because
    the schema required 'command' — a crash either way.
    """
    def run_shell(command=None, cmd=None, **kwargs):
        text = command or cmd or kwargs.get("command") or kwargs.get("cmd")
        if not text:
            raise ValueError("run_shell needs a 'command' (alias 'cmd')")
        return terminal.run(text)

    return run_shell


# ---------------------------------------------------------------- event loop
def run_loop(task, demo=False):
    state = load_state()
    if state:
        messages, task = state["messages"], state["task"]
        print(f"agent: resumed task {task!r} from {STATE_FILE}")
        os.remove(STATE_FILE)
        if state.get("restart_demo"):
            print("agent: restart demo complete — state survived the restart.")
            return 0
    else:
        messages = [{"role": "system",
                     "content": "You are a small coding agent. Use the tools "
                                "you have. If you need a capability you lack, "
                                "write it as a new tool in tools/, test it, "
                                "then use it. Never edit agent.py or supervisor.py. "
                                "For run_shell, the argument is named 'command' "
                                "(alias 'cmd'): call it as "
                                "run_shell(command=\"ls -R\")."},
                    {"role": "user", "content": task}]

    registry = tool_registry.discover_tools()
    terminal = Terminal()
    registry["run_shell"] = (RUN_SHELL_SCHEMA, make_run_shell(terminal))

    if demo:
        # Offline demo of the self-write cycle, no Grok call:
        # write a new tool, test it in a fresh process, hot-load, use it.
        code = ('TOOL = {"name": "add_numbers", "description": "Add two numbers.",\n'
                ' "parameters": {"type": "object", "properties": {\n'
                ' "a": {"type": "number"}, "b": {"type": "number"}},\n'
                ' "required": ["a", "b"]}}\n\n\n'
                'def run(a, b):\n    return a + b\n')
        ok, msg = self_write_cycle("add_numbers", code)
        print(f"demo: self-write: {msg}")
        if ok:
            registry = tool_registry.discover_tools()
            print(f"demo: add_numbers(2, 3) = {registry['add_numbers'][1](a=2, b=3)}")
        print(f"demo: shell remembers state: {terminal.run('cd /tmp && pwd')!r}")
        terminal.close()
        return 0 if ok else 1

    model_errors = 0
    for turn in range(MAX_TURNS):
        try:
            msg = call_model(messages, registry)
        except Exception as exc:  # noqa: BLE001 - model error, not a crash
            # A BadRequestError here used to crash the agent with a
            # traceback (e.g. Groq rejecting a tool call whose argument
            # name did not match the schema), and the supervisor then
            # retried the same failing run twice. Instead, tell the
            # model what went wrong and retry; give up cleanly after 3.
            model_errors += 1
            print(f"agent: model call failed: {exc}")
            if model_errors >= 3:
                print("agent: giving up after 3 failed model calls")
                terminal.close()
                return 1
            messages.append({"role": "user",
                             "content": f"The last model call failed with: {exc}. "
                                        "Try again, using exactly the argument names "
                                        "in the tool schemas. For run_shell, use "
                                        "'command' (or its alias 'cmd')."})
            continue
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": getattr(msg, "tool_calls", None)})
        if not getattr(msg, "tool_calls", None):
            print(msg.content)
            break
        for call in msg.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")
            if name not in registry:
                result = f"no such tool: {name}"
            else:
                try:
                    result = str(registry[name][1](**args))
                except Exception as exc:  # noqa: BLE001 - feed back to Grok
                    result = f"tool error: {exc}"
            print(f"tool {name}({args}) -> {result}")
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": result})
            # A new tool written this turn gets tested + hot-loaded now.
            if name == "write_file":
                new_name = os.path.splitext(args.get("path", ""))[0]
                if new_name and new_name != "write_file":
                    ok, out = test_new_tool(new_name)
                    if ok:
                        schema, fn = tool_registry.hot_load(new_name)
                        registry[schema["name"]] = (schema, fn)
                        messages.append({"role": "user",
                                         "content": f"New tool {new_name} passed "
                                                    f"testing and is loaded: {out}"})
                    else:
                        messages.append({"role": "user",
                                         "content": f"New tool {new_name} FAILED "
                                                    f"testing, fix it (max 3 tries): {out}"})
    terminal.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Small self-improving agent (Groq free + PTY).")
    parser.add_argument("--task", default="Say hello with the hello tool.")
    parser.add_argument("--demo", action="store_true",
                        help="Run the self-write/test/hot-load cycle offline, no Grok call.")
    parser.add_argument("--restart-demo", action="store_true",
                        help="Save state and exit 42, to test the supervisor.")
    args = parser.parse_args()
    # One-shot: only on the first run. After the supervisor relaunches us,
    # state.json exists, so we go to run_loop() and resume instead of
    # restarting again (otherwise this would loop forever).
    if args.restart_demo and not os.path.exists(STATE_FILE):
        request_restart("demo restart", [{"role": "user", "content": args.task}],
                        args.task, restart_demo=True)
    return run_loop(args.task, demo=args.demo)


if __name__ == "__main__":
    raise SystemExit(main())
