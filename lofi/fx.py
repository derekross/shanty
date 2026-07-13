"""The lo-fi treatment: per-stem color, tape wobble, vinyl bed, sidechain, master glue."""

from __future__ import annotations

import numpy as np
from pedalboard import (
    Chorus,
    Compressor,
    Delay,
    Gain,
    HighpassFilter,
    LadderFilter,
    Limiter,
    LowpassFilter,
    PeakFilter,
    Pedalboard,
    Reverb,
)

from dataclasses import dataclass

from . import SR


@dataclass
class FxParams:
    """Per-track lo-fi character."""
    lowpass_hz: float = 9500.0
    crackle: float = 1.0
    wobble_ms: float = 1.6
    delay_s: float = 0.0  # tempo-synced echo on melody/arp; 0 = off
    drum_gain: float = 0.6  # drum bus level in the mix


def _fx(board: Pedalboard, audio: np.ndarray) -> np.ndarray:
    return board(audio, SR)


def process_keys(audio: np.ndarray) -> np.ndarray:
    """Rhodes/keys: warm, slightly warbly, rounded top."""
    board = Pedalboard([
        HighpassFilter(cutoff_frequency_hz=90),
        Chorus(rate_hz=0.9, depth=0.25, centre_delay_ms=7.0, feedback=0.0, mix=0.35),
        LowpassFilter(cutoff_frequency_hz=6500),
        Reverb(room_size=0.35, damping=0.6, wet_level=0.12, dry_level=0.88, width=0.9),
    ])
    return _fx(board, audio)


def process_melody(audio: np.ndarray, delay_s: float = 0.0) -> np.ndarray:
    """Lead voice: a little dreamier — more verb, optional synced echo."""
    plugins = [
        HighpassFilter(cutoff_frequency_hz=180),
        LowpassFilter(cutoff_frequency_hz=7000),
    ]
    if delay_s > 0:
        plugins.append(Delay(delay_seconds=delay_s, feedback=0.3, mix=0.18))
    plugins.append(Reverb(room_size=0.5, damping=0.5, wet_level=0.22, dry_level=0.78, width=1.0))
    return _fx(Pedalboard(plugins), audio)


def process_pads(audio: np.ndarray) -> np.ndarray:
    """Synth pads: wide, washy, sat behind everything."""
    board = Pedalboard([
        HighpassFilter(cutoff_frequency_hz=140),
        Chorus(rate_hz=0.35, depth=0.45, centre_delay_ms=9.0, feedback=0.0, mix=0.5),
        LowpassFilter(cutoff_frequency_hz=7500),
        Reverb(room_size=0.6, damping=0.5, wet_level=0.25, dry_level=0.75, width=1.0),
    ])
    return _fx(board, audio)


def process_arp(audio: np.ndarray, delay_s: float = 0.0) -> np.ndarray:
    """Arpeggios: tight and glassy, echo doing the 80s work."""
    plugins = [
        HighpassFilter(cutoff_frequency_hz=220),
        LowpassFilter(cutoff_frequency_hz=8500),
    ]
    if delay_s > 0:
        plugins.append(Delay(delay_seconds=delay_s, feedback=0.35, mix=0.22))
    plugins.append(Reverb(room_size=0.4, damping=0.6, wet_level=0.12, dry_level=0.88, width=1.0))
    return _fx(Pedalboard(plugins), audio)


def process_bass(audio: np.ndarray) -> np.ndarray:
    """Bass: mono, dark, solid."""
    mono = audio.mean(axis=1, keepdims=True).repeat(2, axis=1)
    board = Pedalboard([
        HighpassFilter(cutoff_frequency_hz=35),
        LowpassFilter(cutoff_frequency_hz=1800),
        Compressor(threshold_db=-20, ratio=3.0, attack_ms=10, release_ms=120),
    ])
    return _fx(board, mono)


def process_drums(audio: np.ndarray) -> np.ndarray:
    """Drums: punchy but dusty."""
    board = Pedalboard([
        LowpassFilter(cutoff_frequency_hz=9000),
        Compressor(threshold_db=-16, ratio=3.0, attack_ms=8, release_ms=90),
        Gain(gain_db=1.5),
    ])
    return _fx(board, audio)


# ---------------------------------------------------------------------------
# Vinyl bed
# ---------------------------------------------------------------------------

def vinyl_bed(n_samples: int, seed: int = 0, crackle_level: float = 1.0) -> np.ndarray:
    """Crackle + dust + hiss, stereo. The 'record player in the room' layer."""
    rng = np.random.default_rng(seed)
    out = np.zeros((n_samples, 2), dtype=np.float32)

    # Hiss: quiet filtered noise.
    hiss = rng.standard_normal((n_samples, 2)).astype(np.float32) * 0.0002
    hiss = Pedalboard([LowpassFilter(7000), HighpassFilter(800)])(hiss, SR)
    out += hiss

    # Crackle: sparse ticks (~3 per second), varied size, lowpassed to soften.
    # Kept well below the music — audible in gaps, not a constant scrape.
    n_ticks = int(n_samples / SR * 3 * crackle_level)
    ticks = np.zeros(n_samples, dtype=np.float32)
    positions = rng.integers(0, n_samples - 64, size=n_ticks)
    for p in positions:
        amp = rng.uniform(0.002, 0.018) * (2.0 if rng.random() < 0.02 else 1.0)  # rare pop
        width = rng.integers(2, 14)
        ticks[p : p + width] += rng.standard_normal(width).astype(np.float32) * amp
    ticks_st = np.stack([ticks, np.roll(ticks, rng.integers(3, 40))], axis=1)
    ticks_st = Pedalboard([LowpassFilter(3500), HighpassFilter(300)])(ticks_st, SR)
    out += ticks_st

    # Low turntable rumble.
    rumble = rng.standard_normal((n_samples, 2)).astype(np.float32) * 0.002
    rumble = Pedalboard([LowpassFilter(90)])(rumble, SR)
    out += rumble
    return out


