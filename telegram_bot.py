"""Telegram chat front-end for the agent.

Runs on the laptop next to agent.py. The user chats with the bot on
Telegram; each message becomes a task for the agent (subprocess
`python agent.py --task <prompt>`), and the agent's final answer
comes back as the reply.

Free, no public IP needed: the bot only makes OUTBOUND https calls
to api.telegram.org (long polling via getUpdates), so it works
behind Wi-Fi / NAT. No port forwarding, no tunnel.

Stdlib only (urllib) except the OPTIONAL encryption layer, which
uses the `cryptography` package — see encrypt.py / encrypt.html.

Config — env vars only, never hardcoded, never printed:
  TELEGRAM_BOT_TOKEN   required — from @BotFather on Telegram.
  TELEGRAM_CHAT_ID     required — allowlist: only this chat's
                       messages are accepted. Find yours: message
                       the bot once and read the log line
                       "ignored message from chat_id=...".
  TELEGRAM_USER_ID     optional — allowlist by sender id instead of
                       (or as well as) chat id.
  TELEGRAM_SECRET      optional — passphrase for the enc: layer.
                       Plain messages still work when it is set;
                       only messages starting with `enc:` are
                       decrypted, and replies to them are encrypted.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = "state.json"
RESTART_CODE = 42
# 1 fresh run + up to 3 re-runs when the agent exits 42 (it resumes
# from state.json each time, so the same task continues, not restarts).
MAX_AGENT_RUNS = 4
AGENT_TIMEOUT = 300  # seconds per agent run
MAX_MESSAGE_LENGTH = 4096  # Telegram's per-message limit
POLL_TIMEOUT = 30  # getUpdates long-poll seconds
TAIL_CHARS = 1500  # how much of a failed run's output to report back

# ---------------------------------------------------------------- crypto
# Optional layer, format shared with encrypt.py and encrypt.html:
#   enc:v1:<base64(salt 16 bytes | iv 12 bytes | AES-GCM ciphertext+tag)>
#   key = PBKDF2-HMAC-SHA256(passphrase, salt, 100_000 iterations, 32 bytes)
# `cryptography` is imported lazily, so the bot runs without it when
# no TELEGRAM_SECRET is set.
ENC_PREFIX = "enc:"
ENC_VERSION = "v1"
_PBKDF2_ITERATIONS = 100_000
_SALT_LEN = 16
_IV_LEN = 12


def _derive_key(passphrase, salt):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"),
                               salt, _PBKDF2_ITERATIONS, dklen=32)


def encrypt_text(plaintext, passphrase):
    """Encrypt to the enc:v1:... format. Needs `cryptography`."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(_SALT_LEN)
    iv = os.urandom(_IV_LEN)
    key = _derive_key(passphrase, salt)
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return (f"{ENC_PREFIX}{ENC_VERSION}:"
            + base64.b64encode(salt + iv + ct).decode("ascii"))


