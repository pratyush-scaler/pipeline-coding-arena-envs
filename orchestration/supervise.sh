#!/usr/bin/env bash
# Keep exactly one driver running; restart it when its log has been silent for 25 minutes (a hung coder ssh once
# froze a driver for an hour). Started by ./start; stop it with ./start --stop.
R="$(cd "$(dirname "$0")/.." && pwd)"
cd "$R"
mkdir -p logs
PY="$R/.venv/bin/python"
interval() { "$PY" -c 'from orchestration import config; print(config.load()["interval"])'; }
start_driver() {
  env -u ANTHROPIC_AUTH_TOKEN PYTHONDONTWRITEBYTECODE=1 nohup "$PY" -u orchestration/driver.py --loop --package \
      --interval "$(interval)" >> logs/driver.out 2>&1 &
  echo "$(date '+%F %T') started driver $!"
}
while true; do
  P=$(pgrep -f "[Pp]ython[0-9.]* -u orchestration/drive[r].py --loop")   # the interpreter, not a shell naming it
  if [ -z "$P" ]; then
    start_driver
  else
    age=$("$PY" -c 'import os, time; p = "logs/driver.log"; print(int(time.time() - os.path.getmtime(p)) if os.path.exists(p) else 0)')
    if [ "$age" -gt 1500 ]; then
      echo "$(date '+%F %T') driver $P silent ${age}s: restarting"
      pkill -P "$P"; kill "$P"; sleep 3; kill -9 "$P" 2>/dev/null
      start_driver
    fi
  fi
  sleep 300
done
