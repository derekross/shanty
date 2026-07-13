#!/usr/bin/env bash
# VPS bootstrap for the lo-fi stream + Shanty bot (Debian/Ubuntu, no root needed
# beyond an existing user account; fluidsynth is extracted locally from .debs).
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"

echo "== python venv + deps =="
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q mido pedalboard soundfile \
    livekit httpx coincurve cryptography nostr-sdk websockets playwright pytest

echo "== headless chromium (full build — the headless shell can't do WebAudio) =="
.venv/bin/playwright install chromium

echo "== fluidsynth + FluidR3 soundfont (local extraction, no sudo) =="
if [ ! -x .local/usr/bin/fluidsynth ]; then
    mkdir -p .local
    apt-get download fluidsynth libfluidsynth3 fluid-soundfont-gm
    for d in *.deb; do dpkg -x "$d" .local; done
    rm -f *.deb
fi
# libfluidsynth3 must be findable; if it isn't system-installed, expose the local copy
if ! ldconfig -p | grep -q libfluidsynth.so.3; then
    echo "NOTE: add to both service files:"
    echo "  Environment=LD_LIBRARY_PATH=%h/lowfisoapbox/.local/usr/lib/x86_64-linux-gnu"
fi

echo "== livekit-client vendor (from the armada checkout or npm) =="
if [ ! -f bot/media/vendor/livekit-client.esm.mjs ]; then
    mkdir -p bot/media/vendor
    if [ -d "$HOME/Projects/armada/client/node_modules/livekit-client/dist" ]; then
        cp "$HOME/Projects/armada/client/node_modules/livekit-client/dist/livekit-client.esm.mjs" \
           "$HOME/Projects/armada/client/node_modules/livekit-client/dist/livekit-client.e2ee.worker.mjs" \
           bot/media/vendor/
    else
        npm pack livekit-client@2.17.2 >/dev/null
        tar xzf livekit-client-2.17.2.tgz package/dist/livekit-client.esm.mjs \
                package/dist/livekit-client.e2ee.worker.mjs
        mv package/dist/*.mjs bot/media/vendor/ && rm -rf package livekit-client-*.tgz
    fi
fi

echo "== smoke tests =="
.venv/bin/python -m pytest bot/tests/ -q
.local/usr/bin/fluidsynth --version | head -1

echo "== systemd user units =="
mkdir -p ~/.config/systemd/user
sed "s|%h/lowfisoapbox|$REPO_DIR|g" deploy/lofi-stream.service > ~/.config/systemd/user/lofi-stream.service
sed "s|%h/lowfisoapbox|$REPO_DIR|g" deploy/shanty.service > ~/.config/systemd/user/shanty.service
sed "s|%h/lowfisoapbox|$REPO_DIR|g" deploy/shanty@.service > ~/.config/systemd/user/shanty@.service
systemctl --user daemon-reload
loginctl enable-linger "$USER" || echo "run 'sudo loginctl enable-linger $USER' so services survive logout"

cat <<EOF

Setup complete. Next:
  1. .venv/bin/python -m bot.cli create-identity          (or copy ~/.config/lowfi/shanty.json from dev)
  2. Invite Shanty from Armada (direct invite to its npub, or use an invite link)
  3. .venv/bin/python -m bot.cli accept-invite [--link <url>] --channel-id <hex>
  4. systemctl --user enable --now lofi-stream shanty
  5. journalctl --user -fu shanty
EOF
