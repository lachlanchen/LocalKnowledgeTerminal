#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="${LKT_SOURCE:-/home/lachlan/LocalKnowledgeTerminal/source}"
cd "$SOURCE_DIR"
git fetch origin main
git merge --ff-only origin/main
python3 -m unittest discover -s tests -v
python3 -m compileall -q lkt tests
sudo install -o root -g root -m 0644 systemd/lkt-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable lkt-worker.service
sudo systemctl restart lkt-llm.service lkt-web.service lkt-worker.service
sudo systemctl --no-pager --full status lkt-llm.service lkt-web.service lkt-worker.service
