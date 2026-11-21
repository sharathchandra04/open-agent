#!/usr/bin/env bash
# setup_open_agent.sh — one-command setup for the open-agent on WSL Ubuntu.
#
# Run it once on your Windows laptop, inside WSL (Ubuntu):
#   bash setup_open_agent.sh
# It does everything: clones/updates the repo, creates a Python venv,
# installs dependencies, asks for your free Groq API key once, runs the
# tests and the offline demo, and prints the commands to start the agent.
# Nothing is started in the background by this script — you start the
# agent yourself with the commands it prints at the end.
set -euo pipefail

REPO_URL="https://github.com/sharathchandra04/open-agent.git"
REPO_DIR="$HOME/open-agent"

echo "== open-agent setup =="

# If this script is run from inside a copy of the repo, use that copy.
if [ -f "./agent.py" ] && [ -f "./supervisor.py" ]; then
  REPO_DIR="$(pwd)"
  echo "Using the repo in this folder: $REPO_DIR"
else
  if [ -d "$REPO_DIR/.git" ]; then
    echo "Updating existing repo at $REPO_DIR ..."
    git -C "$REPO_DIR" pull --ff-only
  else
    echo "Cloning $REPO_URL to $REPO_DIR ..."
    git clone "$REPO_URL" "$REPO_DIR"
  fi
fi
cd "$REPO_DIR"

# Python + venv ------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found, installing it (needs sudo)..."
  sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
fi
python3 --version

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment .venv ..."
  python3 -m venv .venv
fi
echo "Installing dependencies (litellm + cryptography) ..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# Free Groq API key --------------------------------------------------------
# agent.py reads GROQ_API_KEY from the environment, or from .env
# (which is git-ignored, so your key never goes to GitHub).
if [ -n "${GROQ_API_KEY:-}" ]; then
  printf 'GROQ_API_KEY=%s\n' "$GROQ_API_KEY" > .env
  chmod 600 .env
  echo "Saved GROQ_API_KEY from your environment to .env"
elif [ -f ".env" ] && grep -q '^GROQ_API_KEY=.\+' .env 2>/dev/null; then
  echo "Found GROQ_API_KEY in .env, keeping it."
else
  echo ""
  echo "Get a FREE Groq key (no card needed): sign up at"
  echo "  https://console.groq.com  ->  API Keys  ->  Create API Key"
  echo "Paste it here (it will not be shown on screen). Press Enter"
  echo "to skip — the offline demo below still works, but live"
  echo "agent runs need the key."
  printf "GROQ_API_KEY: "
  read -r -s KEY || true
  echo ""
  if [ -n "${KEY:-}" ]; then
    printf 'GROQ_API_KEY=%s\n' "$KEY" > .env
    chmod 600 .env
    echo "Saved to .env (git-ignored, never pushed to GitHub)."
  else
    echo "No key entered. You can add it later with:"
    echo "  echo 'GROQ_API_KEY=your-key-here' > .env"
  fi
fi

# prompts.txt --------------------------------------------------------------
if [ ! -f "prompts.txt" ]; then
  cat > prompts.txt <<'EOF'
# One prompt per line. Lines starting with # and blank lines are skipped.
Say hello with the hello tool.
EOF
  echo "Created a starter prompts.txt — edit it with your own prompts."
else
  echo "prompts.txt already exists — edit it with your own prompts."
fi

# Verify -------------------------------------------------------------------
echo ""
echo "Running tests ..."
.venv/bin/python test_agent.py
.venv/bin/python test_run_prompts.py
echo ""
echo "Running the offline demo (no API key, no network) ..."
.venv/bin/python agent.py --demo

echo ""
echo "== Setup done =="
echo ""
echo "START THE AGENT — one task at a time:"
echo "  cd $REPO_DIR"
echo "  source .venv/bin/activate"
echo "  python supervisor.py --task \"Say hello with the hello tool.\""
echo ""
echo "START THE AGENT — many prompts from a file, in serial order:"
echo "  cd $REPO_DIR"
echo "  source .venv/bin/activate"
echo "  # edit prompts.txt first — one prompt per line, 10 prompts = 10 lines"
echo "  python run_prompts.py prompts.txt"
echo ""
echo "Logs for a prompts-file run go to runs/<date-time>/prompt-NN.log"
echo "with a summary.txt in the same folder."
echo "Keep WSL running while the agent works — if Windows sleeps,"
echo "the agent stops."
