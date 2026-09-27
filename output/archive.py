from datetime import datetime
from pathlib import Path
import re


class TranscriptArchive:
	def __init__(self, directory: Path | None = None) -> None:
		self.directory = directory or Path(__file__).resolve().parents[1] / "transcripts"
		self.started_at = datetime.now().astimezone()
		self.file_path: Path | None = None

	def append_final(self, text: str) -> Path | None:
		if not text.strip():
			return self.file_path
		if self.file_path is None:
			self.directory.mkdir(parents=True, exist_ok=True)
			stamp = self.started_at.strftime("%Y-%m-%d_%H-%M-%S")
			path = self.directory / f"{stamp}.md"
			if path.exists():
				stamp = self.started_at.strftime("%Y-%m-%d_%H-%M-%S_%f")
				path = self.directory / f"{stamp}.md"
			self.file_path = path
			path.write_text(
				f"# 实时语音转写\n\n开始时间：{self.started_at:%Y-%m-%d %H:%M:%S %Z}\n\n",
				encoding="utf-8",
			)
		with self.file_path.open("a", encoding="utf-8", newline="\n") as transcript:
			transcript.write(f"{text.strip()}\n\n")
		return self.file_path


class TranscriptReader:
	def __init__(self, file_path: Path, page_size: int = 40) -> None:
		self.file_path = file_path
		self.page_size = page_size
		self.page_start = 0
		self.page_end = file_path.stat().st_size
		self.newer_page_ends: list[int] = []
		self.last_seen_size = -1

	def read_latest_page(self) -> list[str]:
		if not self.file_path.exists():
			return []
		self.last_seen_size = self.file_path.stat().st_size
		self.page_end = self.last_seen_size
		self.newer_page_ends.clear()
		entries, self.page_start = self._read_page_before(self.page_end)
		return entries

	def read_previous_page(self) -> list[str]:
		if self.page_start == 0:
			return []
		self.newer_page_ends.append(self.page_end)
		self.page_end = self.page_start
		entries, self.page_start = self._read_page_before(self.page_end)
		return entries

	def read_next_page(self) -> list[str]:
		if not self.newer_page_ends:
			return []
		self.page_end = self.newer_page_ends.pop()
		entries, self.page_start = self._read_page_before(self.page_end)
		return entries

	def _read_page_before(self, end_offset: int) -> tuple[list[str], int]:
		position = end_offset
		content = b""
		while position > 0:
			chunk_size = min(4096, position)
			position -= chunk_size
			with self.file_path.open("rb") as transcript:
				transcript.seek(position)
				content = transcript.read(chunk_size) + content
			if content.count(b"\n\n") > self.page_size + 2:
				break

		blocks = re.finditer(rb"(?:\r?\n){2}", content)
		valid_blocks: list[tuple[int, str]] = []
		block_start = 0
		for index, separator in enumerate(blocks):
			block = content[block_start : separator.start()]
			if index > 0 or position == 0:
				text = block.decode("utf-8", errors="ignore").strip()
				if text and not text.startswith("#") and not text.startswith("开始时间："):
					valid_blocks.append((position + block_start, text))
			block_start = separator.end()
		if block_start < len(content):
			text = content[block_start:].decode("utf-8", errors="ignore").strip()
			if text and not text.startswith("#") and not text.startswith("开始时间："):
				valid_blocks.append((position + block_start, text))

		selected = valid_blocks[-self.page_size :]
		if not selected:
			return [], 0
		page_start = 0 if position == 0 and len(valid_blocks) <= self.page_size else selected[0][0]
		return [text for _, text in selected], page_start