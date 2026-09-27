from collections import deque
from dataclasses import dataclass, field


@dataclass
class TranscriptState:
	current_partial: str = ""
	current_sentence_id: str | int | None = None
	committed_sentence_ids: deque[str | int] = field(default_factory=deque)
	last_rendered_width: int = 0

	def update(self, sentence: dict[str, object]) -> tuple[str, str] | None:
		text = sentence.get("text")
		if not isinstance(text, str) or not text:
			return None

		sentence_id = sentence.get("sentence_id")
		if sentence.get("sentence_end") is True:
			if (
				isinstance(sentence_id, (str, int))
				and sentence_id in self.committed_sentence_ids
			):
				if self.current_sentence_id == sentence_id:
					self.current_partial = ""
					self.current_sentence_id = None
				return None
			if isinstance(sentence_id, (str, int)):
				if sentence_id not in self.committed_sentence_ids:
					if len(self.committed_sentence_ids) == 256:
						self.committed_sentence_ids.popleft()
					self.committed_sentence_ids.append(sentence_id)
			if self.current_sentence_id in (None, sentence_id):
				self.current_partial = ""
				self.current_sentence_id = None
			return "FINAL", text

		self.current_partial = text
		self.current_sentence_id = sentence_id if isinstance(sentence_id, (str, int)) else None
		return "PARTIAL", text