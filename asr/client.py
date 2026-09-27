import json
import os
import queue
import threading
import uuid
from pathlib import Path

import dashscope
import websocket
from dotenv import load_dotenv, set_key

from audio.capture import SAMPLE_RATE

MODEL = "qwen-audio-3.1-asr-flash-streaming"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_api_key() -> str | None:
	load_dotenv(ENV_FILE)
	return os.getenv("DASHSCOPE_API_KEY")


def is_api_key_configured() -> bool:
	return bool(load_api_key())


def save_api_key(api_key: str, dotenv_path: Path = ENV_FILE) -> None:
	value = api_key.strip()
	if not value:
		raise ValueError("API Key cannot be empty.")
	try:
		saved, _, _ = set_key(
			dotenv_path,
			"DASHSCOPE_API_KEY",
			value,
			quote_mode="always",
			encoding="utf-8",
		)
	except OSError:
		raise OSError("Unable to save the API Key to the project .env file.") from None
	if not saved:
		raise OSError("Unable to save the API Key to the project .env file.")
	os.environ["DASHSCOPE_API_KEY"] = value


def start_qwen_task(api_key: str) -> tuple[websocket.WebSocket, str]:
	task_id = str(uuid.uuid4())
	connection = websocket.create_connection(
		dashscope.base_websocket_api_url,
		timeout=10,
		header={"Authorization": f"Bearer {api_key}"},
	)
	try:
		connection.send(
			json.dumps(
				{
					"header": {
						"action": "run-task",
						"task_id": task_id,
						"streaming": "duplex",
					},
					"payload": {
						"model": MODEL,
						"task_group": "audio",
						"task": "asr",
						"function": "recognition",
						"parameters": {
							"format": "pcm",
							"sample_rate": SAMPLE_RATE,
						},
						"input": {},
					},
				},
				ensure_ascii=False,
			),
		)
		response = json.loads(connection.recv())
		header = response.get("header", {})
		if header.get("event") != "task-started":
			code = header.get("error_code", header.get("event", "unknown"))
			message = header.get("error_message", "Unexpected server response.")
			raise RuntimeError(f"Qwen task start failed ({code}): {message}")
		return connection, task_id
	except Exception:
		connection.close()
		raise


def send_finish_task(connection: websocket.WebSocket, task_id: str) -> None:
	connection.send(
		json.dumps(
			{
				"header": {
					"action": "finish-task",
					"task_id": task_id,
					"streaming": "duplex",
				},
				"payload": {"input": {}},
			},
		),
	)


def receive_asr_events(
	connection: websocket.WebSocket,
	events: queue.Queue[dict[str, object]],
	stop_event: threading.Event,
) -> None:
	connection.settimeout(1)
	while not stop_event.is_set():
		try:
			message = connection.recv()
		except websocket.WebSocketTimeoutException:
			continue
		except websocket.WebSocketException as error:
			events.put({"header": {"event": "client-error"}, "message": str(error)})
			return

		if not isinstance(message, str):
			events.put({"header": {"event": "client-error"}, "message": "Unexpected binary server response."})
			return

		try:
			server_event = json.loads(message)
		except json.JSONDecodeError:
			events.put({"header": {"event": "client-error"}, "message": "Invalid JSON server response."})
			return

		events.put(server_event)
		if server_event.get("header", {}).get("event") in {"task-finished", "task-failed"}:
			return


def test_qwen_connection() -> int:
	api_key = load_api_key()
	if not api_key:
		print("Qwen connection test failed: DASHSCOPE_API_KEY is not configured.")
		return 2

	connection: websocket.WebSocket | None = None
	try:
		connection, task_id = start_qwen_task(api_key)
		print("Qwen task start: PASS")
		send_finish_task(connection, task_id)
		finished = json.loads(connection.recv())
		if finished.get("header", {}).get("event") != "task-finished":
			print("Qwen task finish: FAIL")
			return 1
		print("Qwen task finish: PASS")
		return 0
	except Exception as error:
		message = str(error).replace(api_key, "[REDACTED]")
		print("Qwen connection test failed:", type(error).__name__, message)
		return 1
	finally:
		if connection is not None:
			connection.close()