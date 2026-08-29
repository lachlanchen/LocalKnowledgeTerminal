#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="${LKT_SOURCE:-/home/lachlan/LocalKnowledgeTerminal/source}"
cd "$SOURCE_DIR"
git fetch origin main
git merge --ff-only origin/main
python3 -m unittest discover -s tests -v
python3 -m compileall -q lkt tests
sudo env LKT_SOURCE="$SOURCE_DIR" "$SOURCE_DIR/scripts/install_services.sh" --restart
sudo systemctl --no-pager --full status \
  lkt-llm.service lkt-web.service lkt-worker.service
