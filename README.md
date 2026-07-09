# lowfisoapbox

A generative lo-fi music engine for Soapbox's 24/7 Concord live-channel stream.
Claude composes every track: procedural music theory (jazz chord banks, voice
leading, motif-based melodies, swung boom-bap drums) rendered through FluidSynth
and numpy drum synthesis, then run through a lo-fi FX chain — tape wobble, vinyl
crackle, sidechain pump, rounded top end. Infinite, never-repeating, royalty-free.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install mido pedalboard soundfile

# FluidSynth + the FluidR3 soundfont, extracted locally (no root needed):
apt-get download fluidsynth libfluidsynth3 fluid-soundfont-gm
for d in *.deb; do dpkg -x "$d" .local; done && rm *.deb
```

(If `libfluidsynth.so.3` isn't already on the system, install `libfluidsynth3`
properly or add `.local/lib/...` to `LD_LIBRARY_PATH`.)

## Usage

```bash
# Compose and render tracks to MP3 (reproducible via --seed)
.venv/bin/python -m lofi.cli render --count 3 -o out

# Run the endless 24/7 stream daemon
.venv/bin/python -m lofi.cli stream --fifo /tmp/lofi.pcm

# Listen to / test the stream
ffmpeg -f s16le -ar 48000 -ac 2 -i /tmp/lofi.pcm -c:a libopus -b:a 96k stream.ogg
# or live: ffplay -f s16le -ar 48000 -ac 2 -i /tmp/lofi.pcm
```

Every track is fully determined by its seed — if a generated track is a keeper,
its seed reproduces it exactly.

## How a track is made

1. **Compose** (`lofi/composer.py`, `lofi/theory.py`) — seed first picks a
   **style**: *synthwave* (minor i–VI–III–VII progressions, driving eighth-note
   synth bass, arpeggios, analog pads, 80s drum machine, dotted-eighth echo),
   *chillsynth* (softer hybrid), or *lofi-retro* (jazz turns, swung boom-bap,
   walking bass). Then key, tempo, swing, structure (intro → A → B → breakdown →
   outro with a ringing resolution), and instruments are drawn from the style's
   palette. Chords are voice-led rootless voicings; melodies develop a 2-bar
   motif over chord-tone/pentatonic pools.
2. **Render** (`lofi/render.py`) — harmonic stems go MIDI → FluidSynth
   (FluidR3_GM: Rhodes, dusty grand, vibraphone, nylon, flute, muted trumpet…);
   drums are synthesized in numpy (swept-sine kick, noise-burst snare, filtered
   hats) with humanized timing and velocity throughout.
3. **The lo-fi treatment** (`lofi/fx.py`) — per-stem color, sidechain ducking
   against the actual kick times, tape wow/flutter via modulated fractional
   delay, a generated vinyl bed (crackle, dust pops, hiss, turntable rumble),
   tanh tape saturation, 12dB/oct ladder lowpass, glue compression, −1dBFS
   ceiling. Master sits around −17dB RMS.

A ~2-minute track renders in ~8 seconds (~15× real time), so the daemon never
falls behind; if it somehow did, it loops the last track instead of going silent.

## Phase 2: Shanty ⚓🎶 — the Concord radio bot

`bot/` is Shanty, the bot that sits in a Concord live channel 24/7 and plays
the stream. Python does all the Nostr/CORD work (key derivations byte-exact
against Armada — see `bot/tests/`; broker grants; presence heartbeats; invites);
a **headless Chromium page** (`bot/media/`) is the media engine — it runs
Armada's own `livekit-client` build with Armada's exact E2EE settings
(AES-256 per-sender keys), because LiveKit's native SDKs hardcode AES-128 and
can't produce Armada-compatible frames. Interop is proven end-to-end by
`bot/tools/interop_check.py` against the real armada.buzz broker/SFU/TURN.

```bash
# one-time setup
.venv/bin/python -m bot.cli create-identity      # prints Shanty's npub
#   → invite that npub from Armada (direct invite), or use a public invite link
.venv/bin/python -m bot.cli accept-invite --channel-id <live channel hex>
.venv/bin/python -m bot.cli accept-invite --link 'https://…/invite/naddr1…#…' --channel-id <hex>

# run (expects the phase-1 daemon writing the FIFO)
.venv/bin/python -m bot.cli run
```

**Chat commands** (in the live channel):
- `!music <url>` — any member; queue a track from `wavlake.com/track/…` or
  `fountain.fm/track/…` (queue cap 10, 15-min track cap). Lo-fi pauses via FIFO
  backpressure and resumes seamlessly after the queue drains.
- `!music queue` — any member; list now-playing + queued tracks.
- `!music on` / `!music off` — staff (owner/admin/mod); mute switch.
- `!music skip` — staff; jump past the current requested track.

Deployment: `deploy/setup.sh` bootstraps a Debian/Ubuntu VPS (venv, local
fluidsynth extraction, full-build headless Chromium, vendored livekit-client,
systemd user units `lofi-stream.service` + `shanty.service`, linger).

Notes that cost debugging time so you don't repeat them:
- The Chromium **must** be the full build (`channel="chromium"`); the default
  Playwright headless shell renders no WebAudio and publishes silence.
- Broker grants carry a `nonce` tag: without it, two grants in one second have
  the same event id and trip the broker's anti-replay cache.
- Presence heartbeats are not optional: Armada clients install a random key
  for any SFU identity that no fresh kind-23313 presence claims, and drop the
  audio as unverified.

## Phase-1/2 interface contract (music daemon → bot)

The daemon emits **s16le, 48kHz, stereo, interleaved PCM** on a FIFO
(default `/tmp/lofi.pcm`). Properties the bot can rely on:

- **Pacing is consumer-driven.** Writes block on the pipe; read at whatever
  rate the SFU needs (100ms of audio = 19,200 bytes). No timestamps, no header —
  raw frames.
- **The stream never ends and never goes silent** — tracks crossfade (3s,
  equal power) and a vinyl-crackle bed runs even through intros/outros.
- **Reader restarts are safe.** If the bot disconnects, the daemon blocks until
  the FIFO is reopened, then resumes mid-stream.
- 48kHz is Opus/WebRTC-native: feed frames straight into a LiveKit
  `AudioSource` (livekit rtc SDK) with no resampling. CORD-07 handles the rest
  (blind broker token, E2EE frame encryption, signed presence).
- Track metadata (name, seed, BPM, key) is printed to the daemon's stdout —
  phase 2 can parse it to post "now playing" into the channel.

## Layout

```
lofi/theory.py      chords, progressions, voice leading
lofi/composer.py    seed -> TrackData (all notes, drums, fx params)
lofi/render.py      MIDI/FluidSynth stems + numpy drum synthesis
lofi/fx.py          the lo-fi treatment + mixdown
lofi/daemon.py      endless crossfaded PCM stream over a FIFO
lofi/cli.py         `render` and `stream` commands
lofi/prototype.py   the original hand-composed 16-bar reference piece
```
