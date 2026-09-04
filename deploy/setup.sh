#!/usr/bin/env bash
# First-time setup on a fresh Ubuntu EC2 instance. Run it from the repo root
# after the code has arrived (deploy/README.md step 3):
#
#   bash deploy/setup.sh
#
# Idempotent: safe to re-run after pulling new code.
set -euo pipefail

if [[ ! -f requirements.txt || ! -d app ]]; then
    echo "Run this from the repo root (requirements.txt and app/ not found)." >&2
    exit 1
fi

sudo apt-get update
sudo apt-get install -y python3-venv

if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# data/ is gitignored, so a fresh clone or archive upload arrives without it.
mkdir -p data

# Install the unit with this user and this path baked in.
sed -e "s|__USER__|$USER|g" -e "s|__REPO__|$PWD|g" \
    deploy/spending-tracker.service | sudo tee /etc/systemd/system/spending-tracker.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now spending-tracker
sudo systemctl restart spending-tracker

sleep 1
systemctl --no-pager status spending-tracker
echo
echo "Done. The app listens on 127.0.0.1:8000 (this machine only)."
echo "From your PC: deploy\\tunnel.ps1 -InstanceIp <public-ip>, then open http://localhost:8000"
