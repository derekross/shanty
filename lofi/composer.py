"""Generative composer: a seed in, a full lo-fi track out.

Every track first picks a STYLE — synthwave, chillsynth, or lofi-retro — which
sets its palette: progressions (minor synthwave staples vs jazz turns), drum
kit and feel (80s machine vs dusty boom-bap), bass mode (driving eighths vs
walking), pads, arpeggios, echo. Then key, tempo, structure, motif melody, and
FX character are drawn on top. Same seed -> same track, always.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .fx import FxParams, mix_track
from .render import DrumHit, Note, render_drums, render_stem, swing8
from .theory import (PROG_MAJOR, PROG_MINOR, Chord, bass_root,
                     chord_pitch_classes, scale_tones, voice_chord)

# GM programs: 4/5 EPs, 0 grand, 11 vibes, 24 nylon, 73 flute, 59 muted trumpet,
# 80 square lead, 81 saw lead, 87 bass+lead, 98 crystal, 88-95 pads, 50/51 synth
# strings, 38/39 synth bass, 32/33 upright & finger bass.

MELODY_REGISTER = {11: (67, 84), 4: (69, 88), 5: (69, 88), 24: (62, 79), 73: (74, 91),
                   59: (64, 79), 80: (67, 86), 81: (67, 86)}

ADJECTIVES = ["dusty", "amber", "quiet", "neon", "velvet", "misty", "chrome",
              "cedar", "faded", "warm", "hazy", "slow", "midnight", "sunday",
              "electric", "violet", "analog", "polar"]
NOUNS = ["windowsill", "porch", "attic", "polaroid", "raincoat", "teacup",
         "lamplight", "corduroy", "clementine", "bookshop", "streetcar",
         "moth", "puddle", "postcard", "skyline", "arcade", "freeway",
         "horizon", "cassette", "terminal"]


@dataclass(frozen=True)
class Style:
    name: str
    tempo: tuple[float, float]
    swing: tuple[float, float]
    drum_patterns: tuple[str, ...]      # boombap | straight | four | half
    kit: str                            # dusty | retro
    bass_modes: tuple[str, ...]         # walking | held | eighths
    bass_programs: tuple[int, ...]
    keys_rhythms: tuple[str, ...]       # lofi | held | stab
    keys_programs: tuple[int, ...]
    pad_programs: tuple[int, ...]
    pad_prob: float
    arp_prob: float
    arp_programs: tuple[int, ...]
    melody_prob: float
    melody_programs: tuple[int, ...]
    minor_bias: float                   # prob of a minor-home progression
    delay_prob: float                   # dotted-8th echo on melody/arp
    lowpass: tuple[float, float]
    crackle: tuple[float, float]


STYLES = [
    Style("synthwave",
          tempo=(78, 96), swing=(0.0, 0.05),
          drum_patterns=("straight", "straight", "four"), kit="retro",
          bass_modes=("eighths", "eighths", "held"), bass_programs=(38, 39),
          keys_rhythms=("stab", "held"), keys_programs=(5, 4, 90),
          pad_programs=(89, 90, 94, 95, 50, 51), pad_prob=0.9,
          arp_prob=0.9, arp_programs=(98, 87, 81, 5),
          melody_prob=0.5, melody_programs=(80, 81, 5),
          minor_bias=0.85, delay_prob=0.85,
          lowpass=(9000, 12000), crackle=(0.4, 0.9)),
    Style("chillsynth",
          tempo=(70, 85), swing=(0.03, 0.10),
          drum_patterns=("straight", "half", "boombap"), kit="retro",
          bass_modes=("held", "eighths"), bass_programs=(38, 39, 33),
          keys_rhythms=("held", "lofi"), keys_programs=(4, 5, 88),
          pad_programs=(88, 89, 94, 51), pad_prob=0.75,
          arp_prob=0.55, arp_programs=(98, 5, 11),
          melody_prob=0.75, melody_programs=(11, 80, 4, 73),
          minor_bias=0.6, delay_prob=0.6,
          lowpass=(8500, 11000), crackle=(0.6, 1.1)),
    Style("lofi-retro",
          tempo=(68, 80), swing=(0.08, 0.16),
          drum_patterns=("boombap", "boombap", "half"), kit="dusty",
          bass_modes=("walking", "held"), bass_programs=(33, 38),
          keys_rhythms=("lofi",), keys_programs=(4, 4, 5, 0),
          pad_programs=(89, 94), pad_prob=0.45,
          arp_prob=0.3, arp_programs=(98, 11),
          melody_prob=0.85, melody_programs=(11, 4, 24, 59, 80),
          minor_bias=0.45, delay_prob=0.35,
          lowpass=(8000, 10000), crackle=(0.8, 1.5)),
]
STYLE_WEIGHTS = [0.5, 0.3, 0.2]  # Derek's taste: synthwave-forward (misty raincoat)


@dataclass
class Section:
    bars: int
    drums: str        # off | on | half | sparse
    melody: bool
    arp: bool


@dataclass
class TrackData:
    seed: int
    name: str
    style: str
    bpm: float
    key_pc: int
    total_beats: float
    kit: str
    keys_program: int
    bass_program: int
    melody_program: int
    pad_program: int | None
    arp_program: int | None
    keys_notes: list[Note] = field(default_factory=list)
    pad_notes: list[Note] = field(default_factory=list)
    bass_notes: list[Note] = field(default_factory=list)
    melody_notes: list[Note] = field(default_factory=list)
    arp_notes: list[Note] = field(default_factory=list)
    drum_hits: list[DrumHit] = field(default_factory=list)
    fx: FxParams = field(default_factory=FxParams)


class Composer:
    def __init__(self, seed: int):
        self.seed = seed
        self.rng = np.random.default_rng(seed)

    # -- helpers ------------------------------------------------------------
    def hum_t(self, t: float, amt: float = 0.008) -> float:
        return max(0.0, t + self.rng.uniform(-amt, amt))

    def hum_v(self, v: int, amt: int = 6) -> int:
        return int(np.clip(v + self.rng.integers(-amt, amt + 1), 1, 127))

    def choice(self, xs):
        return xs[self.rng.integers(len(xs))]

    def uni(self, rng_pair: tuple[float, float]) -> float:
        return float(self.rng.uniform(*rng_pair))

    # -- top level ----------------------------------------------------------
    def compose(self) -> TrackData:
        rng = self.rng
        style = STYLES[rng.choice(len(STYLES), p=STYLE_WEIGHTS)]
        self.style = style
        key_pc = int(rng.integers(12))
        bpm = self.uni(style.tempo)
        self.swing = self.uni(style.swing)
        self.keys_rhythm = self.choice(style.keys_rhythms)
        self.bass_mode = self.choice(style.bass_modes)
        self.drum_pattern = self.choice(style.drum_patterns)

        pool = PROG_MINOR if rng.random() < style.minor_bias else PROG_MAJOR
        progression = self.choice(pool)
        minor_home = progression[0].degree == 9

        has_pads = rng.random() < style.pad_prob
        has_arp = rng.random() < style.arp_prob
        has_melody = rng.random() < style.melody_prob
        if not (has_arp or has_melody):
            has_arp = True  # something has to carry the top line

        sections = [Section(int(self.choice([2, 4])), "off", melody=False, arp=has_arp)]
        sections.append(Section(8, "on", melody=False, arp=has_arp))
        sections.append(Section(8, "on", melody=has_melody, arp=has_arp))
        breakdown = self.choice(["half", "sparse", "on"])
        sections.append(Section(8, breakdown, melody=has_melody,
                                arp=has_arp and rng.random() < 0.7))
        if rng.random() < 0.6:
            sections.append(Section(8, "on", melody=has_melody and rng.random() < 0.85,
                                    arp=has_arp))
        sections.append(Section(4, "off", melody=False, arp=False))  # outro
        total_bars = sum(s.bars for s in sections)

        home = Chord(9, "m9") if minor_home else Chord(0, "maj9")
        chords: list[Chord] = [progression[i % 8] for i in range(total_bars - 4)]
        chords += [progression[4], progression[5], home, home]

        track = TrackData(
            seed=self.seed,
            name=f"{self.choice(ADJECTIVES)} {self.choice(NOUNS)}",
            style=style.name,
            bpm=round(bpm, 1),
            key_pc=key_pc,
            total_beats=total_bars * 4 + 8,  # +2 bars of ring-out tail
            kit=style.kit,
            keys_program=self.choice(style.keys_programs),
            bass_program=self.choice(style.bass_programs),
            melody_program=self.choice(style.melody_programs),
            pad_program=self.choice(style.pad_programs) if has_pads else None,
            arp_program=self.choice(style.arp_programs) if has_arp else None,
            fx=FxParams(
                lowpass_hz=self.uni(style.lowpass),
                crackle=self.uni(style.crackle),
                wobble_ms=float(rng.uniform(0.9, 2.2)),
                delay_s=(0.75 * 60.0 / bpm) if rng.random() < style.delay_prob else 0.0,
            ),
        )

        self._keys(track, chords, sections)
        if has_pads:
            self._pads(track, chords, sections)
        self._bass(track, chords, sections)
        if has_melody:
            self._melody(track, chords, sections)
        if has_arp:
            self._arp(track, chords, sections)
        self._drums(track, sections)
        return track

    # -- chords ---------------------------------------------------------------
    def _keys(self, track: TrackData, chords: list[Chord], sections: list[Section]) -> None:
        rng = self.rng
        prev = None
        last_bar = sum(s.bars for s in sections) - 1
        for bar, chord in enumerate(chords):
            voicing = voice_chord(chord, track.key_pc, prev)
            prev = voicing
            t0 = bar * 4
            if bar == last_bar:  # final chord rings into the tail
                for i, p in enumerate(voicing):
                    track.keys_notes.append(Note(self.hum_t(t0 + i * 0.05), 10.0, p, self.hum_v(52)))
                continue
            if self.keys_rhythm == "held":
                for i, p in enumerate(voicing):
                    track.keys_notes.append(Note(self.hum_t(t0 + i * 0.02), 3.9, p, self.hum_v(52)))
            elif self.keys_rhythm == "stab":
                for i, p in enumerate(voicing):
                    track.keys_notes.append(Note(self.hum_t(t0 + i * 0.012), 0.7, p, self.hum_v(60)))
                for i, p in enumerate(voicing):
                    track.keys_notes.append(Note(self.hum_t(t0 + 2.5 + i * 0.012), 0.6, p, self.hum_v(48)))
            else:  # lofi: rolled downbeat + soft answer or push
                for i, p in enumerate(voicing):
                    track.keys_notes.append(Note(self.hum_t(t0 + i * 0.035), 2.4, p, self.hum_v(56)))
                s = rng.random()
                if s < 0.55:
                    for i, p in enumerate(voicing[1:]):
                        track.keys_notes.append(Note(self.hum_t(t0 + 2.5 + i * 0.03), 1.3, p, self.hum_v(42)))
                elif s < 0.8:
                    for i, p in enumerate(voicing[1:]):
                        track.keys_notes.append(Note(self.hum_t(t0 + 3.5 + i * 0.03), 0.9, p, self.hum_v(40)))

    def _pads(self, track: TrackData, chords: list[Chord], sections: list[Section]) -> None:
        prev = None
        last_bar = sum(s.bars for s in sections) - 1
        for bar, chord in enumerate(chords):
            voicing = voice_chord(chord, track.key_pc, prev, lo=57, hi=76)
            prev = voicing
            t0 = bar * 4
            dur = 10.0 if bar == last_bar else 4.1  # overlap into the next chord a touch
            for p in voicing:
                track.pad_notes.append(Note(self.hum_t(t0, 0.02), dur, p, self.hum_v(44, 4)))

    # -- bass -----------------------------------------------------------------
    def _bass(self, track: TrackData, chords: list[Chord], sections: list[Section]) -> None:
        rng = self.rng
        last_bar = sum(s.bars for s in sections) - 1
        for bar, chord in enumerate(chords):
            root = bass_root(chord, track.key_pc)
            t0 = bar * 4
            if bar == last_bar:
                track.bass_notes.append(Note(self.hum_t(t0), 8.0, root, self.hum_v(66)))
                continue
            next_root = bass_root(chords[min(bar + 1, len(chords) - 1)], track.key_pc)

            if self.bass_mode == "eighths":
                # The synthwave engine: pulsing eighth roots, octave pop before changes.
                for e in range(8):
                    pitch = root
                    if e == 7 and next_root != root and rng.random() < 0.5:
                        pitch = root + 12 if rng.random() < 0.5 else next_root
                    vel = 68 if e in (0, 5) else 56
                    track.bass_notes.append(
                        Note(self.hum_t(t0 + e * 0.5, 0.004), 0.42, pitch, self.hum_v(vel, 4)))
            elif self.bass_mode == "held":
                track.bass_notes.append(Note(self.hum_t(t0), 2.3, root, self.hum_v(68)))
                track.bass_notes.append(Note(self.hum_t(t0 + 2.5), 1.4, root, self.hum_v(56)))
            else:  # walking (the lofi upright feel)
                approach = next_root + (1 if next_root < root else -1)
                s = rng.random()
                track.bass_notes.append(Note(self.hum_t(t0), 1.75, root, self.hum_v(70)))
                if s < 0.5:
                    track.bass_notes.append(Note(self.hum_t(t0 + 2.5), 0.9, root, self.hum_v(56)))
                    track.bass_notes.append(Note(self.hum_t(t0 + 3.5), 0.45, approach, self.hum_v(52)))
                elif s < 0.75:
                    fifth = root + 7 if root + 7 <= 50 else root - 5
                    track.bass_notes.append(Note(self.hum_t(t0 + 2.0), 0.4, fifth, self.hum_v(50)))
                    track.bass_notes.append(Note(self.hum_t(t0 + 2.5), 0.9, root, self.hum_v(56)))
                    track.bass_notes.append(Note(self.hum_t(t0 + 3.5), 0.45, approach, self.hum_v(52)))
                else:
                    track.bass_notes.append(Note(self.hum_t(t0 + 2.5), 1.4, root, self.hum_v(56)))

    # -- melody ---------------------------------------------------------------
    def _make_motif(self) -> list[tuple[float, float, int]]:
        """A 2-bar motif: (onset_beat, dur, contour_step) triples on the 8th grid."""
        rng = self.rng
        n_onsets = int(self.choice([4, 5, 5, 6, 7]))
        grid = np.arange(1, 15) * 0.5
        weights = np.where(grid % 1.0 == 0.5, 1.3, 1.0)
        weights /= weights.sum()
        onsets = np.sort(rng.choice(grid, size=n_onsets, replace=False, p=weights))
        dur_bank = [1.0, 1.5, 2.0] if self.style.name == "synthwave" else [0.5, 0.75, 1.0, 1.5]
        motif = []
        for i, on in enumerate(onsets):
            gap = (onsets[i + 1] - on) if i + 1 < len(onsets) else (8.0 - on)
            dur = float(min(gap, self.choice(dur_bank)))
            if i == len(onsets) - 1:
                dur = float(min(gap + 0.5, 2.5))
            step = int(self.choice([-2, -1, -1, 0, 1, 1, 2]))
            motif.append((float(on), dur, step))
        return motif

    def _pitch_pool(self, chord: Chord, key_pc: int, lo: int, hi: int) -> list[int]:
        pcs = set(chord_pitch_classes(chord, key_pc)) | set(scale_tones(key_pc))
        return [p for p in range(lo, hi + 1) if p % 12 in pcs]

    def _melody(self, track: TrackData, chords: list[Chord], sections: list[Section]) -> None:
        rng = self.rng
        lo, hi = MELODY_REGISTER.get(track.melody_program, (67, 84))
        motif = self._make_motif()
        pos = (lo + hi) // 2

        bar = 0
        for sec in sections:
            if not sec.melody:
                bar += sec.bars
                continue
            for phrase_start in range(bar, bar + sec.bars, 2):
                if rng.random() < 0.2:  # breathe: skip a phrase
                    continue
                shape = motif if rng.random() < 0.7 else self._make_motif()
                for on, dur, step in shape:
                    abs_beat = phrase_start * 4 + swing8(on % 4, self.swing) + (on // 4) * 4
                    chord = chords[min(int(phrase_start + on // 4), len(chords) - 1)]
                    pool = self._pitch_pool(chord, track.key_pc, lo, hi)
                    chord_pool = [p for p in pool
                                  if p % 12 in chord_pitch_classes(chord, track.key_pc)]
                    target = pos + step * 2
                    use = chord_pool if (dur >= 1.0 and chord_pool) else pool
                    pitch = min(use, key=lambda p: abs(p - target))
                    pos = pitch
                    track.melody_notes.append(
                        Note(self.hum_t(abs_beat, 0.015), dur, pitch, self.hum_v(52, 8)))
            bar += sec.bars

    # -- arpeggio ---------------------------------------------------------------
    def _arp(self, track: TrackData, chords: list[Chord], sections: list[Section]) -> None:
        rng = self.rng
        rate = float(self.choice([0.5, 0.5, 0.25]))  # mostly eighths, sometimes 16ths
        mode = self.choice(["up", "updown", "broken"])
        span = int(self.choice([2, 2, 3]))  # octaves of ladder

        bar = 0
        for sec in sections:
            if not sec.arp:
                bar += sec.bars
                continue
            for b in range(sec.bars):
                chord = chords[min(bar + b, len(chords) - 1)]
                pcs = set(chord_pitch_classes(chord, track.key_pc))
                base = bass_root(chord, track.key_pc) + 24  # two octaves above bass root
                ladder = [p for p in range(base, base + span * 12 + 1) if p % 12 in pcs][:8]
                if not ladder:
                    continue
                if mode == "updown":
                    ladder = ladder + ladder[-2:0:-1]
                elif mode == "broken":
                    order = rng.permutation(len(ladder))
                    ladder = [ladder[i] for i in order]
                t0 = (bar + b) * 4
                n_steps = int(4 / rate)
                for s in range(n_steps):
                    beat = s * rate
                    vel = 52 if beat % 1.0 == 0 else 42
                    track.arp_notes.append(
                        Note(self.hum_t(t0 + beat, 0.004), rate * 0.85,
                             ladder[s % len(ladder)], self.hum_v(vel, 4)))
            bar += sec.bars

    # -- drums ------------------------------------------------------------------
    def _drums(self, track: TrackData, sections: list[Section]) -> None:
        rng = self.rng
        pattern = self.drum_pattern
        bar = 0
        for sec in sections:
            for b in range(sec.bars):
                t0 = (bar + b) * 4
                if sec.drums == "off":
                    continue
                pat = "half" if sec.drums == "half" else pattern
                sparse = sec.drums == "sparse"
                fill_bar = (b % 4 == 3) and not sparse

                if pat == "half":
                    track.drum_hits.append(DrumHit(self.hum_t(t0), "kick", 0.95))
                    track.drum_hits.append(DrumHit(self.hum_t(t0 + 2.02), "snare", 0.85))
                elif pat == "four":
                    for beat in range(4):
                        track.drum_hits.append(DrumHit(self.hum_t(t0 + beat, 0.005), "kick",
                                                       0.95 if beat == 0 else 0.85))
                    track.drum_hits.append(DrumHit(self.hum_t(t0 + 1.01), "snare", 0.8))
                    track.drum_hits.append(DrumHit(self.hum_t(t0 + 3.01), "snare", 0.8))
                else:  # boombap / straight share the backbone
                    late = 0.02 if pat == "boombap" else 0.005
                    track.drum_hits.append(DrumHit(self.hum_t(t0), "kick", 0.95))
                    track.drum_hits.append(DrumHit(self.hum_t(t0 + 2.75), "kick", 0.8))
                    if pat == "boombap" and rng.random() < 0.3:
                        track.drum_hits.append(DrumHit(self.hum_t(t0 + 3.5), "kick", 0.5))
                    track.drum_hits.append(DrumHit(self.hum_t(t0 + 1.0 + late), "snare", 0.85))
                    track.drum_hits.append(DrumHit(self.hum_t(t0 + 3.0 + late), "snare", 0.85))
                    if fill_bar:
                        track.drum_hits.append(DrumHit(self.hum_t(t0 + 3.5), "snare", 0.3))
                        track.drum_hits.append(DrumHit(self.hum_t(t0 + 3.75), "snare", 0.45))
                    elif pat == "boombap" and rng.random() < 0.35:
                        track.drum_hits.append(DrumHit(self.hum_t(t0 + 3.75), "snare", 0.18))

                if pat == "four":
                    # Offbeat hats: the disco/synthwave "and" accent.
                    for e in range(4):
                        track.drum_hits.append(
                            DrumHit(self.hum_t(t0 + e + 0.5, 0.008), "hat",
                                    0.45 * rng.uniform(0.9, 1.1)))
                else:
                    hat_skip = 0.55 if sparse else (0.35 if pat == "half" else 0.12)
                    for e in range(8):
                        beat = e * 0.5
                        if e % 2 == 1 and rng.random() < hat_skip:
                            continue
                        vel = 0.5 if e % 2 == 0 else 0.3
                        track.drum_hits.append(
                            DrumHit(self.hum_t(t0 + swing8(beat, self.swing), 0.01),
                                    "hat", vel * rng.uniform(0.85, 1.1)))
                if not sparse and (bar + b) % 2 == 1 and rng.random() < 0.5:
                    track.drum_hits.append(
                        DrumHit(self.hum_t(t0 + swing8(3.5, self.swing)), "ohat", 0.35))
            bar += sec.bars


def compose(seed: int) -> TrackData:
    return Composer(seed).compose()


def render_track(track: TrackData) -> np.ndarray:
    """TrackData -> final mixed stereo audio at 48kHz."""
    stems: dict[str, np.ndarray] = {
        "keys": render_stem(track.keys_notes, track.keys_program, track.bpm, track.total_beats),
        "bass": render_stem(track.bass_notes, track.bass_program, track.bpm, track.total_beats),
    }
    if track.pad_notes and track.pad_program is not None:
        stems["pads"] = render_stem(track.pad_notes, track.pad_program, track.bpm, track.total_beats)
    if track.melody_notes:
        stems["melody"] = render_stem(track.melody_notes, track.melody_program, track.bpm, track.total_beats)
    if track.arp_notes and track.arp_program is not None:
        stems["arp"] = render_stem(track.arp_notes, track.arp_program, track.bpm, track.total_beats)
    drums, kicks = render_drums(track.drum_hits, track.bpm, track.total_beats,
                                seed=track.seed, kit=track.kit)
    stems["drums"] = drums
    return mix_track(stems, kicks, seed=track.seed, fx=track.fx)