def decrypt_text(enc_string, passphrase):
    """Decrypt an enc:v1:... string. Raises ValueError on bad input."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    if not enc_string.startswith(ENC_PREFIX):
        raise ValueError("not an enc: message")
    rest = enc_string[len(ENC_PREFIX):]
    version, _, b64 = rest.partition(":")
    if version != ENC_VERSION or not b64:
        raise ValueError("unsupported enc: version or bad format")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - bad base64 is a format error
        raise ValueError("bad base64 in enc: message") from exc
    if len(raw) < _SALT_LEN + _IV_LEN + 1:
        raise ValueError("enc: message too short")
    salt, iv, ct = raw[:_SALT_LEN], raw[_SALT_LEN:_SALT_LEN + _IV_LEN], raw[_SALT_LEN + _IV_LEN:]
    key = _derive_key(passphrase, salt)
    try:
        pt = AESGCM(key).decrypt(iv, ct, None)
    except InvalidTag as exc:
        raise ValueError("decryption failed (wrong secret?)") from exc
    return pt.decode("utf-8")


# ---------------------------------------------------------------- Telegram API
def api_call(token, method, params=None):
    """One POST to the Bot API. Returns the parsed `result` field.

    The token is in the URL by Telegram's design; it is never logged
    or included in any error message.
    """
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        # Long polling (getUpdates, timeout=30) needs a longer socket
        # timeout than the poll itself.
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 30) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram API {method} failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram API {method} failed: {exc.reason}") from exc
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {body.get('description')}")
    return body["result"]


def send_message(token, chat_id, text):
    return api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})


def send_chat_action(token, chat_id, action="typing"):
    try:
        api_call(token, "sendChatAction", {"chat_id": chat_id, "action": action})
    except Exception:  # noqa: BLE001 - typing indicator is best-effort
        pass


def fetch_updates(token, offset):
    params = {"timeout": POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    result = api_call(token, "getUpdates", params)
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------- replies
def split_message(text, limit=MAX_MESSAGE_LENGTH):
    """Split a long reply into Telegram-sized chunks, in order.

    Cuts at the last newline within the limit when possible, else
    hard-splits. Chunks join back to the original text exactly.
    """
    if len(text) <= limit:
        return [text]
    chunks = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


# ---------------------------------------------------------------- agent handoff
# agent.py prints one line per tool call:
#   tool <name>({args}) -> <result>
# and prints the final answer with a bare print() when the model
# makes no tool call. So the final answer is everything printed
# AFTER the last tool line, minus the agent's own housekeeping
# lines (e.g. "agent: resumed task ..."). If there are no tool
# lines, the answer is the whole output minus housekeeping lines.
_TOOL_LINE = re.compile(r"^tool \S+\(.*\) -> ")


def extract_final_answer(stdout):
    """Pull the agent's final answer out of its printed output."""
    lines = stdout.splitlines()
    last_tool = -1
    for i, line in enumerate(lines):
        if _TOOL_LINE.match(line):
            last_tool = i
    tail = lines[last_tool + 1:] if last_tool >= 0 else lines
    tail = [ln for ln in tail if not ln.startswith("agent: ")]
    return "\n".join(tail).strip()


def _tail(text, limit=TAIL_CHARS):
    return text[-limit:] if len(text) > limit else text


