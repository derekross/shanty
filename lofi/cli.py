"""CLI: render one-off tracks, or run the endless stream daemon.

  .venv/bin/python -m lofi.cli render [--seed N] [--count N] [-o DIR]
  .venv/bin/python -m lofi.cli stream [--fifo PATH] [--buffer N]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from .composer import compose, render_track
from .render import save_wav, wav_to_mp3
from .theory import NOTE_NAMES


def cmd_render(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng()
    for i in range(args.count):
        seed = args.seed if args.seed is not None else int(rng.integers(2**31))
        if args.seed is not None and args.count > 1:
            seed += i
        track = compose(seed)
        audio = render_track(track)
        slug = track.name.replace(" ", "-")
        wav = out_dir / f"{slug}-{seed}.wav"
        save_wav(wav, audio)
        mp3 = wav.with_suffix(".mp3")
        wav_to_mp3(wav, mp3)
        if not args.keep_wav:
            wav.unlink()
        print(f"♪ {track.name}  [{track.style}, seed {seed}]  {track.bpm:.0f}bpm in "
              f"{NOTE_NAMES[track.key_pc]}  {len(audio)/48000:.0f}s  -> {mp3}")


def cmd_stream(args: argparse.Namespace) -> None:
    from .daemon import run_stream
    run_stream(fifo_paths=args.fifo or ["/tmp/lofi.pcm"], buffer_tracks=args.buffer,
               nowplaying_path=args.nowplaying)


def main() -> None:
    ap = argparse.ArgumentParser(prog="lofi", description="Soapbox lo-fi generator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="compose and render track(s) to MP3")
    r.add_argument("--seed", type=int, default=None, help="seed (reproducible track)")
    r.add_argument("--count", type=int, default=1, help="number of tracks")
    r.add_argument("-o", "--out", default="out", help="output directory")
    r.add_argument("--keep-wav", action="store_true")
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("stream", help="run the endless 24/7 PCM stream daemon")
    s.add_argument("--fifo", action="append", default=None,
                   help="FIFO path for s16le 48kHz stereo PCM; repeat for one per "
                        "bot instance (default /tmp/lofi.pcm)")
    s.add_argument("--buffer", type=int, default=3, help="tracks to keep rendered ahead")
    s.add_argument("--nowplaying", default="/tmp/lofi-nowplaying.json",
                   help="JSON file updated with each track's metadata")
    s.set_defaults(func=cmd_stream)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
