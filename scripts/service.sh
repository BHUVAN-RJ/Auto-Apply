#!/bin/bash
# Keep the Autopilot server up: a launchd agent that starts it at login and
# starts it again within seconds of it dying.
#
#   scripts/service.sh install    write the agent and load it
#   scripts/service.sh uninstall  unload it and remove the file
#   scripts/service.sh status     is it loaded, is it answering
#   scripts/service.sh stop       stop the server and leave it stopped
#   scripts/service.sh start      start it again
#
# The agent supervises the server only. Chrome and the injector are not in
# it on purpose: Chrome holds the person's logins and their open forms, and
# a supervisor that reopened it would fight the rule that the browser stays
# on the finished form until a person is done with it.
#
# The clone's path is written into the agent at install time, the way
# make_app.sh writes it into Autopilot.app, so nothing machine-specific is
# ever committed - the repository is public.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="local.autopilot.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

# Where the logs go is where the launcher puts them, so one tail covers both.
if [ -z "${AUTOPILOT_HOME:-}" ]; then
  APP_HOME="$HOME/Library/Application Support/Autopilot"
  if [ -e "$REPO/data/queue.json" ] && [ ! -e "$APP_HOME/data/queue.json" ]; then
    AUTOPILOT_HOME="$REPO"
  else
    AUTOPILOT_HOME="$APP_HOME"
  fi
fi
LOGS="$AUTOPILOT_HOME/logs"

loaded() { launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; }

install_agent() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOGS"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <!-- A login shell, so Homebrew's PATH is there for lualatex, git and lsof;
       launchd's own PATH has none of them. -->
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-l</string>
    <string>-c</string>
    <string>exec '$REPO/scripts/autopilot' serve</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <!-- Restart when it dies or is killed, and stay down when it exits 0,
       which is how 'serve' says another server already holds the port. -->
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- A server that must answer a keypress, not a batch job: App Nap leaves
       Interactive processes alone. -->
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/server.log</string>
  <key>StandardErrorPath</key><string>$LOGS/server.log</string>
</dict>
</plist>
PLIST
  # Any copy already running by hand is stopped first, so that the agent's
  # copy gets the port and the two of them never split the data folder.
  "$REPO/scripts/autopilot" stop >/dev/null 2>&1 || true
  launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID_NUM" "$PLIST"
  launchctl enable "gui/$UID_NUM/$LABEL"
  echo "installed $PLIST"
  status
}

uninstall_agent() {
  launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
  rm -f "$PLIST"
  echo "removed $PLIST"
}

status() {
  local port; port="$(cat "$AUTOPILOT_HOME/run/port" 2>/dev/null || echo 8787)"
  for _ in $(seq 1 20); do
    curl -fsS -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
  loaded && echo "agent   loaded" || echo "agent   not loaded"
  if curl -fsS -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    echo "server  answering on $port"
  else
    echo "server  not answering on $port; tail $LOGS/server.log"
  fi
  echo "data    $AUTOPILOT_HOME"
}

case "${1:-status}" in
  install) install_agent ;;
  uninstall) uninstall_agent ;;
  status) status ;;
  stop) launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true; echo "stopped; scripts/service.sh start brings it back" ;;
  start) launchctl bootstrap "gui/$UID_NUM" "$PLIST"; status ;;
  *) echo "usage: $0 [install|uninstall|status|stop|start]" >&2; exit 2 ;;
esac
