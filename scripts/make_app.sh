#!/bin/bash
# Build ~/Applications/Autopilot.app: a small bundle whose only job is to
# run scripts/autopilot from this clone. Built on this Mac, so it carries
# no quarantine flag and Gatekeeper never asks about it; no signing needed.
# Rebuilt by install.sh; run it again if the clone moves.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="${AUTOPILOT_APP:-$HOME/Applications/Autopilot.app}"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Autopilot</string>
  <key>CFBundleDisplayName</key><string>Autopilot</string>
  <key>CFBundleIdentifier</key><string>local.autopilot.launcher</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>$(git -C "$REPO" describe --tags --match 'v*' --always 2>/dev/null || echo dev)</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Autopilot</string>
  <key>CFBundleIconFile</key><string>Autopilot</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# The clone's path is written in at build time; a login shell so Homebrew's
# PATH is there when the app is started from the Dock.
cat > "$APP/Contents/MacOS/Autopilot" <<LAUNCH
#!/bin/zsh -l
exec "$REPO/scripts/autopilot" start
LAUNCH
chmod +x "$APP/Contents/MacOS/Autopilot"

# The icon: the review page's orb, drawn once with the system's own tools.
ICON_SRC="$REPO/scripts/icon.png"
if [ -f "$ICON_SRC" ]; then
  SET="$(mktemp -d)/Autopilot.iconset"
  mkdir -p "$SET"
  for size in 16 32 128 256 512; do
    sips -z $size $size "$ICON_SRC" --out "$SET/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$ICON_SRC" --out "$SET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/Autopilot.icns" || true
fi

echo "  built $APP"
