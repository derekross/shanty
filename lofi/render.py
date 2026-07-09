"""Rendering: note events -> MIDI -> FluidSynth stems, plus numpy-synthesized drums.

A "stem" is a list of Note events rendered with one General MIDI program.
Drums are synthesized directly (they take lo-fi processing better than
soundfont kits and give us exact kick times for sidechain ducking).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import mido
import numpy as np
import soundfile as sf

from . import SR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_FLUIDSYNTH = PROJECT_ROOT / ".local/usr/bin/fluidsynth"
FLUIDSYNTH = str(_LOCAL_FLUIDSYNTH) if _LOCAL_FLUIDSYNTH.exists() else shutil.which("fluidsynth")
_LOCAL_SF2 = PROJECT_ROOT / ".local/usr/share/sounds/sf2/FluidR3_GM.sf2"
SOUNDFONT = str(_LOCAL_SF2) if _LOCAL_SF2.exists() else "/usr/share/sounds/sf2/default-GM.sf2"

TPB = 480  # MIDI ticks per beat


@dataclass
class Note:
    start: float  # in beats
    dur: float    # in beats
    pitch: int    # MIDI note number
    vel: int      # 1-127


def swing8(t: float, amount: float = 0.16) -> float:
    """Delay off-beat eighths. amount is in beats (0.16 ~ gentle lo-fi swing)."""
    frac = t % 1.0
    if abs(frac - 0.5) < 1e-6:
        return t + amount
    return t


def _beats_to_ticks(beats: float) -> int:
    return max(0, int(round(beats * TPB)))


def notes_to_midi(notes: list[Note], program: int, bpm: float, tail_beats: float = 4.0) -> mido.MidiFile:
    mid = mido.MidiFile(ticks_per_beat=TPB)
    track = mido.MidiTrack()
    mid.tracks.append(track)

    events: list[tuple[int, int, mido.Message]] = []  # (tick, order, msg)
    for n in notes:
        on = _beats_to_ticks(n.start)
        off = _beats_to_ticks(n.start + n.dur)
        if off <= on:
            off = on + 1
        vel = int(np.clip(n.vel, 1, 127))
        events.append((on, 1, mido.Message("note_on", note=n.pitch, velocity=vel)))
        events.append((off, 0, mido.Message("note_off", note=n.pitch, velocity=0)))

    events.sort(key=lambda e: (e[0], e[1]))

    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    track.append(mido.Message("program_change", program=program, time=0))

    now = 0
    for tick, _, msg in events:
        msg.time = tick - now
        track.append(msg)
        now = tick
    # Pad so FluidSynth renders the instrument's release tail.
    end = now + _beats_to_ticks(tail_beats)
    track.append(mido.MetaMessage("end_of_track", time=end - now))
    return mid


def render_stem(notes: list[Note], program: int, bpm: float, length_beats: float,
                gain: float = 0.5) -> np.ndarray:
    """Render note events to a float32 stereo buffer of exactly length_beats (+tail trimmed/padded)."""
    n_samples = int(round(length_beats * 60.0 / bpm * SR))
    if not notes:
        return np.zeros((n_samples, 2), dtype=np.float32)

    with tempfile.TemporaryDirectory(prefix="lofi-") as td:
        midi_path = Path(td) / "stem.mid"
        wav_path = Path(td) / "stem.wav"
        notes_to_midi(notes, program, bpm).save(str(midi_path))
        subprocess.run(
            [FLUIDSYNTH, "-ni", "-g", str(gain), "-r", str(SR),
             "-o", "synth.reverb.active=0", "-o", "synth.chorus.active=0",
             "-F", str(wav_path), SOUNDFONT, str(midi_path)],
            check=True, capture_output=True,
        )
        audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
        assert sr == SR

    if len(audio) >= n_samples:
        out = audio[:n_samples].copy()
        # Fade the last 30ms so a trim never clicks.
        fade = min(int(0.03 * SR), n_samples)
        out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]
    else:
        out = np.zeros((n_samples, 2), dtype=np.float32)
        out[: len(audio)] = audio
    return out


# ---------------------------------------------------------------------------
# Drum synthesis
# ---------------------------------------------------------------------------

def _env(n: int, decay_s: float, sr: int = SR) -> np.ndarray:
    t = np.arange(n) / sr
    return np.exp(-t / decay_s).astype(np.float32)


def synth_kick(sr: int = SR, retro: bool = False) -> np.ndarray:
    """Kick drum. Dusty: boom-bap sine sweep 110->43Hz with a soft knock.
    Retro: shorter, punchier 80s drum-machine thump with a harder click."""
    n = int(0.40 * sr)
    t = np.arange(n) / sr
    if retro:
        freq = 48.0 + 100.0 * np.exp(-t / 0.03)
        body = np.sin(2 * np.pi * np.cumsum(freq) / sr) * _env(n, 0.11, sr)
        click_amp, click_dur = 0.6, 0.004
    else:
        freq = 43.0 + 67.0 * np.exp(-t / 0.045)
        body = np.sin(2 * np.pi * np.cumsum(freq) / sr) * _env(n, 0.16, sr)
        click_amp, click_dur = 0.4, 0.006
    knock = np.random.default_rng(7).standard_normal(int(click_dur * sr)).astype(np.float32) * click_amp
    knock *= _env(len(knock), 0.002, sr)
    out = body.astype(np.float32)
    out[: len(knock)] += knock
    return np.tanh(out * 1.8) * 0.9


def synth_snare(rng: np.random.Generator, sr: int = SR, gated: bool = False) -> np.ndarray:
    """Snare. Dusty: papery filtered noise + 185Hz body.
    Gated: the 80s trick — a big noise burst chopped off hard at ~130ms."""
    n = int(0.28 * sr) if gated else int(0.22 * sr)
    noise = rng.standard_normal(n).astype(np.float32)
    if gated:
        noise = _one_pole_hp(noise, 300, sr)
        noise = _one_pole_lp(noise, 8000, sr)
        noise *= _env(n, 0.16, sr)
        gate_at = int(0.13 * sr)
        fade = int(0.006 * sr)
        noise[gate_at + fade:] = 0.0
        noise[gate_at : gate_at + fade] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
        body_hz, body_amp, body_decay = 200.0, 0.35, 0.04
    else:
        noise = _one_pole_hp(noise, 400, sr)
        noise = _one_pole_lp(noise, 5500, sr)
        noise *= _env(n, 0.055, sr)
        body_hz, body_amp, body_decay = 185.0, 0.5, 0.03
    t = np.arange(n) / sr
    body = np.sin(2 * np.pi * body_hz * t).astype(np.float32) * _env(n, body_decay, sr) * body_amp
    return np.tanh((noise * 1.3 + body) * 1.4) * 0.8


def synth_hat(rng: np.random.Generator, open_hat: bool = False, sr: int = SR,
              bright: bool = False) -> np.ndarray:
    if bright:
        dur = 0.22 if open_hat else 0.045
        hp = 8000.0
    else:
        dur = 0.30 if open_hat else 0.055
        hp = 6500.0
    n = int(dur * sr)
    noise = rng.standard_normal(n).astype(np.float32)
    noise = _one_pole_hp(noise, hp, sr)
    noise = _one_pole_hp(noise, hp, sr)
    noise *= _env(n, dur * 0.35, sr)
    return noise * 0.55


def _one_pole_lp(x: np.ndarray, cutoff: float, sr: int = SR) -> np.ndarray:
    a = float(np.exp(-2 * np.pi * cutoff / sr))
    y = np.empty_like(x)
    acc = 0.0
    b = 1.0 - a
    for i in range(len(x)):
        acc = b * x[i] + a * acc
        y[i] = acc
    return y


def _one_pole_hp(x: np.ndarray, cutoff: float, sr: int = SR) -> np.ndarray:
    return x - _one_pole_lp(x, cutoff, sr)


@dataclass
class DrumHit:
    time: float   # in beats
    kind: str     # kick | snare | hat | ohat
    vel: float    # 0..1


def render_drums(hits: list[DrumHit], bpm: float, length_beats: float,
                 seed: int = 0, kit: str = "dusty") -> tuple[np.ndarray, list[float]]:
    """Render drum hits to stereo audio. Returns (audio, kick_times_seconds).

    kit: "dusty" (boom-bap lo-fi) or "retro" (80s machine: punchy kick,
    gated snare, bright hats).
    """
    rng = np.random.default_rng(seed)
    retro = kit == "retro"
    n_samples = int(round(length_beats * 60.0 / bpm * SR))
    out = np.zeros(n_samples + SR, dtype=np.float32)
    kick_times: list[float] = []

    # Small sample bank: unique noise per hit isn't audible, but the Python-loop
    # one-pole filters are too slow to run hundreds of times.
    kick = synth_kick(retro=retro)
    snares = [synth_snare(rng, gated=retro) for _ in range(3)]
    hats = [synth_hat(rng, bright=retro) for _ in range(4)]
    ohat = synth_hat(rng, open_hat=True, bright=retro)

    for h in hits:
        t_s = h.time * 60.0 / bpm
        i = int(t_s * SR)
        if h.kind == "kick":
            s = kick
            kick_times.append(t_s)
        elif h.kind == "snare":
            s = snares[rng.integers(len(snares))]
        elif h.kind == "ohat":
            s = ohat
        else:
            s = hats[rng.integers(len(hats))]
        end = min(i + len(s), len(out))
        out[i:end] += s[: end - i] * h.vel

    stereo = np.stack([out[:n_samples], out[:n_samples]], axis=1)
    return stereo, kick_times


def save_wav(path: str | Path, audio: np.ndarray) -> None:
    sf.write(str(path), audio, SR, subtype="PCM_16")


def wav_to_mp3(wav_path: str | Path, mp3_path: str | Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(wav_path),
         "-codec:a", "libmp3lame", "-b:a", "192k", str(mp3_path)],
        check=True,
    )
