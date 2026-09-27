import argparse

import sounddevice as sd

from audio.capture import capture_audio
from asr.client import test_qwen_connection
from asr.session import run_realtime_asr
from gui import launch_gui


def main() -> int:
	parser = argparse.ArgumentParser(description="Qwen streaming speech captions")
	parser.add_argument(
		"--test-qwen-connection",
		action="store_true",
		help="test the Qwen Streaming connection without sending audio",
	)
	parser.add_argument(
		"--test-microphone",
		action="store_true",
		help="capture microphone frames without connecting to Qwen",
	)
	parser.add_argument(
		"--console",
		action="store_true",
		help="run realtime recognition in the terminal",
	)
	parser.add_argument(
		"--duration-seconds",
		type=float,
		help="stop console recognition after this many seconds",
	)
	args = parser.parse_args()

	if args.test_qwen_connection:
		return test_qwen_connection()
	if args.test_microphone:
		capture_audio()
		return 0
	if args.duration_seconds is not None and args.duration_seconds <= 0:
		parser.error("--duration-seconds must be greater than zero")
	if not args.console and args.duration_seconds is None:
		return launch_gui()

	try:
		return run_realtime_asr(args.duration_seconds)
	except sd.PortAudioError as error:
		raise SystemExit(f"Microphone capture failed: {error}") from error


if __name__ == "__main__":
	raise SystemExit(main())
