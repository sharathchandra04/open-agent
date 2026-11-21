# Self-improving Python agent — minimal skeleton

Small event agentic loop: Groq free model + a PTY terminal, able to write tools
for itself, test them, and restart — built from the design discussed
on 2026-11-16 (supervisor / agent / tools, hot-load tools, restart
only for core changes).

Default model is Groq's free `openai/gpt-oss-120b` (30 req/min,
1,000 req/day, 8,000 tokens/min, 200,000 tokens/day, no card).
Grok (`xai/grok-4`) is still a fallback if `XAI_API_KEY` is set
and no Groq key is set, or set `MODEL=xai/grok-4` explicitly.

## Files

| File | What it is |
|---|---|
| `supervisor.py` | Tiny, dumb, **never edited by the agent**. Runs `agent.py`; exit 42 = relaunch, crash = relaunch once then stop, exit 0 = done. |
| `agent.py` | Event loop + Grok client + tool registry + PTY shell. Never edited by the agent directly. |
| `tools/__init__.py` | Discovers one file per tool; `hot_load()` imports a tested tool. |
| `tools/write_file.py` | Only way the agent writes code. Refuses anything outside `tools/`, and refuses `agent.py` / `supervisor.py`. |
| `tools/hello.py` | Example tool — the shape every tool follows (`TOOL` schema + `run()`). |
| `test_agent.py` | 12 tests, no Grok key, no network. |
| `run_prompts.py` | Runs prompts from a text file, one per line, in serial order, each as a fresh `supervisor.py --task` run. Logs to `runs/<timestamp>/prompt-NN.log` + `summary.txt`. |
| `test_run_prompts.py` | 4 tests for the prompts-file runner, no key, no network. |
| `prompts.txt` | Example prompts file — one prompt per line. Edit it with your own prompts. |
| `setup_open_agent.sh` | One-command setup for WSL Ubuntu: clones/updates the repo, makes `.venv`, installs deps, saves your Groq key to git-ignored `.env`, runs tests + offline demo. |

## How the self-write cycle works

1. Grok writes `tools/<new>.py` via `write_file`.
2. `agent.test_new_tool()` checks it in a **fresh process**: `py_compile`, then import in a new `python -c`. Max 3 tries; failures go back to Grok as an observation to fix.
3. Only on a pass, `hot_load()` imports it into this process and adds it to the registry. Grok can use it on the next turn. **No restart for a new tool.**
4. For a change to `agent.py` itself: `request_restart()` saves `state.json` (messages + task) and exits 42; the supervisor relaunches and `agent.py` resumes from `state.json`. Always `git commit` before a test run, so a bad self-edit is one `git checkout` away.

## Run it on your laptop (WSL Ubuntu) — one command

Download `setup_open_agent.sh`, then in WSL Ubuntu:

```bash
bash setup_open_agent.sh
```

That clones/updates the repo to `~/open-agent`, creates `.venv`,
installs dependencies, asks once for your free Groq key (saved to
git-ignored `.env`, never to GitHub), runs the tests and the offline
demo, and prints the start commands. You need WSL Ubuntu installed
first (`wsl --install Ubuntu` on Windows) — the script itself does
not install WSL.

## Run it

```bash
pip install -r requirements.txt   # litellm only (PTY shell is stdlib pty)
export GROQ_API_KEY="..."         # free key, no card: sign up at
                                  # console.groq.com and create a key
python supervisor.py --task "Say hello with the hello tool."
```

Or put the key in `.env` next to `agent.py` instead — `agent.py`
loads it automatically, and `.env` is git-ignored:

```bash
echo 'GROQ_API_KEY=your-key-here' > .env
```

### Many prompts from a text file, in serial order

```bash
# Edit prompts.txt — one prompt per line, blank lines and # lines skipped.
# 10 prompts = 10 lines, they run one after another, in file order.
python run_prompts.py prompts.txt
python run_prompts.py prompts.txt --timeout 600 --stop-on-error
```

Each prompt runs as a fresh `supervisor.py --task "<prompt>"` (so a
restart inside one prompt is handled by the supervisor), a stale
`state.json` is removed before each prompt, and output goes live to
the terminal and to `runs/<timestamp>/prompt-NN.log`, with a
`summary.txt` at the end. If one prompt fails, the rest still run
unless you pass `--stop-on-error`.