def run_agent(prompt, timeout=AGENT_TIMEOUT, agent_dir=None, _run=None):
    """Run agent.py on one prompt as a subprocess.

    Returns (ok, answer). Before the fresh run, any stale state.json
    left by a previous crashed run is removed — otherwise agent.py
    would resume the OLD task instead of this prompt. On exit 42 the
    agent saved state.json and wants a restart, so it is re-run
    (up to MAX_AGENT_RUNS total) and resumes from that state.
    `_run` is a test seam (defaults to subprocess.run).
    """
    agent_dir = agent_dir or HERE
    run = _run or subprocess.run
    state_path = os.path.join(agent_dir, STATE_FILE)
    if os.path.exists(state_path):
        os.remove(state_path)
    last_out = ""
    for attempt in range(1, MAX_AGENT_RUNS + 1):
        try:
            proc = run([sys.executable, "agent.py", "--task", prompt],
                       cwd=agent_dir, capture_output=True, text=True,
                       timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") + (exc.stderr or "")
            return False, (f"Agent timed out after {timeout}s. "
                           f"Last output:\n{_tail(out)}")
        last_out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            answer = extract_final_answer(proc.stdout or "")
            return True, answer or "(agent finished but printed no answer)"
        if proc.returncode == RESTART_CODE:
            print(f"telegram_bot: agent exited 42, re-running "
                  f"({attempt}/{MAX_AGENT_RUNS})")
            continue
        return False, (f"Agent failed (exit {proc.returncode}). "
                       f"Last output:\n{_tail(last_out)}")
    return False, (f"Agent kept asking to restart (exit 42 "
                   f"{MAX_AGENT_RUNS} times); giving up. "
                   f"Last output:\n{_tail(last_out)}")


# ---------------------------------------------------------------- allowlist
# Chat ids we have already logged as ignored, so the log is not
# spammed by every message from a stranger.
_logged_ignored_chat_ids = set()


def is_allowed(update, config):
    """True only if the message is from the allowlisted chat/user."""
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    user_id = str((msg.get("from") or {}).get("id", ""))
    if config.get("chat_id") and chat_id == str(config["chat_id"]):
        return True
    if config.get("user_id") and user_id == str(config["user_id"]):
        return True
    return False


def _typing_loop(token, chat_id, stop):
    """Re-send the typing indicator while the agent works (it expires
    after a few seconds on Telegram's side). Best-effort only."""
    while not stop.wait(4):
        send_chat_action(token, chat_id, "typing")


def handle_update(update, config, runner=None):
    """Handle one getUpdates item: run the agent, send the reply.

    `runner` defaults to run_agent at call time (not at def time),
    so tests can swap it via telegram_bot.run_agent.
    """
    if runner is None:
        runner = run_agent
    msg = update.get("message") or {}
    if not isinstance(msg, dict):
        return  # malformed message: ignore
    text = msg.get("text")
    # Non-text messages (photo, sticker, ...) and empty text: ignore.
    if not isinstance(text, str) or not text.strip():
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    if not is_allowed(update, config):
        if chat_id not in _logged_ignored_chat_ids:
            _logged_ignored_chat_ids.add(chat_id)
            print(f"telegram_bot: ignored message from chat_id={chat_id} "
                  f"(not in allowlist — set TELEGRAM_CHAT_ID to this to allow it)")
        return

    secret = config.get("secret") or ""
    encrypted = False
    prompt = text
    if secret and text.startswith(ENC_PREFIX):
        try:
            prompt = decrypt_text(text, secret)
            encrypted = True
        except ValueError as exc:
            print(f"telegram_bot: decrypt failed: {exc}")
            send_message(config["token"], chat_id,
                         "Could not decrypt that message (wrong secret or bad format).")
            return

    send_chat_action(config["token"], chat_id, "typing")
    stop_typing = threading.Event()
    typer = threading.Thread(target=_typing_loop,
                             args=(config["token"], chat_id, stop_typing),
                             daemon=True)
    typer.start()
    try:
        ok, answer = runner(prompt)
    finally:
        stop_typing.set()
        typer.join(timeout=1)
    if not ok:
        # Failure note (already includes the tail of the agent output).
        reply_chunks = split_message(answer)
    else:
        reply_chunks = split_message(answer)
        if encrypted and secret:
            # Encrypt each chunk separately, so every message is
            # independently decryptable (its own salt/iv).
            reply_chunks = [encrypt_text(c, secret) for c in reply_chunks]
    for chunk in reply_chunks:
        send_message(config["token"], chat_id, chunk)


def poll_once(config, offset):
    """One getUpdates round. Returns the next offset to use."""
    for update in fetch_updates(config["token"], offset):
        if not isinstance(update, dict) or "update_id" not in update:
            print(f"telegram_bot: ignoring malformed update: {update!r}")
            continue
        handle_update(update, config)
        offset = update["update_id"] + 1
    return offset


# ---------------------------------------------------------------- config + loop
def load_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    user_id = os.environ.get("TELEGRAM_USER_ID", "")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Create a bot with "
                         "@BotFather on Telegram and copy its token.")
    if not chat_id and not user_id:
        raise SystemExit("Set TELEGRAM_CHAT_ID (your chat id — message the "
                         "bot once and read the log line) or TELEGRAM_USER_ID.")
    return {"token": token, "chat_id": chat_id, "user_id": user_id,
            "secret": os.environ.get("TELEGRAM_SECRET", "")}


def main():
    config = load_config()
    print("telegram_bot: polling Telegram. Send a message to the bot; "
          "each one runs as an agent task. Ctrl-C to stop.")
    offset = None
    while True:
        try:
            offset = poll_once(config, offset)
        except KeyboardInterrupt:
            print("telegram_bot: stopping.")
            return 0
        except Exception as exc:  # noqa: BLE001 - poll errors must not kill the bot
            print(f"telegram_bot: poll error: {exc} — retrying in 5s")
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
