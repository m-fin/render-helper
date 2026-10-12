#!/bin/bash
# HomeRender one-step installer for macOS.  Usage: bash install.command [--uninstall]
set -euo pipefail
LABEL=com.mfin.homerender
DEST="$HOME/Library/Application Support/homerender"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/homerender.log"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload -w "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"; rm -rf "$DEST"
  read -r -p "Also delete the token and key from Keychain? [y/N] " a
  if [[ "$a" =~ ^[Yy]$ ]]; then
    security delete-generic-password -s homerender-github >/dev/null 2>&1 || true
    security delete-generic-password -s homerender-hmac  >/dev/null 2>&1 || true
  fi
  echo "HomeRender uninstalled."; exit 0
fi

echo "== HomeRender installer =="
# 1. ffmpeg
FF="$(command -v ffmpeg || true)"
for d in /opt/homebrew/bin /usr/local/bin; do [[ -z "$FF" && -x "$d/ffmpeg" ]] && FF="$d/ffmpeg"; done
if [[ -z "$FF" ]]; then echo "ffmpeg not found. Install it with: brew install ffmpeg   then re-run."; exit 1; fi
FFDIR="$(dirname "$FF")"; echo "ffmpeg: $FF"
PY=/usr/bin/python3; [[ -x "$PY" ]] || PY="$(command -v python3)"
"$PY" -c 'import sys; assert sys.version_info >= (3,8)' || { echo "Need Python 3.8+ (xcode-select --install)"; exit 1; }

# 2. copy files (idempotent overwrite)
mkdir -p "$DEST" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$SRC/rh_worker.py" "$SRC/rh_common.py" "$DEST/"

# 3. secrets -> Keychain (asked only if missing)
if security find-generic-password -s homerender-github -w >/dev/null 2>&1; then
  echo "GitHub token: already in Keychain (keeping it)."
else
  read -r -s -p "Paste the fine-grained GitHub token for m-fin/homerender: " TOK; echo
  [[ -n "$TOK" ]] || { echo "No token entered."; exit 1; }
  security add-generic-password -U -a "$USER" -s homerender-github -w "$TOK"; unset TOK
fi
if security find-generic-password -s homerender-hmac -w >/dev/null 2>&1; then
  echo "HMAC key: already in Keychain (keeping it)."
else
  read -r -s -p "Paste the shared HMAC key, or press Enter to generate one: " KEY; echo
  if [[ -z "$KEY" ]]; then
    KEY="$(openssl rand -hex 32)"
    echo "Generated HMAC key (give it to Grok Bot in your private 1:1 chat as RH_HMAC_KEY; never in a group chat):"
    echo "  $KEY"
  fi
  security add-generic-password -U -a "$USER" -s homerender-hmac -w "$KEY"; unset KEY
fi

# 4. launchd agent (reload if already present)
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DEST/rh_worker.py</string>
    <string>--ffmpeg-dir</string><string>$FFDIR</string></array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG</string><key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
echo "Background worker loaded ($LABEL). Log: $LOG"

# 5. tiny local test render (1s, 64x64)
echo "Running test job..."
if "$PY" "$DEST/rh_worker.py" --ffmpeg-dir "$FFDIR" --selftest; then
  echo "HomeRender installed. PASS"
else
  echo "HomeRender test FAILED. See output above."; exit 1
fi
