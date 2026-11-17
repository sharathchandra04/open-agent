"""Tests for telegram_bot.py / encrypt.py / encrypt.html.

Offline only: the Telegram API is never hit — api_call is replaced
with a fake. Encryption tests need the `cryptography` package
(`pip install -r requirements.txt`); they SKIP cleanly without it.
The encrypt.html cross-check needs node; it SKIPs cleanly without it.

Run:  python test_telegram_bot.py   (or: python -m pytest test_telegram_bot.py -v)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import telegram_bot as tb  # noqa: E402

CONFIG = {"token": "fake-token", "chat_id": "111", "user_id": "", "secret": ""}


def _has_crypto():
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _update(update_id, chat_id="111", text="hello", user_id="222"):
    upd = {"update_id": update_id}
    if text is not None or chat_id is not None:
        msg = {}
        if chat_id is not None:
            msg["chat"] = {"id": int(chat_id), "type": "private"}
        if text is not None:
            msg["text"] = text
        if user_id is not None:
            msg["from"] = {"id": int(user_id)}
        upd["message"] = msg
    return upd


class FakeApi:
    """Stand-in for telegram_bot.api_call. Records calls; canned getUpdates."""

    def __init__(self, updates=None):
        self.calls = []
        self.updates = updates or []

    def __call__(self, token, method, params=None):
        self.calls.append((method, params or {}))
        assert token == "fake-token", "token leaked into a fake call?"
        if method == "getUpdates":
            return list(self.updates)
        return {"message_id": 1}

    def sent_texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


def _with_fake_api(fake):
    old = tb.api_call
    tb.api_call = fake
    return old


# ---------------------------------------------------------------- allowlist
def test_allowlist_accepts_configured_chat():
    fake = FakeApi()
    old = _with_fake_api(fake)
    try:
        seen = []
        tb.handle_update(_update(1, text="do the thing"),
                         CONFIG, runner=lambda p: seen.append(p) or (True, "done it"))
        assert seen == ["do the thing"]
        assert fake.sent_texts() == ["done it"]
    finally:
        tb.api_call = old


def test_allowlist_rejects_other_chat():
    fake = FakeApi()
    old = _with_fake_api(fake)
    try:
        seen = []
        tb.handle_update(_update(1, chat_id="999", text="do evil"),
                         CONFIG, runner=lambda p: seen.append(p) or (True, "x"))
        assert seen == []  # runner never called
        assert fake.sent_texts() == []  # nothing sent back either
    finally:
        tb.api_call = old


def test_user_id_allowlist_alternative():
    fake = FakeApi()
    old = _with_fake_api(fake)
    try:
        cfg = {"token": "fake-token", "chat_id": "", "user_id": "222", "secret": ""}
        seen = []
        tb.handle_update(_update(1, chat_id="999", text="hi"),
                         cfg, runner=lambda p: seen.append(p) or (True, "ok"))
        assert seen == ["hi"]
    finally:
        tb.api_call = old


# ---------------------------------------------------------------- polling / offsets
def test_offset_advances_past_processed_updates():
    updates = [
        _update(10, text="one"),
        _update(11, chat_id="999", text="ignored"),
        {"update_id": 12, "message": {"chat": {"id": 111}}},  # no text
        {"bogus": True},  # malformed, no update_id: ignored, cannot advance
    ]
    fake = FakeApi(updates)
    old = _with_fake_api(fake)
    try:
        seen = []
        # handle_update is called by poll_once; swap the runner via
        # a patched module-level default by wrapping run_agent.
        orig_run = tb.run_agent
        tb.run_agent = lambda p, **kw: seen.append(p) or (True, "ok")
        try:
            offset = tb.poll_once(CONFIG, None)
        finally:
            tb.run_agent = orig_run
        assert offset == 13, offset  # past the last good update_id
        assert seen == ["one"]
    finally:
        tb.api_call = old


def test_malformed_updates_ignored():
    fake = FakeApi([{}, {"update_id": 5}, {"update_id": 6, "message": "not-a-dict"}])
    old = _with_fake_api(fake)
    try:
        seen = []
        orig_run = tb.run_agent
        tb.run_agent = lambda p, **kw: seen.append(p) or (True, "ok")
        try:
            offset = tb.poll_once(CONFIG, 0)
        finally:
            tb.run_agent = orig_run
        assert offset == 7
        assert seen == []
    finally:
        tb.api_call = old


def test_non_text_and_empty_ignored():
    fake = FakeApi()
    old = _with_fake_api(fake)
    try:
        seen = []
        runner = lambda p: seen.append(p) or (True, "ok")
        tb.handle_update({"update_id": 1, "message": {"chat": {"id": 111},
                                                     "photo": [{"file_id": "x"}]}},
                         CONFIG, runner=runner)
        tb.handle_update(_update(2, text="   "), CONFIG, runner=runner)
        tb.handle_update(_update(3, text=None), CONFIG, runner=runner)
        assert seen == []
        assert fake.sent_texts() == []
    finally:
        tb.api_call = old


# ---------------------------------------------------------------- splitting
def test_split_message_4096():
    assert tb.split_message("short") == ["short"]
    assert tb.split_message("x" * 4096) == ["x" * 4096]
    text = "line\n" * 1500  # 7500 chars, newlines everywhere
    chunks = tb.split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text
    hard = "y" * 5000  # no newlines at all: hard split
    chunks = tb.split_message(hard)
    assert len(chunks) == 2
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == hard


# ---------------------------------------------------------------- agent handoff
def test_agent_handoff_parses_fake_stdout():
    out = ("tool hello({}) -> Hello!\n"
           "tool run_shell({'command': 'pwd'}) -> /tmp\n"
           "the final answer\nsecond line\n")
    assert tb.extract_final_answer(out) == "the final answer\nsecond line"
    # No tool lines: whole output is the answer.
    assert tb.extract_final_answer("just an answer\n") == "just an answer"
    # Housekeeping lines (e.g. resume notice) are dropped.
    out2 = ("agent: resumed task 'old' from state.json\n"
            "tool hello({}) -> Hello!\n"
            "answer after resume\n")
    assert tb.extract_final_answer(out2) == "answer after resume"
    # Empty output: empty answer (caller substitutes a placeholder).
    assert tb.extract_final_answer("") == ""


def _fake_run_factory(results):
    """results: list of (returncode, stdout, stderr). Returns (fake, calls)."""
    calls = []

    def fake(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        calls.append({"cmd": cmd, "cwd": cwd})
        code, out, err = results.pop(0)
        return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)

    return fake, calls


def test_exit_42_reruns_then_succeeds():
    fake, calls = _fake_run_factory([
        (42, "", ""),  # restart 1: state.json saved by the agent
        (42, "", ""),  # restart 2
        (0, "tool hello({}) -> Hello!\nall done\n", ""),
    ])
    ok, answer = tb.run_agent("do it", agent_dir=HERE, _run=fake)
    assert ok and answer == "all done", (ok, answer)
    assert len(calls) == 3
    assert calls[0]["cmd"][:2] == [sys.executable, "agent.py"]
    assert calls[0]["cmd"][2:] == ["--task", "do it"]


def test_exit_42_gives_up_after_max_runs():
    fake, calls = _fake_run_factory([(42, "", "")] * 10)
    ok, answer = tb.run_agent("do it", agent_dir=HERE, _run=fake)
    assert not ok
    assert len(calls) == tb.MAX_AGENT_RUNS  # 1 fresh + 3 re-runs
    assert "42" in answer


def test_agent_failure_reports_tail():
    fake, calls = _fake_run_factory([(1, "some output\n", "traceback here\n")])
    ok, answer = tb.run_agent("do it", agent_dir=HERE, _run=fake)
    assert not ok
    assert "exit 1" in answer and "some output" in answer


def test_stale_state_removed_before_fresh_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w") as f:
            json.dump({"messages": [], "task": "OLD TASK"}, f)

        def fake(cmd, cwd=None, capture_output=None, text=None, timeout=None):
            # The stale state must already be gone when the agent starts,
            # or agent.py would resume OLD TASK instead of the new prompt.
            assert not os.path.exists(state_path), "stale state.json not removed"
            assert cwd == tmp
            return types.SimpleNamespace(returncode=0, stdout="fresh answer\n",
                                         stderr="")

        ok, answer = tb.run_agent("new prompt", agent_dir=tmp, _run=fake)
        assert ok and answer == "fresh answer"


# ---------------------------------------------------------------- encryption
def test_encryption_round_trip():
    if not _has_crypto():
        print("SKIP test_encryption_round_trip: cryptography not installed")
        return
    secret = "test-passphrase"
    text = "do the thing — with unicode ✓"
    enc = tb.encrypt_text(text, secret)
    assert enc.startswith("enc:v1:")
    assert tb.decrypt_text(enc, secret) == text
    # Same plaintext encrypts differently each time (random salt/iv).
    assert tb.encrypt_text(text, secret) != enc


def test_encryption_wrong_secret_fails():
    if not _has_crypto():
        print("SKIP test_encryption_wrong_secret_fails: cryptography not installed")
        return
    enc = tb.encrypt_text("secret stuff", "right-secret")
    try:
        tb.decrypt_text(enc, "wrong-secret")
        raise AssertionError("wrong secret should fail")
    except ValueError:
        pass
    for bad in ("hello", "enc:v9:AAAA", "enc:v1:not-base64!!", "enc:v1:AA=="):
        try:
            tb.decrypt_text(bad, "right-secret")
            raise AssertionError(f"bad input should fail: {bad!r}")
        except ValueError:
            pass


def test_plain_message_works_when_secret_set():
    if not _has_crypto():
        print("SKIP test_plain_message_works_when_secret_set: cryptography not installed")
        return
    fake = FakeApi()
    old = _with_fake_api(fake)
    try:
        cfg = dict(CONFIG, secret="s3cret")
        seen = []
        tb.handle_update(_update(1, text="plain prompt"),
                         cfg, runner=lambda p: seen.append(p) or (True, "plain reply"))
        assert seen == ["plain prompt"]  # not decrypted, passed through
        assert fake.sent_texts() == ["plain reply"]  # not encrypted either
    finally:
        tb.api_call = old


def test_encrypted_message_round_trips_through_bot():
    if not _has_crypto():
        print("SKIP test_encrypted_message_round_trips_through_bot: cryptography not installed")
        return
    fake = FakeApi()
    old = _with_fake_api(fake)
    try:
        cfg = dict(CONFIG, secret="s3cret")
        enc_in = tb.encrypt_text("hidden prompt", "s3cret")
        seen = []
        tb.handle_update(_update(1, text=enc_in),
                         cfg, runner=lambda p: seen.append(p) or (True, "hidden reply"))
        assert seen == ["hidden prompt"]
        sent = fake.sent_texts()
        assert len(sent) == 1 and sent[0].startswith("enc:v1:")
        assert tb.decrypt_text(sent[0], "s3cret") == "hidden reply"
    finally:
        tb.api_call = old


def test_encrypted_message_bad_decrypt_gets_error_reply():
    if not _has_crypto():
        print("SKIP test_encrypted_message_bad_decrypt_gets_error_reply: cryptography not installed")
        return
    fake = FakeApi()
    old = _with_fake_api(fake)
    try:
        cfg = dict(CONFIG, secret="s3cret")
        seen = []
        tb.handle_update(_update(1, text="enc:v1:AAAA"),
                         cfg, runner=lambda p: seen.append(p) or (True, "x"))
        assert seen == []  # agent never ran
        assert len(fake.sent_texts()) == 1
        assert "decrypt" in fake.sent_texts()[0].lower()
    finally:
        tb.api_call = old


# ---------------------------------------------------------------- config
def test_load_config_requires_token_and_chat():
    old = {k: os.environ.get(k) for k in
           ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_USER_ID")}
    try:
        for k in old:
            os.environ.pop(k, None)
        try:
            tb.load_config()
            raise AssertionError("expected SystemExit with no token")
        except SystemExit as exc:
            assert "TELEGRAM_BOT_TOKEN" in str(exc)
        os.environ["TELEGRAM_BOT_TOKEN"] = "t"
        try:
            tb.load_config()
            raise AssertionError("expected SystemExit with no chat/user id")
        except SystemExit as exc:
            assert "TELEGRAM_CHAT_ID" in str(exc)
        os.environ["TELEGRAM_CHAT_ID"] = "111"
        assert tb.load_config()["token"] == "t"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------- encrypt.html
_NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const { webcrypto } = require('node:crypto');
const { TextEncoder, TextDecoder } = require('node:util');
const html = fs.readFileSync(process.argv[2], 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('no script found in encrypt.html'); process.exit(1); }
const el = () => ({ addEventListener() {}, value: '', textContent: '' });
const sandbox = {
  document: { getElementById: el },
  console,
  crypto: webcrypto,
  TextEncoder, TextDecoder,
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
  btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
};
vm.createContext(sandbox);
vm.runInContext(m[1], sandbox);
(async () => {
  const [enc, secret, plain] = process.argv.slice(3);
  const back = await sandbox.decryptText(enc, secret);
  if (back !== plain) {
    console.error('node decrypt mismatch: ' + JSON.stringify(back));
    process.exit(1);
  }
  console.log(await sandbox.encryptText(plain, secret));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_encrypt_html_matches_python():
    if not _has_crypto():
        print("SKIP test_encrypt_html_matches_python: cryptography not installed")
        return
    node = shutil.which("node")
    if not node:
        print("SKIP test_encrypt_html_matches_python: node not installed")
        return
    secret = "html-test-secret"
    plain = "cross-check from python ✓"
    enc = tb.encrypt_text(plain, secret)
    with tempfile.TemporaryDirectory() as tmp:
        harness = os.path.join(tmp, "harness.js")
        with open(harness, "w") as f:
            f.write(_NODE_HARNESS)
        proc = subprocess.run([node, harness,
                               os.path.join(HERE, "encrypt.html"),
                               enc, secret, plain],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        enc2 = proc.stdout.strip()
        assert enc2.startswith("enc:v1:")
        # And the page's encryption must decrypt on the Python side.
        assert tb.decrypt_text(enc2, secret) == plain


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
