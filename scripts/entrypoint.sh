#!/usr/bin/env bash
set -euo pipefail

# Load environment file if present (do not fail if missing)
if [ -f /app/.env ]; then
  set -a
  # shellcheck source=/dev/null
  source /app/.env
  set +a
fi

# Default command: start the bot runner module. If arguments are provided, run them instead.
if [ "$#" -gt 0 ]; then
  exec "$@"
else
  echo "Starting ogatt_booker.bot_runner"
  exec python -m ogatt_booker.bot_runner
fi
