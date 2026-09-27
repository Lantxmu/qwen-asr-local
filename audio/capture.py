import sys
import threading
import time
from collections.abc import Iterator

import sounddevice as sd

SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_SIZE = SAMPLE_RATE // 10


def iter_audio_frames(
	duration_seconds: float | None = None,
	stop_event: threading.Event | None = None,
	pause_event: threading.Event | None = None,
) -> Iterator[tuple[bytes, bool]]:
	if stop_event is not None and stop_event.is_set():
		return
	started_at = time.monotonic()
	with sd.InputStream(
		samplerate=SAMPLE_RATE,
		channels=CHANNELS,
		dtype="int16",
		blocksize=FRAME_SIZE,
	) as stream:
		while (
			(stop_event is None or not stop_event.is_set())
			and (duration_seconds is None or time.monotonic() - started_at < duration_seconds)
		):
			audio_frame, overflowed = stream.read(FRAME_SIZE)
			if (
				audio_frame.dtype.name != "int16"
				or audio_frame.shape != (FRAME_SIZE, CHANNELS)
			):
				raise RuntimeError("Microphone returned an unexpected audio frame format.")
			frame = bytes(audio_frame.nbytes) if pause_event is not None and pause_event.is_set() else audio_frame.tobytes()
			yield frame, bool(overflowed)


def capture_audio() -> None:
	frames_received = 0
	try:
		print("Audio capture started.")
		print(f"Sample rate: {SAMPLE_RATE}")
		print(f"Channels: {CHANNELS}")
		print(f"Frame size: {FRAME_SIZE}")
		for _frame, overflowed in iter_audio_frames():
			frames_received += 1
			print(f"frames received: {frames_received}")
			if overflowed:
				print("Warning: audio input overflowed.", file=sys.stderr)
	except KeyboardInterrupt:
		print("\nAudio capture stopped.")