# ---------------------------------------------------------------------------
# Sidechain + master
# ---------------------------------------------------------------------------

def sidechain_envelope(n_samples: int, kick_times_s: list[float],
                       depth: float = 0.35, attack_s: float = 0.012,
                       release_s: float = 0.28) -> np.ndarray:
    """Volume envelope that dips at each kick — that lo-fi 'breathing' feel."""
    env = np.ones(n_samples, dtype=np.float32)
    n_att = int(attack_s * SR)
    n_rel = int(release_s * SR)
    dip = np.concatenate([
        np.linspace(0.0, 1.0, n_att, dtype=np.float32),
        np.exp(-np.linspace(0.0, 5.0, n_rel, dtype=np.float32)),
    ]) * depth
    for t in kick_times_s:
        i = int(t * SR)
        end = min(i + len(dip), n_samples)
        env[i:end] = np.minimum(env[i:end], 1.0 - dip[: end - i])
    return env


def tape_wobble(audio: np.ndarray, seed: int = 0,
                wow_hz: float = 0.5, depth_ms: float = 1.6) -> np.ndarray:
    """Slow pitch drift via a modulated fractional delay — tape wow."""
    rng = np.random.default_rng(seed)
    n = len(audio)
    t = np.arange(n) / SR
    phase = rng.uniform(0, 2 * np.pi)
    # Wow plus a touch of faster flutter; +0.12 keeps the delay non-negative
    # (a negative delay would read past the end of the buffer).
    delay_s = (depth_ms / 1000.0) * (
        0.5 * (1 + np.sin(2 * np.pi * wow_hz * t + phase))
        + 0.12 * (1 + np.sin(2 * np.pi * 6.3 * t))
    )
    max_delay = int(np.ceil(delay_s.max() * SR)) + 2
    src = np.arange(n) - delay_s * SR + max_delay
    padded = np.vstack([np.zeros((max_delay, 2), dtype=np.float32), audio,
                        np.zeros((2, 2), dtype=np.float32)])
    i0 = src.astype(np.int64)
    frac = (src - i0).astype(np.float32)[:, None]
    return padded[i0] * (1 - frac) + padded[i0 + 1] * frac


def master_chain(audio: np.ndarray, lowpass_hz: float = 9500.0) -> np.ndarray:
    """Glue: saturation, rounded top end, gentle compression, limiter."""
    audio = np.tanh(audio * 1.1).astype(np.float32)  # soft tape saturation
    board = Pedalboard([
        HighpassFilter(cutoff_frequency_hz=55),
        HighpassFilter(cutoff_frequency_hz=55),  # doubled: 24dB/oct against sub rumble
        PeakFilter(cutoff_frequency_hz=500, gain_db=4.5, q=0.5),  # radio warmth
        LadderFilter(mode=LadderFilter.Mode.LPF24, cutoff_hz=lowpass_hz, resonance=0.05),
        Compressor(threshold_db=-20, ratio=4.0, attack_ms=5, release_ms=300),
        Gain(gain_db=4.0),  # push transients into the limiter — smaller crest
        Limiter(threshold_db=-3.0, release_ms=120),
    ])
    out = _fx(board, audio)
    # Pedalboard's Limiter clips at 0dBFS when pushed; normalize to a true -1dB ceiling.
    peak = float(np.abs(out).max())
    ceiling = 0.891
    if peak > ceiling:
        out *= ceiling / peak
    return out


def mix_track(stems: dict[str, np.ndarray], kick_times_s: list[float],
              seed: int = 0, fx: FxParams | None = None) -> np.ndarray:
    """Full lo-fi mixdown. stems maps any of keys/pads/bass/melody/arp/drums
    to raw stem audio (all the same length); missing or None entries are skipped."""
    fx = fx or FxParams()
    stems = {k: v for k, v in stems.items() if v is not None}
    n = len(next(iter(stems.values())))
    music = np.zeros((n, 2), dtype=np.float32)
    if "keys" in stems:
        music += process_keys(stems["keys"]) * 1.1
    if "pads" in stems:
        music += process_pads(stems["pads"]) * 0.65
    if "bass" in stems:
        music += process_bass(stems["bass"]) * 0.62
    if "melody" in stems:
        music += process_melody(stems["melody"], fx.delay_s) * 0.9
    if "arp" in stems:
        music += process_arp(stems["arp"], fx.delay_s) * 0.5
    music *= sidechain_envelope(n, kick_times_s)[:, None]
    full = music
    if "drums" in stems:
        full = full + process_drums(stems["drums"]) * fx.drum_gain
    full = tape_wobble(full, seed=seed, depth_ms=fx.wobble_ms)
    full = full + vinyl_bed(n, seed=seed + 1, crackle_level=fx.crackle)
    return master_chain(full, lowpass_hz=fx.lowpass_hz)
