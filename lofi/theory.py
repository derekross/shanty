"""Music theory core: chords, progressions, voice-led voicings.

Progressions are written as (degree, quality) pairs relative to a major key
(degree in semitones above the tonic). The composer transposes them to any key.
"""

from __future__ import annotations

from dataclasses import dataclass

# Chord quality -> intervals in semitones from the chord root.
QUALITIES: dict[str, tuple[int, ...]] = {
    "maj7": (0, 4, 7, 11),
    "maj9": (0, 4, 7, 11, 14),
    "6/9": (0, 4, 7, 9, 14),
    "m7": (0, 3, 7, 10),
    "m9": (0, 3, 7, 10, 14),
    "m11": (0, 3, 7, 10, 14, 17),
    "7": (0, 4, 7, 10),
    "9": (0, 4, 7, 10, 14),
    "13": (0, 4, 10, 14, 21),
    "7b9": (0, 4, 7, 10, 13),
    "m6": (0, 3, 7, 9),
    "sus13": (0, 5, 10, 14, 21),
}

NOTE_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


@dataclass(frozen=True)
class Chord:
    degree: int      # semitones above tonic
    quality: str

    def name(self, key_pc: int) -> str:
        return NOTE_NAMES[(key_pc + self.degree) % 12] + self.quality


def _p(*pairs: tuple[int, str]) -> list[Chord]:
    return [Chord(d, q) for d, q in pairs]


# Eight-bar progressions, one chord per bar, split by tonal home.
# Major pool: jazz lo-fi staples. Minor pool: aeolian/dorian moods and the
# synthwave staples (i-VI-III-VII and friends), written vs the relative major
# so degree 9 is the minor home.

PROG_MAJOR: list[list[Chord]] = [
    # Royal road with deceptive turnaround (the prototype's progression)
    _p((5, "maj9"), (7, "13"), (4, "m7"), (9, "m9"),
       (2, "m9"), (7, "13"), (0, "maj9"), (4, "7b9")),
    # I-vi-ii-V, twice, second time landing home
    _p((0, "maj9"), (9, "m9"), (2, "m9"), (7, "13"),
       (0, "maj7"), (9, "m7"), (2, "m11"), (7, "sus13")),
    # Borrowed iv: the "sigh" progression
    _p((0, "maj7"), (4, "m7"), (5, "maj9"), (5, "m6"),
       (0, "maj9"), (9, "m9"), (5, "m6"), (0, "6/9")),
    # ii-V with a tritone sub into home
    _p((2, "m9"), (7, "13"), (4, "m7"), (9, "m9"),
       (2, "m11"), (1, "7"), (0, "maj9"), (0, "6/9")),
    # Gentle circle: iii-vi-ii-V with maj7 rest points
    _p((4, "m9"), (9, "m9"), (2, "m9"), (7, "13"),
       (0, "maj9"), (5, "maj9"), (4, "m7"), (7, "7b9")),
]

PROG_MINOR: list[list[Chord]] = [
    # THE synthwave progression: i-VI-III-VII (Am-F-C-G), twice with color
    _p((9, "m9"), (5, "maj7"), (0, "maj9"), (7, "13"),
       (9, "m9"), (5, "maj9"), (0, "maj7"), (7, "sus13")),
    # i-VII-VI-VII, second pass pulling home through V
    _p((9, "m9"), (7, "9"), (5, "maj9"), (7, "13"),
       (9, "m9"), (7, "9"), (5, "maj9"), (4, "7b9")),
    # i-iv-VI-V: darker drive
    _p((9, "m9"), (2, "m9"), (5, "maj9"), (4, "7b9"),
       (9, "m9"), (2, "m11"), (5, "maj9"), (4, "7b9")),
    # i-VI-iv-VII wash
    _p((9, "m9"), (5, "maj9"), (2, "m9"), (7, "13"),
       (9, "m11"), (5, "maj9"), (2, "m9"), (4, "7b9")),
    # vi-IV-I-V, jazzed (relative-minor mood)
    _p((9, "m9"), (5, "maj9"), (0, "maj7"), (7, "13"),
       (9, "m9"), (5, "maj9"), (2, "m11"), (7, "7b9")),
    # Dorian vamp (i7-IV9 feel, written vs relative major: vi-II9)
    _p((9, "m9"), (2, "9"), (9, "m9"), (2, "9"),
       (5, "maj9"), (4, "m7"), (9, "m11"), (7, "sus13")),
    # Aeolian drift: vi-IV-V-iii
    _p((9, "m9"), (5, "maj7"), (7, "13"), (4, "m9"),
       (9, "m9"), (5, "maj9"), (7, "sus13"), (9, "m9")),
]

PROGRESSIONS: list[list[Chord]] = PROG_MAJOR + PROG_MINOR

# Major pentatonic degrees; the composer draws melody pitches from this + chord tones.
PENTATONIC = (0, 2, 4, 7, 9)


def chord_pitch_classes(chord: Chord, key_pc: int) -> list[int]:
    root = (key_pc + chord.degree) % 12
    return [(root + iv) % 12 for iv in QUALITIES[chord.quality]]


def voice_chord(chord: Chord, key_pc: int, prev_voicing: list[int] | None,
                lo: int = 52, hi: int = 71) -> list[int]:
    """Rootless-ish voicing in [lo, hi], voice-led from the previous chord.

    For each chord tone (skipping the root when the chord is dense — the bass
    covers it), pick the octave placement closest to the previous voicing's
    centroid, then de-duplicate and sort.
    """
    root = (key_pc + chord.degree) % 12
    intervals = QUALITIES[chord.quality]
    pcs = [(root + iv) % 12 for iv in intervals]
    if len(pcs) >= 4:
        pcs = pcs[1:]  # drop root, bass has it

    center = (sum(prev_voicing) / len(prev_voicing)) if prev_voicing else (lo + hi) / 2

    voicing: list[int] = []
    for pc in pcs:
        candidates = [p for p in range(lo, hi + 1) if p % 12 == pc]
        if not candidates:
            continue
        pitch = min(candidates, key=lambda p: abs(p - center) + 2 * (p in voicing))
        if pitch not in voicing:
            voicing.append(pitch)
    voicing.sort()
    # Avoid mud: if the two lowest are a second apart down low, lift the lower one.
    if len(voicing) >= 2 and voicing[1] - voicing[0] <= 2 and voicing[0] < 55:
        lifted = voicing[0] + 12
        if lifted <= hi and lifted not in voicing:
            voicing[0] = lifted
            voicing.sort()
    return voicing


def bass_root(chord: Chord, key_pc: int, lo: int = 36, hi: int = 50) -> int:
    """Root pitch for the bass register."""
    pc = (key_pc + chord.degree) % 12
    for p in range(lo, hi + 1):
        if p % 12 == pc:
            return p
    return lo


def scale_tones(key_pc: int) -> list[int]:
    """Pitch classes usable for melodies: major pentatonic of the key."""
    return [(key_pc + d) % 12 for d in PENTATONIC]
