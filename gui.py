import sys
import threading
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, Qt, Signal, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QPainter, QPalette
from PySide6.QtWidgets import (
	QApplication,
	QCheckBox,
	QFrame,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QInputDialog,
	QMessageBox,
	QMenu,
	QMainWindow,
	QPlainTextEdit,
	QPushButton,
	QSlider,
	QSplitter,
	QStyle,
	QSystemTrayIcon,
	QVBoxLayout,
	QWidget,
)

from asr.client import is_api_key_configured, save_api_key
from asr.segment import TranscriptState
from asr.session import run_realtime_asr
from output.archive import TranscriptArchive, TranscriptReader


class ASRWorker(QThread):
	subtitle_updated = Signal(str, str)
	status_updated = Signal(str, str)
	archive_path_updated = Signal(str)

	def __init__(self) -> None:
		super().__init__()
		self.stop_event = threading.Event()
		self.pause_event = threading.Event()
		self.transcript_state = TranscriptState()

	def request_stop(self) -> None:
		self.stop_event.set()

	def request_pause(self, paused: bool) -> None:
		if paused:
			self.pause_event.set()
		else:
			self.pause_event.clear()

	def run(self) -> None:
		archive = TranscriptArchive()

		def handle_event(server_event: dict[str, object]) -> None:
			header = server_event.get("header", {})
			event_name = header.get("event")
			if event_name == "result-generated":
				payload = server_event.get("payload", {})
				output = payload.get("output", {})
				sentence = output.get("sentence", {})
				result = self.transcript_state.update(sentence)
				if result is not None:
					self.subtitle_updated.emit(*result)
					label, text = result
					if label == "FINAL":
						try:
							archive_path = archive.append_final(text)
							if archive_path is not None:
								self.archive_path_updated.emit(str(archive_path))
						except OSError as error:
							self.status_updated.emit("error", f"字幕保存失败：{error}")
			elif event_name in {"task-failed", "client-error"}:
				message = str(
					header.get("error_message")
					or server_event.get("message")
					or "Recognition connection failed."
				)
				self.status_updated.emit("error", message)

		result = run_realtime_asr(
			stop_event=self.stop_event,
			on_event=handle_event,
			on_status=self.status_updated.emit,
			pause_event=self.pause_event,
		)
		if result == 0 and not self.stop_event.is_set():
			self.status_updated.emit("stopped", "Recognition session ended.")


class RightAlignedCaption(QLabel):
	def paintEvent(self, event) -> None:
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
		painter.setFont(self.font())
		painter.setPen(self.palette().color(QPalette.ColorRole.Text))
		painter.drawText(
			self.rect(),
			Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
			self.text(),
		)