To use Grok instead: `export XAI_API_KEY="..."` (and no Groq key),
or `MODEL=xai/grok-4 python supervisor.py --task "..."`.
To pick another free Groq model: `MODEL=groq/openai/gpt-oss-20b ...`

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
python test_agent.py   # 12/12, no key, no network
python test_run_prompts.py   # 4/4, no key, no network
python test_telegram_bot.py   # Telegram bot tests, no network (API is faked)
```

## Telegram bot

Chat with the agent from Telegram, remotely, for free — no public IP
needed. `telegram_bot.py` long-polls the Bot API (outbound https only,
so it works behind Wi-Fi / NAT) and runs each message as an agent task
(`python agent.py --task <prompt>`, up to 300s per message). One message
at a time, in order.

### Setup

1. On Telegram, message **@BotFather**, send `/newbot`, follow the steps,
   and copy the token it gives you.
2. Message your new bot once (any text), then look at the bot's log:
   it prints `ignored message from chat_id=...` — that number is your
   chat id. (Or ask **@userinfobot** for your id.)
3. Set the env vars (never put the token in code, in chat, or in a file
   you share — anyone with it controls the bot):
   ```bash
   export TELEGRAM_BOT_TOKEN="..."   # from @BotFather
   export TELEGRAM_CHAT_ID="..."     # your chat id from step 2
   export GROQ_API_KEY="..."         # free key, as before
   ```
4. Install and run:
   ```bash
   pip install -r requirements.txt
   python telegram_bot.py
   ```
   Then send the bot a message on Telegram; the agent's final answer
   comes back as the reply. Keep the laptop on and online — if it
   sleeps, the bot stops polling. Groq's free limits still apply per
   call (30 req/min, 1,000 req/day, 200,000 tokens/day).

### Security notes

- **Allowlist:** only messages from `TELEGRAM_CHAT_ID` (or
  `TELEGRAM_USER_ID`, if you set that instead) are accepted. Everything
  else is ignored and its chat id is logged once, so you can spot a
  wrong id. Without this, anyone who finds the bot could run commands
  on your laptop through the agent's shell.
- **Token:** never share `TELEGRAM_BOT_TOKEN`. If it leaks, revoke it
  with @BotFather (`/revoke`).
- **Answer parsing:** the bot takes the agent's final answer as
  everything printed after the last `tool ... -> ...` line (housekeeping
  `agent: ...` lines dropped). If the agent exits 42 it is re-run up to
  3 times, resuming from `state.json`; a stale `state.json` from a
  crashed run is removed before each fresh prompt so a new prompt never
  resumes an old task. Replies longer than 4096 chars are split into
  Telegram-sized chunks, in order.

### Optional encryption layer (opt-in)

Telegram bot chats are **not** end-to-end encrypted — Telegram can read
them. Plain messages work by default. If you set a passphrase, you can
send encrypted ones instead:

```bash
export TELEGRAM_SECRET="a passphrase only you know"
python encrypt.py "do the thing"        # prints enc:... — paste it into Telegram
python encrypt.py --decrypt "enc:..."   # decrypt a reply
```

- Format: `enc:v1:<base64(salt 16 bytes | iv 12 bytes | AES-GCM ciphertext+tag)>`,
  key = PBKDF2-HMAC-SHA256(passphrase, salt, 100,000 iterations, 32 bytes).
  Same format in `telegram_bot.py`, `encrypt.py`, and `encrypt.html`.
- Only messages starting with `enc:` are decrypted; plain messages still
  work when a secret is set. Replies to encrypted messages are encrypted
  the same way (also prefixed `enc:`).
- `encrypt.html` is an offline page (no network, no libraries) with two
  boxes: encrypt a prompt, decrypt a reply. It uses WebCrypto (PBKDF2 +
  AES-GCM, same salt/iv layout and iteration count) and was verified
  against the Python implementation with node v24. Open it directly or
  with `python -m http.server` — WebCrypto needs a secure context.

## Safety rules (enforced, not just agreed)

- Agent writes only inside `tools/` — enforced in `write_file.py`.
- A new tool is never imported into the agent's own process until it passes `py_compile` + a fresh-process import.
- Core changes (`agent.py`, `supervisor.py`) need a diff the user approves by hand.
- `git commit` before any test run of self-written code.
