"""Sound-first prototype: one hand-composed 16-bar lo-fi piece, rendered to MP3.

74 BPM in C. The progression is the "royal road" (IVmaj7-V7-iii7-vi9) answered
by ii-V into Cmaj9, with a deceptive E7b9 turnaround pulling back to Fmaj9.
Bars 1-8: keys + bass, drums enter bar 3. Bars 9-16: vibraphone melody joins.

Run:  .venv/bin/python -m lofi.prototype
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .fx import mix_track
from .render import DrumHit, Note, render_drums, render_stem, save_wav, swing8, wav_to_mp3

BPM = 74
BARS = 16
BEATS = BARS * 4
SEED = 11

rng = np.random.default_rng(SEED)


def hum_t(t: float, amt: float = 0.008) -> float:
    return max(0.0, t + rng.uniform(-amt, amt))


def hum_v(v: int, amt: int = 6) -> int:
    return int(np.clip(v + rng.integers(-amt, amt + 1), 1, 127))


# --- Harmony ----------------------------------------------------------------
# (name, rootless keys voicing, bass root MIDI)
PROGRESSION = [
    ("Fmaj9", [57, 60, 64, 67], 41),
    ("G13",   [59, 64, 65, 69], 43),
    ("Em7",   [55, 59, 62, 64], 40),
    ("Am9",   [55, 59, 60, 64], 45),
    ("Dm9",   [53, 57, 60, 64], 38),
    ("G13",   [59, 64, 65, 69], 43),
    ("Cmaj9", [55, 59, 62, 64], 48),
    ("E7b9",  [56, 59, 62, 65], 40),
]


def build_keys() -> list[Note]:
    notes = []
    for bar in range(BARS):
        _, voicing, _ = PROGRESSION[bar % 8]
        t0 = bar * 4
        # Rolled chord on the downbeat.
        for i, p in enumerate(voicing):
            notes.append(Note(hum_t(t0 + i * 0.035), 2.4, p, hum_v(58)))
        # Softer top-of-voicing answer on the and-of-two.
        for i, p in enumerate(voicing[1:]):
            notes.append(Note(hum_t(t0 + 2.5 + i * 0.03), 1.3, p, hum_v(42)))
    return notes


def build_bass() -> list[Note]:
    notes = []
    for bar in range(BARS):
        _, _, root = PROGRESSION[bar % 8]
        _, _, next_root = PROGRESSION[(bar + 1) % 8]
        t0 = bar * 4
        notes.append(Note(hum_t(t0), 1.75, root, hum_v(72)))
        notes.append(Note(hum_t(t0 + 2.5), 0.9, root, hum_v(58)))
        # Chromatic approach into the next bar's root.
        approach = next_root + (1 if next_root < root else -1)
        notes.append(Note(hum_t(t0 + 3.5), 0.45, approach, hum_v(52)))
    return notes


# --- Melody (vibraphone, bars 9-16) ----------------------------------------
# (bar, beat-in-bar, dur, pitch)
MELODY = [
    (8,  1.0, 0.75, 76), (8,  2.0, 0.5, 72), (8,  2.5, 1.5, 69),        # Fmaj9
    (9,  1.5, 0.5, 69), (9,  2.0, 0.75, 71), (9,  3.0, 1.0, 74),        # G13
    (10, 0.5, 0.75, 76), (10, 1.5, 0.5, 74), (10, 2.0, 1.75, 71),       # Em7
    (11, 1.0, 0.5, 67), (11, 1.5, 0.5, 69), (11, 2.0, 2.0, 72),         # Am9
    (12, 0.5, 0.75, 77), (12, 1.5, 0.5, 76), (12, 2.0, 1.5, 72),        # Dm9
    (13, 1.0, 0.5, 71), (13, 1.5, 0.5, 69), (13, 2.5, 1.5, 74),         # G13
    (14, 0.5, 1.0, 76), (14, 2.0, 0.5, 74), (14, 2.5, 1.5, 71),         # Cmaj9
    (15, 1.0, 1.0, 77), (15, 2.5, 1.5, 76),                             # E7b9: b9 -> 3
]


def build_melody() -> list[Note]:
    return [
        Note(hum_t(bar * 4 + swing8(beat), 0.015), dur, pitch, hum_v(54, 8))
        for bar, beat, dur, pitch in MELODY
    ]


# --- Drums ------------------------------------------------------------------

def build_drums() -> list[DrumHit]:
    hits = []
    for bar in range(2, BARS):  # drums enter at bar 3
        t0 = bar * 4
        last_bar_of_phrase = bar % 4 == 3

        hits.append(DrumHit(hum_t(t0), "kick", 0.95))
        hits.append(DrumHit(hum_t(t0 + 2.75), "kick", 0.8))
        if rng.random() < 0.3:
            hits.append(DrumHit(hum_t(t0 + 3.5), "kick", 0.5))

        # Backbeat, slightly laid back.
        hits.append(DrumHit(hum_t(t0 + 1.02), "snare", 0.85))
        hits.append(DrumHit(hum_t(t0 + 3.03), "snare", 0.85))
        if last_bar_of_phrase:  # little fill
            hits.append(DrumHit(hum_t(t0 + 3.5), "snare", 0.30))
            hits.append(DrumHit(hum_t(t0 + 3.75), "snare", 0.45))
        elif rng.random() < 0.35:
            hits.append(DrumHit(hum_t(t0 + 3.75), "snare", 0.18))  # ghost

        # Swung eighth hats, sparser in the first half.
        for e in range(8):
            beat = e * 0.5
            if bar < 8 and e % 2 == 1 and rng.random() < 0.5:
                continue
            vel = 0.5 if e % 2 == 0 else 0.3
            hits.append(DrumHit(hum_t(t0 + swing8(beat), 0.01), "hat", vel * rng.uniform(0.85, 1.1)))
        if bar >= 8 and bar % 2 == 1:
            hits.append(DrumHit(hum_t(t0 + swing8(3.5)), "ohat", 0.35))
    return hits


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "out"
    out_dir.mkdir(exist_ok=True)

    print("rendering stems…")
    keys = render_stem(build_keys(), program=4, bpm=BPM, length_beats=BEATS + 4)    # Rhodes EP1
    bass = render_stem(build_bass(), program=32, bpm=BPM, length_beats=BEATS + 4)   # acoustic bass
    melody = render_stem(build_melody(), program=11, bpm=BPM, length_beats=BEATS + 4)  # vibraphone
    drums, kicks = render_drums(build_drums(), bpm=BPM, length_beats=BEATS + 4, seed=SEED)

    print("mixing…")
    mix = mix_track({"keys": keys, "bass": bass, "drums": drums, "melody": melody},
                    kicks, seed=SEED)

    wav = out_dir / "prototype.wav"
    mp3 = out_dir / "prototype.mp3"
    save_wav(wav, mix)
    wav_to_mp3(wav, mp3)
    peak = float(np.abs(mix).max())
    print(f"done: {mp3}  ({len(mix)/48000:.1f}s, peak {peak:.2f})")


if __name__ == "__main__":
    main()
