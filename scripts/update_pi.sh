#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="${LKT_SOURCE:-/home/lachlan/LocalKnowledgeTerminal/source}"
cd "$SOURCE_DIR"
git fetch origin main
git merge --ff-only origin/main
python3 -m unittest discover -s tests -v
python3 -m compileall -q lkt tests
sudo systemctl restart lkt-llm.service lkt-web.service
sudo systemctl --no-pager --full status lkt-llm.service lkt-web.service