class SubtitleOverlay(QWidget):
	def __init__(self) -> None:
		super().__init__(
			None,
			Qt.WindowType.Tool
			| Qt.WindowType.FramelessWindowHint
			| Qt.WindowType.WindowStaysOnTopHint,
		)
		self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
		self.setObjectName("subtitleOverlay")
		self.setFixedWidth(920)
		self.current_text = ""
		self.hide_timer = QTimer(self)
		self.hide_timer.setSingleShot(True)
		self.hide_timer.timeout.connect(self.hide)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		self.panel = QFrame()
		self.panel.setObjectName("overlayPanel")
		panel_layout = QVBoxLayout(self.panel)
		panel_layout.setContentsMargins(28, 8, 28, 10)
		panel_layout.setSpacing(3)
		self.eyebrow = QLabel("LIVE  /  QWEN ASR")
		self.eyebrow.setObjectName("overlayEyebrow")
		self.caption = RightAlignedCaption()
		self.caption.setObjectName("overlayCaption")
		self.caption.setWordWrap(False)
		panel_layout.addWidget(self.eyebrow, alignment=Qt.AlignmentFlag.AlignHCenter)
		panel_layout.addWidget(self.caption)
		layout.addWidget(self.panel)
		self.setStyleSheet(OVERLAY_STYLESHEET)

	def show_caption(self, label: str, text: str) -> None:
		self.hide_timer.stop()
		self.current_text = text
		self.eyebrow.setText("LIVE  /  PARTIAL" if label == "PARTIAL" else "CONFIRMED")
		self.caption.setText(text)
		self._move_to_top_center()
		self.show()
		self.raise_()
		if label == "FINAL":
			self.hide_timer.start(5000)

	def show_message(self, message: str, *, hide_after_ms: int | None = None) -> None:
		self.hide_timer.stop()
		self.current_text = message
		self.eyebrow.setText("QWEN ASR")
		self.caption.setText(message)
		self._move_to_top_center()
		self.show()
		self.raise_()
		if hide_after_ms is not None:
			self.hide_timer.start(hide_after_ms)

	def set_opacity(self, percentage: int) -> None:
		percentage = max(45, min(100, percentage))
		panel_alpha = round(255 * percentage / 100)
		self.setWindowOpacity(1.0)
		self.panel.setStyleSheet(
			"QFrame#overlayPanel { "
			f"background: rgba(0, 0, 0, {panel_alpha}); "
			"border: none; }"
		)

	def _move_to_top_center(self) -> None:
		screen = QApplication.primaryScreen()
		if screen is None:
			return
		area = screen.availableGeometry()
		self.move(area.center().x() - self.width() // 2, area.top() + 14)


OVERLAY_STYLESHEET = """
QWidget#subtitleOverlay { background: transparent; }
QFrame#overlayPanel { background: rgba(0, 0, 0, 180); border: none; }
QLabel#overlayEyebrow { color: #b8df7b; font-size: 10px; font-weight: 700; }
QLabel#overlayCaption { color: #f6f8f2; font-size: 25px; font-weight: 650; }
"""


class SubtitleWindow(QMainWindow):
	def __init__(self) -> None:
		super().__init__()
		self.worker: ASRWorker | None = None
		self.current_label = ""
		self.current_text = ""
		self.transcript_reader: TranscriptReader | None = None
		self.history_is_latest = True
		self.history_refreshing = False
		self.quit_requested = False
		self.settings = QSettings("QwenASRLocal", "ShengMu")
		self.overlay_opacity = max(45, min(100, int(self.settings.value("overlay_opacity", 88))))
		self.overlay = SubtitleOverlay()
		self.overlay.set_opacity(self.overlay_opacity)
		self.setWindowTitle("声幕 | 实时字幕")
		self.resize(940, 620)
		self.setMinimumSize(700, 520)
		self.setStyleSheet(STYLESHEET)
		self._build_ui()
		self._build_tray()
		self._set_status("idle", "准备就绪")

	def _build_ui(self) -> None:
		root = QWidget()
		root.setObjectName("root")
		layout = QVBoxLayout(root)
		layout.setContentsMargins(34, 28, 34, 26)
		layout.setSpacing(22)

		header = QHBoxLayout()
		brand = QVBoxLayout()
		brand.setSpacing(4)
		title = QLabel("声幕")
		title.setObjectName("brand")
		subtitle = QLabel("LOCAL CLIENT  /  QWEN AUDIO 3.1")
		subtitle.setObjectName("eyebrow")
		brand.addWidget(title)
		brand.addWidget(subtitle)
		header.addLayout(brand)
		header.addStretch()
		self.status_dot = QLabel("●")
		self.status_dot.setObjectName("statusDot")
		self.status_label = QLabel()
		self.status_label.setObjectName("statusLabel")
		header.addWidget(self.status_dot)
		header.addWidget(self.status_label)
		layout.addLayout(header)

		self.live_panel = QFrame()
		self.live_panel.setObjectName("livePanel")
		self.live_panel.setMinimumWidth(240)
		live_layout = QVBoxLayout(self.live_panel)
		live_layout.setContentsMargins(26, 22, 26, 24)
		live_layout.setSpacing(12)
		live_heading = QHBoxLayout()
		live_label = QLabel("正在识别")
		live_label.setObjectName("sectionLabel")
		self.partial_badge = QLabel("LIVE")
		self.partial_badge.setObjectName("liveBadge")
		live_heading.addWidget(live_label)
		live_heading.addStretch()
		live_heading.addWidget(self.partial_badge)
		live_layout.addLayout(live_heading)
		self.partial_text = QLabel("按下开始，语音字幕将在这里出现。")
		self.partial_text.setObjectName("partialText")
		self.partial_text.setWordWrap(True)
		self.partial_text.setMinimumHeight(112)
		self.partial_text.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
		live_layout.addWidget(self.partial_text)
		history_panel = QFrame()
		history_panel.setObjectName("historyPanel")
		history_panel.setMinimumWidth(240)
		history_layout = QVBoxLayout(history_panel)
		history_layout.setContentsMargins(18, 16, 18, 16)
		history_layout.setSpacing(8)
		history_heading = QHBoxLayout()
		history_title = QLabel("字幕历史")
		history_title.setObjectName("sectionLabel")
		history_hint = QLabel("向上滚动回看更早记录")
		history_hint.setObjectName("archiveLocation")
		history_heading.addWidget(history_title)
		history_heading.addStretch()
		history_heading.addWidget(history_hint)
		history_layout.addLayout(history_heading)
		self.history_text = QPlainTextEdit()
		self.history_text.setObjectName("historyText")
		self.history_text.setReadOnly(True)
		self.history_text.setPlaceholderText("开始听课后，已确认的字幕会显示在这里。")
		self.history_text.verticalScrollBar().valueChanged.connect(self.load_previous_history_page)
		self.history_text.verticalScrollBar().valueChanged.connect(self.load_next_history_page)
		history_layout.addWidget(self.history_text)
		self.history_timer = QTimer(self)
		self.history_timer.setInterval(1000)
		self.history_timer.timeout.connect(self.refresh_history_from_archive)

		content_splitter = QSplitter(Qt.Orientation.Horizontal)
		content_splitter.setObjectName("contentSplitter")
		content_splitter.setChildrenCollapsible(False)
		content_splitter.addWidget(self.live_panel)
		content_splitter.addWidget(history_panel)
		content_splitter.setStretchFactor(0, 1)
		content_splitter.setStretchFactor(1, 1)
		content_splitter.setSizes([430, 430])
		layout.addWidget(content_splitter, 1)

		transport = QHBoxLayout()
		transport.setSpacing(10)
		self.start_button = QPushButton("开始识别")
		self.start_button.setObjectName("startButton")
		self.start_button.clicked.connect(self.start_recognition)
		self.pause_button = QPushButton("暂停")
		self.pause_button.setObjectName("pauseButton")
		self.pause_button.setEnabled(False)
		self.pause_button.clicked.connect(self.toggle_pause)
		self.stop_button = QPushButton("结束识别")
		self.stop_button.setObjectName("stopButton")
		self.stop_button.setEnabled(False)
		self.stop_button.clicked.connect(self.stop_recognition)
		transport.addWidget(self.start_button)
		transport.addWidget(self.pause_button)
		transport.addWidget(self.stop_button)
		transport.addStretch()
		footer = QHBoxLayout()
		footer.setSpacing(12)
		self.api_key_button = QPushButton("设置 API Key")
		self.api_key_button.setObjectName("clearButton")
		self.api_key_button.clicked.connect(self.configure_api_key)
		self.api_key_status = QLabel()
		self.api_key_status.setObjectName("archiveLocation")
		self.update_api_key_status()
		self.overlay_checkbox = QCheckBox("显示顶部字幕")
		self.overlay_checkbox.setObjectName("overlayCheckbox")
		self.overlay_checkbox.setChecked(True)
		self.overlay_checkbox.toggled.connect(self.set_overlay_enabled)
		self.opacity_label = QLabel("浮窗不透明度")
		self.opacity_label.setObjectName("archiveLocation")
		self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
		self.opacity_slider.setRange(45, 100)
		self.opacity_slider.setFixedWidth(112)
		self.opacity_slider.setValue(self.overlay_opacity)
		self.opacity_slider.setToolTip("调整顶部字幕浮窗的不透明度")
		self.opacity_slider.valueChanged.connect(self.set_overlay_opacity)
		self.opacity_value_label = QLabel(f"{self.overlay_opacity}%")
		self.opacity_value_label.setObjectName("countLabel")
		self.open_archive_button = QPushButton("打开存档目录")
		self.open_archive_button.setObjectName("clearButton")
		self.open_archive_button.clicked.connect(self.open_archive_folder)
		footer.addWidget(self.api_key_button)
		footer.addWidget(self.api_key_status)
		footer.addStretch()
		footer.addWidget(self.open_archive_button)
		footer.addWidget(self.opacity_label)
		footer.addWidget(self.opacity_slider)
		footer.addWidget(self.opacity_value_label)
		footer.addWidget(self.overlay_checkbox)
		layout.addLayout(transport)
		layout.addLayout(footer)
		self.setCentralWidget(root)

	def _set_status(self, kind: str, message: str) -> None:
		labels = {
			"idle": ("待机", "#69777c"),
			"started": ("正在聆听", "#a7d76d"),
			"paused": ("已暂停", "#efba72"),
			"warning": ("音频警告", "#efba72"),
			"error": ("连接异常", "#e47e76"),
			"stopped": ("已停止", "#89979b"),
		}
		label, color = labels.get(kind, (message, "#a7d76d"))
		self.status_label.setText(label)
		self.status_dot.setStyleSheet(f"color: {color};")
		self.status_label.setToolTip(message)
		if kind == "started":
			self.partial_badge.setText("LIVE")
			self.pause_button.setText("暂停")
			self.pause_button.setEnabled(self.worker is not None and self.worker.isRunning())
			if self.overlay_checkbox.isChecked():
				self.overlay.show_message("正在聆听 · 字幕将在屏幕顶部显示")
		elif kind == "paused":
			self.partial_badge.setText("PAUSED")
			if self.overlay_checkbox.isChecked():
				self.overlay.show_message("识别已暂停")
		elif kind in {"stopped", "error", "idle"}:
			self.partial_badge.setText(kind.upper())
			self.pause_button.setEnabled(False)
			if kind == "error" and self.overlay_checkbox.isChecked():
				self.overlay.show_message("识别连接异常", hide_after_ms=5000)
			elif kind in {"stopped", "idle"}:
				self.overlay.hide_timer.start(1400)

	def start_recognition(self) -> None:
		if self.worker is not None and self.worker.isRunning():
			return
		self.worker = ASRWorker()
		self.worker.subtitle_updated.connect(self.update_subtitle)
		self.worker.status_updated.connect(self._set_status)
		self.worker.archive_path_updated.connect(self.set_history_archive)
		self.worker.finished.connect(self.recognition_finished)
		self.history_text.clear()
		self.transcript_reader = None
		self.history_is_latest = True
		self.history_timer.stop()
		self.start_button.setEnabled(False)
		self.stop_button.setEnabled(True)
		self.pause_button.setEnabled(False)
		self.pause_button.setText("暂停")
		self._set_status("started", "正在建立识别连接")
		self.partial_text.setText("正在连接 Qwen…")
		self.worker.start()

	def stop_recognition(self) -> None:
		if self.worker is not None and self.worker.isRunning():
			self.stop_button.setEnabled(False)
			self.pause_button.setEnabled(False)
			self._set_status("stopped", "正在安全结束识别")
			self.worker.request_stop()

	def toggle_pause(self) -> None:
		worker = self.worker
		if worker is None or not worker.isRunning():
			return
		paused = not worker.pause_event.is_set()
		worker.request_pause(paused)
		if paused:
			self.pause_button.setText("继续")
			self._set_status("paused", "识别已暂停，麦克风帧不会发送到服务端")
		else:
			self._set_status("started", "识别已继续")

	def update_subtitle(self, label: str, text: str) -> None:
		self.current_label = label
		self.current_text = text
		self.partial_text.setText(text if label == "PARTIAL" else "等待下一句…")
		if self.overlay_checkbox.isChecked():
			self.overlay.show_caption(label, text)
		if label == "FINAL":
			if self.transcript_reader is None:
				self.history_text.appendPlainText(text)

	def set_history_archive(self, file_path: str) -> None:
		self.transcript_reader = TranscriptReader(Path(file_path))
		self.history_is_latest = True
		self.history_timer.start()
		self.refresh_history_from_archive()

	def refresh_history_from_archive(self) -> None:
		reader = self.transcript_reader
		if reader is None or not reader.file_path.exists():
			return
		if reader.file_path.stat().st_size == reader.last_seen_size:
			return
		if not self.history_is_latest:
			reader.last_seen_size = reader.file_path.stat().st_size
			return
		entries = reader.read_latest_page()
		self.history_refreshing = True
		self.history_text.setPlainText("\n\n".join(entries))
		bar = self.history_text.verticalScrollBar()
		bar.setValue(bar.maximum())
		self.history_refreshing = False

	def load_previous_history_page(self, value: int) -> None:
		reader = self.transcript_reader
		if (
			self.history_refreshing
			or value != self.history_text.verticalScrollBar().minimum()
			or reader is None
		):
			return
		entries = reader.read_previous_page()
		if not entries:
			return
		self.history_is_latest = False
		bar = self.history_text.verticalScrollBar()
		self.history_refreshing = True
		self.history_text.setPlainText("\n\n".join(entries))
		bar.setValue(bar.maximum())
		self.history_refreshing = False

	def load_next_history_page(self, value: int) -> None:
		reader = self.transcript_reader
		if (
			self.history_refreshing
			or value != self.history_text.verticalScrollBar().maximum()
			or reader is None
			or self.history_is_latest
		):
			return
		entries = reader.read_next_page()
		if not reader.newer_page_ends:
			self.history_is_latest = True
			entries = reader.read_latest_page()
		if not entries:
			return
		self.history_refreshing = True
		self.history_text.setPlainText("\n\n".join(entries))
		self.history_text.verticalScrollBar().setValue(0)
		self.history_refreshing = False

	def recognition_finished(self) -> None:
		worker = self.worker
		self.worker = None
		self.history_timer.stop()
		self.start_button.setEnabled(True)
		self.stop_button.setEnabled(False)
		self.pause_button.setEnabled(False)
		self.pause_button.setText("暂停")
		if self.status_label.text() == "正在聆听":
			self._set_status("stopped", "识别已结束")
		if worker is not None:
			worker.deleteLater()

	def set_overlay_enabled(self, enabled: bool) -> None:
		if not enabled:
			self.overlay.hide()
		elif self.current_text:
			self.overlay.show_caption(self.current_label, self.current_text)
		elif self.worker is not None and self.worker.isRunning():
			self.overlay.show_message("正在聆听 · 字幕将在屏幕顶部显示")

	def open_archive_folder(self) -> None:
		archive_directory = Path(__file__).resolve().parent / "transcripts"
		archive_directory.mkdir(parents=True, exist_ok=True)
		QDesktopServices.openUrl(QUrl.fromLocalFile(str(archive_directory)))

	def configure_api_key(self) -> None:
		api_key, accepted = QInputDialog.getText(
			self,
			"设置 DashScope API Key",
			"输入百炼 API Key（输入内容会隐藏）：",
			QLineEdit.EchoMode.Password,
		)
		if not accepted:
			return
		try:
			save_api_key(api_key)
		except ValueError:
			QMessageBox.warning(self, "无法保存", "API Key 不能为空。")
			return
		except OSError:
			QMessageBox.critical(self, "无法保存", "写入项目 .env 失败，请检查文件权限。")
			return
		self.update_api_key_status()
		QMessageBox.information(self, "已保存", "API Key 已写入项目 .env；界面不会显示密钥内容。")

	def update_api_key_status(self) -> None:
		configured = is_api_key_configured()
		self.api_key_status.setText("API Key 已配置" if configured else "API Key 未配置")
		self.api_key_status.setToolTip("密钥内容不会显示在客户端界面。")

	def set_overlay_opacity(self, percentage: int) -> None:
		self.overlay.set_opacity(percentage)
		self.opacity_value_label.setText(f"{percentage}%")
		self.settings.setValue("overlay_opacity", percentage)

	def _build_tray(self) -> None:
		self.tray = QSystemTrayIcon(self)
		self.tray.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
		self.tray.setToolTip("声幕实时字幕")
		menu = QMenu()
		show_action = menu.addAction("显示控制窗口")
		show_action.triggered.connect(self.show_from_tray)
		menu.addSeparator()
		start_action = menu.addAction("开始识别")
		start_action.triggered.connect(self.start_recognition)
		stop_action = menu.addAction("停止识别")
		stop_action.triggered.connect(self.stop_recognition)
		menu.addSeparator()
		quit_action = menu.addAction("退出声幕")
		quit_action.triggered.connect(self.quit_application)
		self.tray.setContextMenu(menu)
		self.tray.activated.connect(self.tray_activated)
		if QSystemTrayIcon.isSystemTrayAvailable():
			self.tray.show()

	def tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
		if reason in {
			QSystemTrayIcon.ActivationReason.Trigger,
			QSystemTrayIcon.ActivationReason.DoubleClick,
		}:
			self.show_from_tray()

	def show_from_tray(self) -> None:
		self.showNormal()
		self.raise_()
		self.activateWindow()

	def quit_application(self) -> None:
		self.quit_requested = True
		if self.worker is not None and self.worker.isRunning():
			self.worker.request_stop()
		self.close()

	def closeEvent(self, event: QCloseEvent) -> None:
		if not self.quit_requested:
			if self.tray.isVisible():
				self.hide()
				event.ignore()
				return
			self.quit_requested = True
		if self.worker is not None and self.worker.isRunning():
			self.worker.request_stop()
			if not self.worker.wait(12_000):
				event.ignore()
				self.worker.finished.connect(self.close)
				return
		self.tray.hide()
		self.overlay.close()
		event.accept()
		if self.quit_requested:
			QApplication.quit()


STYLESHEET = """
QMainWindow, QWidget#root { background: #101719; color: #e9efeb; }
QLabel#brand { color: #eef4ee; font-size: 31px; font-weight: 700; }
QLabel#eyebrow { color: #80908d; font-size: 10px; font-weight: 700; }
QLabel#statusDot { font-size: 15px; }
QLabel#statusLabel { color: #bac5bf; font-size: 12px; }
QFrame#livePanel { background: #1a2526; border: 1px solid #2b3b38; border-radius: 10px; }
QFrame#historyPanel { background: #141d1f; border: 1px solid #273432; border-radius: 8px; }
QSplitter#contentSplitter { background: transparent; }
QSplitter#contentSplitter::handle { background: #34433e; width: 5px; margin: 8px 0; }
QSplitter#contentSplitter::handle:hover { background: #b8df7b; }
QLabel#sectionLabel { color: #9aaa9f; font-size: 12px; font-weight: 600; }
QLabel#liveBadge { color: #b9df83; background: #29392d; border-radius: 4px; padding: 5px 9px; font-size: 10px; font-weight: 700; }
QLabel#partialText { color: #f0f3ec; font-size: 27px; font-weight: 500; }
QPlainTextEdit#historyText { background: #101719; color: #d9e2dc; border: 1px solid #273432; border-radius: 5px; padding: 8px; font-size: 15px; }
QPlainTextEdit#historyText QScrollBar:vertical { width: 8px; background: #101719; }
QPlainTextEdit#historyText QScrollBar::handle:vertical { background: #52615a; border-radius: 4px; min-height: 24px; }
QLabel#archiveTitle { color: #e2e9e3; font-size: 14px; font-weight: 650; }
QLabel#archiveLocation { color: #82928a; font-size: 12px; }
QLabel#countLabel { color: #82928a; font-size: 12px; }
QPushButton { min-height: 42px; padding: 0 18px; border-radius: 6px; font-size: 13px; font-weight: 650; }
QPushButton#startButton { background: #b8df7b; color: #142019; border: 0; }
QPushButton#startButton:hover { background: #c9ec90; }
QPushButton#startButton:disabled { background: #4b594b; color: #9eaa9b; }
QPushButton#pauseButton { background: #d6ad62; color: #211d13; border: 0; }
QPushButton#pauseButton:hover { background: #e3c078; }
QPushButton#pauseButton:disabled { background: #4a4538; color: #9c927d; }
QPushButton#stopButton { background: #273333; color: #e6ece5; border: 1px solid #3c4c47; }
QPushButton#stopButton:hover { background: #33423f; }
QPushButton#stopButton:disabled { color: #72807b; }
QPushButton#clearButton { background: transparent; color: #aab7af; border: 1px solid #34413e; }
QPushButton#clearButton:hover { color: #eef4ee; border-color: #738579; }
QCheckBox#pinCheckbox { color: #aab7af; spacing: 8px; font-size: 12px; padding-left: 8px; }
QCheckBox#pinCheckbox::indicator { width: 16px; height: 16px; border: 1px solid #52615a; border-radius: 3px; background: #182122; }
QCheckBox#pinCheckbox::indicator:checked { background: #b8df7b; border-color: #b8df7b; }
"""


def launch_gui() -> int:
	app = QApplication(sys.argv)
	app.setApplicationName("声幕")
	app.setQuitOnLastWindowClosed(False)
	window = SubtitleWindow()
	window.show()
	return app.exec()