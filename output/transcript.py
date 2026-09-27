import sys

from asr.segment import TranscriptState


def display_asr_event(
	server_event: dict[str, object],
	api_key: str,
	transcript_state: TranscriptState,
) -> bool:
	header = server_event.get("header", {})
	event_name = header.get("event")
	if event_name == "result-generated":
		payload = server_event.get("payload", {})
		output = payload.get("output", {})
		sentence = output.get("sentence", {})
		result = transcript_state.update(sentence)
		if result is not None:
			label, text = result
			line = f"[{label}] {text}"
			padding = max(0, transcript_state.last_rendered_width - len(line))
			print(f"\r{line}{' ' * padding}", end="\n" if label == "FINAL" else "", flush=True)
			transcript_state.last_rendered_width = 0 if label == "FINAL" else len(line)
		return False

	if event_name == "task-failed":
		message = str(header.get("error_message", "Qwen task failed."))
		print("\nQwen task failed:", message.replace(api_key, "[REDACTED]"), file=sys.stderr)
		return True

	if event_name == "client-error":
		message = str(server_event.get("message", "WebSocket receive failed."))
		print("\nWebSocket receive failed:", message.replace(api_key, "[REDACTED]"), file=sys.stderr)
		return True

	return event_name == "task-finished"