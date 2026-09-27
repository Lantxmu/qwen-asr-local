import json
import queue
import sys
import threading
import time
from collections.abc import Callable

import websocket

from audio.capture import iter_audio_frames
from asr.client import load_api_key, receive_asr_events, send_finish_task, start_qwen_task
from asr.segment import TranscriptState
from output.transcript import display_asr_event


def run_realtime_asr(
	duration_seconds: float | None = None,
	stop_event: threading.Event | None = None,
	on_event: Callable[[dict[str, object]], None] | None = None,
	on_status: Callable[[str, str], None] | None = None,
	pause_event: threading.Event | None = None,
) -> int:
	api_key = load_api_key()
	if not api_key:
		message = "DASHSCOPE_API_KEY is not configured."
		if on_status is not None:
			on_status("error", message)
		else:
			print("Realtime ASR failed:", message, file=sys.stderr)
		return 2

	stop_event = stop_event or threading.Event()
	connection: websocket.WebSocket | None = None
	receiver: threading.Thread | None = None
	receiver_stop = threading.Event()
	events: queue.Queue[dict[str, object]] = queue.Queue()
	transcript_state = TranscriptState()
	task_started = False
	task_failed = False
	finish_received = False

	def report_status(kind: str, message: str) -> None:
		if on_status is not None:
			on_status(kind, message)
		elif message:
			print(message, file=sys.stderr if kind == "error" else sys.stdout)

	def handle_event(server_event: dict[str, object]) -> bool:
		if on_event is not None:
			on_event(server_event)
			return server_event.get("header", {}).get("event") in {"task-failed", "client-error"}
		return display_asr_event(server_event, api_key, transcript_state)

	try:
		connection, task_id = start_qwen_task(api_key)
		task_started = True
		report_status("started", "Qwen streaming task started.")
		receiver = threading.Thread(
			target=receive_asr_events,
			args=(connection, events, receiver_stop),
			daemon=True,
		)
		receiver.start()

		for audio_frame, overflowed in iter_audio_frames(duration_seconds, stop_event, pause_event):
			connection.send_binary(audio_frame)
			if overflowed:
				report_status("warning", "Audio input overflowed.")
			while True:
				try:
					server_event = events.get_nowait()
				except queue.Empty:
					break
				if handle_event(server_event):
					task_failed = True
					break
			if task_failed:
				break
	except Exception as error:
		message = str(error).replace(api_key, "[REDACTED]")
		report_status("error", f"{type(error).__name__}: {message}")
		task_failed = True
	finally:
		if connection is not None:
			if task_started and not task_failed:
				try:
					connection.settimeout(10)
					send_finish_task(connection, task_id)
					shutdown_deadline = time.monotonic() + 10
					while time.monotonic() < shutdown_deadline:
						try:
							server_event = events.get(timeout=0.5)
						except queue.Empty:
							if receiver is not None and not receiver.is_alive():
								break
							continue
						event_name = server_event.get("header", {}).get("event")
						if event_name == "task-finished":
							finish_received = True
							break
						if handle_event(server_event):
							task_failed = True
							break
				except Exception as error:
					message = str(error).replace(api_key, "[REDACTED]")
					report_status("error", f"Shutdown {type(error).__name__}: {message}")
				if not finish_received and not task_failed:
					report_status("error", "Qwen task did not confirm task-finished.")
					task_failed = True
			receiver_stop.set()
			connection.close()
		if receiver is not None:
			receiver.join(timeout=2)
	if not task_failed:
		report_status("stopped", "Qwen streaming task stopped.")
	return 1 if task_failed else 0