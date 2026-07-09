"""LiveKit E2EE audio publisher: feeds PCM (FIFO or any reader) into a CORD-07 room.

Interop-critical settings (must match Armada's SenderKeyProvider in
client/src/components/PersistentVoiceRoom.tsx):
  - per-participant keys (NO shared key)
  - key_derivation_function = HKDF  ← Python SDK defaults to PBKDF2; Armada's
    livekit-client 2.x derives with HKDF(salt="LKFrameEncryptionKey", info=128
    zero bytes). Wrong KDF = undecodable frames.
  - ratchet_window_size = 0, failure_tolerance = -1 (keys are external and
    deterministic; ratcheting must never fire)
  - AES-GCM, key_index 0
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from livekit import rtc
from livekit.rtc._proto import e2ee_pb2

from .voice import AvToken

log = logging.getLogger("lowfi.publisher")

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_MS = 10
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000          # 480
BYTES_PER_FRAME = SAMPLES_PER_FRAME * CHANNELS * 2          # s16le


def e2ee_options() -> rtc.E2EEOptions:
    return rtc.E2EEOptions(
        key_provider_options=rtc.KeyProviderOptions(
            ratchet_window_size=0,
            failure_tolerance=-1,
            key_derivation_function=e2ee_pb2.KeyDerivationFunction.HKDF,
        ),
        encryption_type=e2ee_pb2.EncryptionType.GCM,
    )


@dataclass
class PublisherHandles:
    room: rtc.Room
    source: rtc.AudioSource
    track: rtc.LocalAudioTrack
    publication: rtc.LocalTrackPublication


async def connect_and_publish(token: AvToken, sender_key: bytes,
                              track_name: str = "lofi") -> PublisherHandles:
    """Connect to the SFU with E2EE and publish a stereo 48kHz audio track."""
    room = rtc.Room()
    await room.connect(token.url, token.token, rtc.RoomOptions(
        auto_subscribe=False,
        e2ee=e2ee_options(),
    ))
    room.e2ee_manager.key_provider.set_key(token.identity, sender_key, 0)
    room.e2ee_manager.set_enabled(True)

    source = rtc.AudioSource(SAMPLE_RATE, CHANNELS)
    track = rtc.LocalAudioTrack.create_audio_track(track_name, source)
    publication = await room.local_participant.publish_track(track, rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_MICROPHONE,
        red=True,
        dtx=False,  # music: never gate "silence"
    ))
    log.info("published track %s to room as %s", publication.sid, token.identity)
    return PublisherHandles(room, source, track, publication)


async def feed_fifo(source: rtc.AudioSource, fifo_path: str,
                    stop: asyncio.Event) -> None:
    """Read s16le/48k/stereo PCM from the FIFO and capture 10ms frames.

    Blocks on the FIFO when the phase-1 daemon is slow and reopens it if the
    writer goes away — the stream must survive daemon restarts.
    """
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            fd = await loop.run_in_executor(None, os.open, fifo_path, os.O_RDONLY)
        except FileNotFoundError:
            log.warning("FIFO %s missing; retrying in 2s", fifo_path)
            await asyncio.sleep(2)
            continue
        log.info("reading PCM from %s", fifo_path)
        try:
            buf = b""
            while not stop.is_set():
                chunk = await loop.run_in_executor(None, os.read, fd, BYTES_PER_FRAME * 4)
                if not chunk:  # writer closed
                    log.warning("FIFO writer went away; reopening")
                    break
                buf += chunk
                while len(buf) >= BYTES_PER_FRAME:
                    frame = rtc.AudioFrame(
                        data=buf[:BYTES_PER_FRAME],
                        sample_rate=SAMPLE_RATE,
                        num_channels=CHANNELS,
                        samples_per_channel=SAMPLES_PER_FRAME,
                    )
                    buf = buf[BYTES_PER_FRAME:]
                    await source.capture_frame(frame)
        finally:
            os.close(fd)
