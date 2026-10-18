#!/usr/bin/env bash
# Extend every configured workspace's stop deadline hourly, so a long batch never loses a workspace mid-run.
R="$(cd "$(dirname "$0")/.." && pwd)"
cd "$R"
while true; do
  for ws in $("$R/.venv/bin/python" -c 'from orchestration import config; print(" ".join(config.workspaces()))'); do
    coder schedule extend "$ws" 10h >/dev/null 2>&1 && echo "$(date '+%H:%M') extended $ws" || echo "$(date '+%H:%M') FAILED $ws"
  done
  sleep 3600
done
