#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:99

# Start virtual X server
Xvfb :99 -screen 0 1366x900x24 >/tmp/xvfb.log 2>&1 &

# Optional lightweight WM improves browser usability
fluxbox >/tmp/fluxbox.log 2>&1 &

# Start VNC server for the live Playwright browser session
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 >/tmp/x11vnc.log 2>&1 &

# Start noVNC web client
websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

# Keep the container idle by default so commands can be run manually.
# Example:
#   docker exec -it theatre-ogatt-booker-1 bash
#   python -m ogatt_booker watch --title "Евгений Онегин" --seats 2 --interval 120
# Logs remain visible directly in the terminal where you launch the command.
if [ "$#" -gt 0 ]; then
  exec "$@"
else
  exec bash
fi
