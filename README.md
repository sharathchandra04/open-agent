# Self-improving Python agent — minimal skeleton

Small event agentic loop: Grok + a PTY terminal, able to write tools
for itself, test them, and restart — built from the design discussed
on 2026-11-16 (supervisor / agent / tools, hot-load tools, restart
only for core changes).

## Files

| File | What it is |
|---|---|
| `supervisor.py` | Tiny, dumb, **never edited by the agent**. Runs `agent.py`; exit 42 = relaunch, crash = relaunch once then stop, exit 0 = done. |
| `agent.py` | Event loop + Grok client + tool registry + PTY shell. Never edited by the agent directly. |
| `tools/__init__.py` | Discovers one file per tool; `hot_load()` imports a tested tool. |
| `tools/write_file.py` | Only way the agent writes code. Refuses anything outside `tools/`, and refuses `agent.py` / `supervisor.py`. |
| `tools/hello.py` | Example tool — the shape every tool follows (`TOOL` schema + `run()`). |
| `test_agent.py` | 8 tests, no Grok key, no network. |

## How the self-write cycle works

1. Grok writes `tools/<new>.py` via `write_file`.
2. `agent.test_new_tool()` checks it in a **fresh process**: `py_compile`, then import in a new `python -c`. Max 3 tries; failures go back to Grok as an observation to fix.
3. Only on a pass, `hot_load()` imports it into this process and adds it to the registry. Grok can use it on the next turn. **No restart for a new tool.**
4. For a change to `agent.py` itself: `request_restart()` saves `state.json` (messages + task) and exits 42; the supervisor relaunches and `agent.py` resumes from `state.json`. Always `git commit` before a test run, so a bad self-edit is one `git checkout` away.

## Run it

```bash
pip install -r requirements.txt   # litellm only (PTY shell is stdlib pty)
export XAI_API_KEY="..."          # xAI key; model is xai/grok-4, same thin
                                  # LiteLLM client as the resume builder
python supervisor.py --task "Say hello with the hello tool."
```

Try the loop with no key and no Grok call:

```bash
python agent.py --demo
# writes tools/add_numbers.py, tests it in a fresh process,
# hot-loads it, calls add_numbers(2, 3) = 5, shows the PTY shell
# remembering `cd /tmp`
```

Test the restart path:

```bash
python agent.py --restart-demo   # exits 42
python supervisor.py --restart-demo  # relaunches, resumes, then finishes
```

Tests:

```bash
python test_agent.py   # 8/8, no key, no network
```

## Safety rules (enforced, not just agreed)

- Agent writes only inside `tools/` — enforced in `write_file.py`.
- A new tool is never imported into the agent's own process until it passes `py_compile` + a fresh-process import.
- Core changes (`agent.py`, `supervisor.py`) need a diff the user approves by hand.
- `git commit` before any test run of self-written code.
