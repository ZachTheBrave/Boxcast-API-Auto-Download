#!/usr/bin/env bash
set -e

########################################
# Config – CHANGE THESE FOR YOUR SETUP
########################################

# Your GitHub repo URL for this project
REPO_URL="https://github.com/ZachTheBrave/Boxcast-API-Auto-Download.git"

# Where you want the app to live on the Pi/server
APP_DIR="/home/zachariah/Church Scripts/"

# Name of the main Python script inside the repo
MAIN_SCRIPT="Church Autodownload.py"

# Python binary to use
PYTHON_BIN="python3"

########################################
# Helper
########################################

green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*"; }

########################################
# 1. Install system packages
########################################

green "==> Updating apt package list..."
sudo apt-get update -y

green "==> Installing git, Python, venv support, pip, and vim (if needed)..."
sudo apt-get install -y git "$PYTHON_BIN" python3-venv python3-pip vim

########################################
# 2. Clone or update the repo
########################################

if [ -d "$APP_DIR/.git" ]; then
  green "==> Repo already exists at $APP_DIR, pulling latest changes..."
  cd "$APP_DIR"
  git pull --ff-only || yellow "git pull failed (maybe local changes). Continuing with existing code."
else
  green "==> Cloning repo into $APP_DIR..."
  sudo mkdir -p "$APP_DIR"
  sudo chown "$(whoami)":"$(whoami)" "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

########################################
# 3. Create venv and install requirements
########################################

VENV_DIR="$APP_DIR/venv"

if [ ! -d "$VENV_DIR" ]; then
  green "==> Creating Python virtual environment in $VENV_DIR..."
  $PYTHON_BIN -m venv "$VENV_DIR"
else
  green "==> Virtual environment already exists at $VENV_DIR"
fi

# Activate venv
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

green "==> Upgrading pip..."
pip install --upgrade pip

if [ -f "requirements.txt" ]; then
  green "==> Installing Python dependencies from requirements.txt..."
  pip install -r requirements.txt
else
  yellow "requirements.txt not found in $APP_DIR – skipping dependency install."
fi

########################################
# 4. Permissions & quick summary
########################################

sudo chown -R "$(whoami)":"$(whoami)" "$APP_DIR"

green ""
green "========================================"
green " Cold start complete!"
green "========================================"
echo
echo "Repository directory:  $APP_DIR"
echo "Virtualenv:           $VENV_DIR"
echo
echo "Next steps (run these manually):"
echo
echo "  cd \"$APP_DIR\""
echo "  source venv/bin/activate"
echo "  python create_vault.py"
echo "    (enter BoxCast client_id / client_secret, Gmail info, and notification emails)"
echo
echo "Then to run the downloader manually:"
echo
echo "  cd \"$APP_DIR\""
echo "  source venv/bin/activate"
echo "  python \"$MAIN_SCRIPT\""
echo
green 'You can later turn this into a systemd service / cron job if you want it fully automated.'
