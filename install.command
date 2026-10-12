#!/bin/bash
# Mac Render Helper one-step installer for macOS.  Usage: bash install.command [--uninstall]
set -euo pipefail
LABEL=com.mfin.macrenderhelper
DEST="$HOME/Library/Application Support/macrenderhelper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/macrenderhelper.log"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload -w "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"; rm -rf "$DEST"  # also removes worker_id (re-pairs on reinstall)
  read -r -p "Also delete the token and key from Keychain? [y/N] " a
  if [[ "$a" =~ ^[Yy]$ ]]; then
    security delete-generic-password -s macrenderhelper-github >/dev/null 2>&1 || true
    security delete-generic-password -s macrenderhelper-hmac  >/dev/null 2>&1 || true
  fi
  echo "Mac Render Helper uninstalled."; exit 0
fi

[[ "$(uname -s)" == "Darwin" ]] || { echo "Mac Render Helper is macOS-only."; exit 1; }
echo "== Mac Render Helper installer =="
# 1. ffmpeg
FF="$(command -v ffmpeg || true)"
for d in /opt/homebrew/bin /usr/local/bin; do [[ -z "$FF" && -x "$d/ffmpeg" ]] && FF="$d/ffmpeg"; done
if [[ -z "$FF" ]]; then echo "ffmpeg not found. Install it with: brew install ffmpeg   then re-run."; exit 1; fi
FFDIR="$(dirname "$FF")"; echo "ffmpeg: $FF"
PY=/usr/bin/python3; [[ -x "$PY" ]] || PY="$(command -v python3)"
"$PY" -c 'import sys; assert sys.version_info >= (3,8)' || { echo "Need Python 3.8+ (xcode-select --install)"; exit 1; }

# 2. copy files (idempotent overwrite)
mkdir -p "$DEST" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$SRC/rh_worker.py" "$SRC/rh_common.py" "$SRC/rh_xfer.py" "$SRC/rh_capture.mjs" "$SRC/package.json" "$DEST/"
cp "$SRC/package-lock.json" "$DEST/" 2>/dev/null || true
# browser capture: playwright-core from package.json/lock (no browser download; uses installed Chrome).
# worker_id and Keychain items are left untouched, so no re-pairing.
NODE="$(command -v node || true)"
for d in /opt/homebrew/bin /usr/local/bin; do [[ -z "$NODE" && -x "$d/node" ]] && NODE="$d/node"; done
NODEDIR="$(dirname "${NODE:-/opt/homebrew/bin/node}")"
if [[ -z "$NODE" ]]; then
  echo "Install Node: brew install node (or https://nodejs.org)"
else
  echo "node $("$NODE" -v): installing playwright-core..."
  ( cd "$DEST" && PATH="$NODEDIR:$PATH" PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 "$NODEDIR/npm" install --omit=dev --no-audit --no-fund ) \
    && echo "Browser capture: ready." || echo "npm install failed; browser capture unavailable (ffmpeg jobs still work)."
fi
[[ -d "/Applications/Google Chrome.app" ]] || echo "Browser capture: Google Chrome not found in /Applications."

# 3. secrets -> Keychain (asked only if missing)
if security find-generic-password -s macrenderhelper-github -w >/dev/null 2>&1; then
  echo "GitHub token: already in Keychain (keeping it)."
else
  read -r -s -p "Paste the fine-grained GitHub token for m-fin/render-helper: " TOK; echo
  [[ -n "$TOK" ]] || { echo "No token entered."; exit 1; }
  security add-generic-password -U -a "$USER" -s macrenderhelper-github -w "$TOK"; unset TOK
fi
if security find-generic-password -s macrenderhelper-hmac -w >/dev/null 2>&1; then
  echo "HMAC key: already in Keychain (keeping it)."
else
  read -r -s -p "Paste the shared HMAC key, or press Enter to generate one: " KEY; echo
  if [[ -z "$KEY" ]]; then
    KEY="$(openssl rand -hex 32)"
    echo "Generated HMAC key (shown ONCE). Copy it now:"
    echo ""
    echo "  $KEY"
    echo ""
    echo "Paste it into the masked secret box (RH_HMAC_KEY) in your 1:1 chat with Crave Builder."
    echo "Never type or paste it as a normal chat message."
  fi
  security add-generic-password -U -a "$USER" -s macrenderhelper-hmac -w "$KEY"; unset KEY
fi

# 4. launchd agent (reload if already present)
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DEST/rh_worker.py</string>
    <string>--ffmpeg-dir</string><string>$FFDIR</string>
    <string>--node-dir</string><string>$NODEDIR</string>
    <string>--xfer</string></array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG</string><key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
echo "Background worker loaded ($LABEL). Log: $LOG"
echo "Tailscale file endpoint: serves only on this Mac's 100.x Tailscale address, port 8765 (off until Tailscale is up)."

# 5. tiny local test render (1s, 64x64)
echo "Running test job..."
if "$PY" "$DEST/rh_worker.py" --ffmpeg-dir "$FFDIR" --selftest; then
  echo "Mac Render Helper installed. PASS"
else
  echo "Mac Render Helper test FAILED. See output above."; exit 1
fi
