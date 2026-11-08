import sys
import threading
from pathlib import Path

from PySide6.QtCore import QLockFile, QSettings, QStandardPaths, QThread, Qt, Signal, QTimer, QUrl
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
	QToolButton,
	QVBoxLayout,
	QWidget,
)

from asr.client import is_api_key_configured, save_api_key
from asr.segment import TranscriptState
from asr.session import run_realtime_asr
from diagnostics import LOGGER, install_qt_message_handler, log_event
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
		log_event("recognition_worker_started")
		try:
			self._run_session()
		except Exception as error:
			LOGGER.exception("event=recognition_worker_exception")
			self.status_updated.emit(
				"error",
				f"识别线程异常：{type(error).__name__}，详情已写入诊断日志。",
			)
		finally:
			log_event("recognition_worker_finished")

	def _run_session(self) -> None:
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
			Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
			self.text(),
		)


class SubtitleOverlay(QWidget):
	def __init__(self) -> None:
		super().__init__(
			None,
			Qt.WindowType.Tool
			| Qt.WindowType.FramelessWindowHint
			| Qt.WindowType.WindowStaysOnTopHint
			| Qt.WindowType.WindowTransparentForInput,
		)
		self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
		self.setObjectName("subtitleOverlay")
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
		self.eyebrow = QLabel("声幕 · 实时字幕")
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
		self.eyebrow.setText("聆听中 · 实时字幕" if label == "PARTIAL" else "已确认 · 实时字幕")
		self.caption.setText(text)
		self._move_to_top_center()
		self.show()
		self.raise_()
		if label == "FINAL":
			self.hide_timer.start(5000)

	def show_message(self, message: str, *, hide_after_ms: int | None = None) -> None:
		self.hide_timer.stop()
		self.current_text = message
		self.eyebrow.setText("声幕 · 实时字幕")
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
			f"background: rgba(33, 53, 48, {panel_alpha}); "
			"border: 1px solid rgba(228, 236, 222, 90); "
			"border-radius: 14px; }"
		)

	def _move_to_top_center(self) -> None:
		screen = QApplication.primaryScreen()
		if screen is None:
			return
		area = screen.geometry()
		self.setFixedWidth(area.width())
		self.move(area.left(), area.top() + 14)


OVERLAY_STYLESHEET = """
QWidget#subtitleOverlay { background: transparent; }
QFrame#overlayPanel { background: rgba(33, 53, 48, 220); border: 1px solid rgba(228, 236, 222, 90); border-radius: 14px; }
QLabel#overlayEyebrow { color: #d5e7be; font-size: 12px; font-weight: 700; }
QLabel#overlayCaption { color: #ffffff; font-size: 27px; font-weight: 650; }
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
		self.setWindowTitle("声幕 · 烟雨听写")
		self.resize(1000, 700)
		self.setMinimumSize(800, 640)
		self.setStyleSheet(STYLESHEET)
		self._build_ui()
		self._build_tray()
		self._set_status("idle", "准备就绪")

	def _build_ui(self) -> None:
		root = QWidget()
		root.setObjectName("root")
		layout = QVBoxLayout(root)
		layout.setContentsMargins(24, 18, 24, 18)
		layout.setSpacing(12)

		header = QHBoxLayout()
		brand = QVBoxLayout()
		brand.setSpacing(4)
		title = QLabel("声幕")
		title.setObjectName("brand")
		subtitle = QLabel("烟雨入声，字字留痕")
		subtitle.setObjectName("eyebrow")
		brand.addWidget(title)
		brand.addWidget(subtitle)
		header.addLayout(brand)
		self.settings_toggle = QToolButton()
		self.settings_toggle.setObjectName("settingsToggle")
		self.settings_toggle.setText("设置")
		self.settings_toggle.setArrowType(Qt.ArrowType.DownArrow)
		self.settings_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
		self.settings_toggle.setCheckable(True)
		self.settings_toggle.setAccessibleName("展开或收起设置菜单")
		self.settings_toggle.setAccessibleDescription("包含 API Key、存档目录和浮窗显示选项")
		self.settings_toggle.toggled.connect(self.toggle_settings)
		header.addWidget(self.settings_toggle, alignment=Qt.AlignmentFlag.AlignVCenter)
		header.addStretch()
		self.status_dot = QLabel("●")
		self.status_dot.setObjectName("statusDot")
		self.status_dot.setAccessibleName("识别状态指示灯")
		self.status_label = QLabel()
		self.status_label.setObjectName("statusLabel")
		self.status_label.setAccessibleName("识别状态")
		self.status_label.setMinimumWidth(88)
		self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
		live_label = QLabel("实时字幕")
		live_label.setObjectName("sectionLabel")
		self.partial_badge = QLabel("聆听中")
		self.partial_badge.setObjectName("liveBadge")
		live_heading.addWidget(live_label)
		live_heading.addStretch()
		live_heading.addWidget(self.partial_badge)
		live_layout.addLayout(live_heading)
		self.partial_text = QLabel("开启聆听，让每句话都在此浮现。")
		self.partial_text.setObjectName("partialText")
		self.partial_text.setAccessibleName("实时字幕")
		self.partial_text.setWordWrap(True)
		self.partial_text.setMinimumHeight(112)
		self.partial_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
		history_hint = QLabel("向上轻滚，回看更早的声音")
		history_hint.setObjectName("archiveLocation")
		history_heading.addWidget(history_title)
		history_heading.addStretch()
		history_heading.addWidget(history_hint)
		history_layout.addLayout(history_heading)
		self.history_text = QPlainTextEdit()
		self.history_text.setObjectName("historyText")
		self.history_text.setAccessibleName("字幕历史记录")
		self.history_text.setAccessibleDescription("只读字幕记录。滚动到顶部可查看更早记录，滚动到底部可返回最新记录。")
		self.history_text.setReadOnly(True)
		self.history_text.setPlaceholderText("每一句确认的字幕，都会安静地留在这里。")
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
		content_splitter.setSizes([560, 400])

		transport = QHBoxLayout()
		transport.setSpacing(10)
		self.start_button = QPushButton("开始识别")
		self.start_button.setObjectName("startButton")
		self.start_button.setShortcut("Ctrl+Return")
		self.start_button.setAccessibleDescription("快捷键 Ctrl+Enter")
		self.start_button.clicked.connect(self.start_recognition)
		self.pause_button = QPushButton("暂停")
		self.pause_button.setObjectName("pauseButton")
		self.pause_button.setShortcut("Ctrl+P")
		self.pause_button.setAccessibleDescription("快捷键 Ctrl+P；再次按下可继续识别")
		self.pause_button.setEnabled(False)
		self.pause_button.clicked.connect(self.toggle_pause)
		self.stop_button = QPushButton("结束识别")
		self.stop_button.setObjectName("stopButton")
		self.stop_button.setShortcut("Ctrl+E")
		self.stop_button.setAccessibleDescription("快捷键 Ctrl+E")
		self.stop_button.setEnabled(False)
		self.stop_button.clicked.connect(self.stop_recognition)
		transport.addWidget(self.start_button)
		transport.addWidget(self.pause_button)
		transport.addWidget(self.stop_button)
		transport.addStretch()
		settings_panel = QFrame()
		settings_panel.setObjectName("settingsPanel")
		self.settings_panel = settings_panel
		self.settings_panel.setVisible(False)
		settings_layout = QVBoxLayout(settings_panel)
		settings_layout.setContentsMargins(16, 12, 16, 12)
		settings_layout.setSpacing(8)
		settings_heading = QLabel("偏好设置")
		settings_heading.setObjectName("sectionLabel")
		settings_layout.addWidget(settings_heading)
		settings_row = QHBoxLayout()
		settings_row.setSpacing(10)
		self.api_key_button = QPushButton("设置 API Key")
		self.api_key_button.setObjectName("clearButton")
		self.api_key_button.setAccessibleDescription("设置或更新语音识别服务的 API Key")
		self.api_key_button.clicked.connect(self.configure_api_key)
		self.api_key_status = QLabel()
		self.api_key_status.setObjectName("archiveLocation")
		self.api_key_status.setAccessibleName("API Key 配置状态")
		self.update_api_key_status()
		self.overlay_checkbox = QCheckBox("显示顶部字幕")
		self.overlay_checkbox.setObjectName("overlayCheckbox")
		self.overlay_checkbox.setAccessibleDescription("在其他窗口上方显示实时识别字幕")
		self.overlay_checkbox.setChecked(True)
		self.overlay_checkbox.toggled.connect(self.set_overlay_enabled)
		self.opacity_label = QLabel("浮窗不透明度")
		self.opacity_label.setObjectName("archiveLocation")
		self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
		self.opacity_label.setBuddy(self.opacity_slider)
		self.opacity_slider.setRange(45, 100)
		self.opacity_slider.setFixedWidth(132)
		self.opacity_slider.setAccessibleName("浮窗不透明度")
		self.opacity_slider.setAccessibleDescription("使用左右方向键调整字幕浮窗不透明度，范围为 45% 到 100%")
		self.opacity_slider.setValue(self.overlay_opacity)
		self.opacity_slider.setToolTip("调整顶部字幕浮窗的不透明度")
		self.opacity_slider.valueChanged.connect(self.set_overlay_opacity)
		self.opacity_value_label = QLabel(f"{self.overlay_opacity}%")
		self.opacity_value_label.setObjectName("countLabel")
		self.opacity_value_label.setAccessibleName("当前浮窗不透明度")
		self.open_archive_button = QPushButton("打开存档目录")
		self.open_archive_button.setObjectName("clearButton")
		self.open_archive_button.setAccessibleDescription("打开本地字幕存档文件夹")
		self.open_archive_button.clicked.connect(self.open_archive_folder)
		settings_row.addWidget(self.api_key_button)
		settings_row.addWidget(self.api_key_status)
		settings_row.addStretch()
		settings_row.addWidget(self.open_archive_button)
		overlay_row = QHBoxLayout()
		overlay_row.setSpacing(10)
		overlay_row.addWidget(self.opacity_label)
		overlay_row.addWidget(self.opacity_slider)
		overlay_row.addWidget(self.opacity_value_label)
		overlay_row.addSpacing(8)
		overlay_row.addWidget(self.overlay_checkbox)
		settings_layout.addLayout(settings_row)
		settings_layout.addLayout(overlay_row)
		layout.addWidget(settings_panel)
		layout.addWidget(content_splitter, 1)
		layout.addLayout(transport)
		self.setCentralWidget(root)
		QWidget.setTabOrder(self.settings_toggle, self.start_button)
		QWidget.setTabOrder(self.start_button, self.pause_button)
		QWidget.setTabOrder(self.pause_button, self.stop_button)
		QWidget.setTabOrder(self.stop_button, self.history_text)
		QWidget.setTabOrder(self.history_text, self.api_key_button)
		QWidget.setTabOrder(self.api_key_button, self.open_archive_button)
		QWidget.setTabOrder(self.open_archive_button, self.opacity_slider)
		QWidget.setTabOrder(self.opacity_slider, self.overlay_checkbox)

	def toggle_settings(self, expanded: bool) -> None:
		self.settings_panel.setVisible(expanded)
		self.settings_toggle.setArrowType(
			Qt.ArrowType.UpArrow if expanded else Qt.ArrowType.DownArrow
		)
		self.settings_toggle.setAccessibleDescription(
			"点击收起设置菜单" if expanded else "点击展开设置菜单"
		)

	def _set_status(self, kind: str, message: str) -> None:
		labels = {
			"idle": ("静候中", "#71847B"),
			"started": ("正在聆听", "#39745B"),
			"paused": ("暂歇片刻", "#9B681E"),
			"warning": ("音频提醒", "#9B681E"),
			"error": ("连接异常", "#A9443D"),
			"stopped": ("已收声", "#71847B"),
		}
		label, color = labels.get(kind, (message, "#a7d76d"))
		self.status_label.setText(label)
		self.status_dot.setStyleSheet(f"color: {color};")
		self.status_label.setToolTip(message)
		self.status_label.setAccessibleDescription(message)
		if kind == "started":
			self.partial_badge.setText("聆听中")
			self.pause_button.setText("暂停")
			self.pause_button.setEnabled(self.worker is not None and self.worker.isRunning())
			if self.overlay_checkbox.isChecked():
				self.overlay.show_message("正在聆听 · 字幕将在屏幕顶部显示")
		elif kind == "paused":
			self.partial_badge.setText("暂歇中")
			if self.overlay_checkbox.isChecked():
				self.overlay.show_message("识别已暂停")
		elif kind in {"stopped", "error", "idle"}:
			self.partial_badge.setText({"stopped": "已收声", "error": "待检查", "idle": "静候中"}[kind])
			self.pause_button.setEnabled(False)
			if kind == "error" and self.overlay_checkbox.isChecked():
				self.overlay.show_message("识别连接异常", hide_after_ms=5000)
			elif kind in {"stopped", "idle"}:
				self.overlay.hide_timer.start(1400)

	def start_recognition(self) -> None:
		if self.worker is not None and self.worker.isRunning():
			return
		log_event("recognition_start_requested")
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
		self.partial_text.setText("正在接通语音服务…")
		self.worker.start()

	def stop_recognition(self) -> None:
		if self.worker is not None and self.worker.isRunning():
			log_event("recognition_stop_requested")
			self.stop_button.setEnabled(False)
			self.pause_button.setEnabled(False)
			self._set_status("stopped", "正在安全结束识别")
			self.worker.request_stop()

	def toggle_pause(self) -> None:
		worker = self.worker
		if worker is None or not worker.isRunning():
			return
		paused = not worker.pause_event.is_set()
		log_event("recognition_pause_changed", paused=paused)
		worker.request_pause(paused)
		if paused:
			self.pause_button.setText("继续")
			self._set_status("paused", "识别已暂停，麦克风帧不会发送到服务端")
		else:
			self._set_status("started", "识别已继续")

	def update_subtitle(self, label: str, text: str) -> None:
		self.current_label = label
		self.current_text = text
		self.partial_text.setText(text if label == "PARTIAL" else "静候下一句…")
		self.partial_text.setAccessibleDescription(text)
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
		self.api_key_status.setText("API Key 已就绪" if configured else "尚未配置 API Key")
		self.api_key_status.setToolTip("密钥内容不会显示在客户端界面。")
		self.api_key_status.setAccessibleDescription("密钥内容不会显示在客户端界面。")

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
		log_event("application_quit_requested", source="tray_or_button")
		self.quit_requested = True
		if self.worker is not None and self.worker.isRunning():
			self.worker.request_stop()
		self.close()

	def closeEvent(self, event: QCloseEvent) -> None:
		if not self.quit_requested:
			if self.tray.isVisible():
				log_event("window_hidden_to_tray")
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
		log_event("main_window_closed")
		event.accept()
		if self.quit_requested:
			QApplication.quit()


STYLESHEET = """
QMainWindow, QWidget#root { background: #F1F3EE; color: #293E39; }
QLabel#brand { color: #263F3A; font-family: "Microsoft YaHei"; font-size: 34px; font-weight: 700; }
QLabel#eyebrow { color: #70847A; font-size: 13px; font-weight: 600; }
QToolButton#settingsToggle { background: transparent; color: #466357; border: 1px solid transparent; border-radius: 5px; padding: 8px 10px; font-size: 14px; font-weight: 600; }
QToolButton#settingsToggle:hover, QToolButton#settingsToggle:checked { background: #E7ECE6; border-color: #D3DDD4; }
QToolButton#settingsToggle:focus { border: 2px solid #356953; }
QLabel#statusDot { color: #668579; font-size: 16px; }
QLabel#statusLabel { background: #E3EBE5; color: #365D4E; border: 1px solid #CBD9CF; border-radius: 12px; padding: 6px 11px; font-size: 14px; font-weight: 650; }
QFrame#livePanel { background: #FBFAF6; border: 1px solid #D7DFD7; border-radius: 8px; }
QFrame#historyPanel { background: #E7EDE8; border: 1px solid #D0DBD3; border-radius: 8px; }
QFrame#settingsPanel { background: #E7ECE6; border: 1px solid #D3DDD4; border-radius: 6px; }
QSplitter#contentSplitter { background: transparent; }
QSplitter#contentSplitter::handle { background: #CFD9D1; width: 7px; margin: 12px 0; border-radius: 3px; }
QSplitter#contentSplitter::handle:hover { background: #719889; }
QLabel#sectionLabel { color: #39584E; font-size: 15px; font-weight: 700; }
QLabel#liveBadge { color: #3E715E; background: #E4EEE5; border: 1px solid #C7D9C9; border-radius: 10px; padding: 5px 11px; font-size: 12px; font-weight: 700; }
QLabel#partialText { color: #294840; font-size: 29px; font-weight: 500; }
QPlainTextEdit#historyText { background: #F8FAF7; color: #2F4941; border: 1px solid #D5DED7; border-radius: 9px; padding: 12px; font-size: 16px; selection-background-color: #B8D0BF; }
QPlainTextEdit#historyText QScrollBar:vertical { width: 12px; background: #EEF2EE; margin: 3px; border-radius: 6px; }
QPlainTextEdit#historyText QScrollBar::handle:vertical { background: #9CAF9F; border-radius: 5px; min-height: 28px; }
QPlainTextEdit#historyText QScrollBar::handle:vertical:hover { background: #718F7E; }
QLabel#archiveTitle { color: #304D44; font-size: 15px; font-weight: 700; }
QLabel#archiveLocation { color: #5E756A; font-size: 13px; }
QLabel#countLabel { color: #526D61; font-size: 13px; font-weight: 650; }
QPushButton { min-height: 42px; padding: 0 16px; border-radius: 6px; font-size: 14px; font-weight: 650; }
QPushButton#startButton { background: #477D68; color: #FFFFFF; border: 1px solid #477D68; }
QPushButton#startButton:hover { background: #396B57; border-color: #396B57; }
QPushButton#startButton:disabled { background: #CBD6CE; color: #66766C; border-color: #CBD6CE; }
QPushButton#startButton:focus { border: 2px solid #244E40; }
QPushButton#pauseButton { background: #E9D6B5; color: #5A4327; border: 1px solid #D5BC91; }
QPushButton#pauseButton:hover { background: #E2C99E; }
QPushButton#pauseButton:disabled { background: #E8E4DA; color: #8D897E; border-color: #DDD8CC; }
QPushButton#pauseButton:focus { border: 2px solid #8C622D; }
QPushButton#stopButton { background: transparent; color: #73534B; border: 1px solid #C9B8AE; }
QPushButton#stopButton:hover { background: #EFE7E1; border-color: #AD8C7E; }
QPushButton#stopButton:disabled { color: #9A9D93; border-color: #D8DCD5; }
QPushButton#clearButton { background: transparent; color: #466357; border: 1px solid #C5D2C8; }
QPushButton#clearButton:hover { background: #FFFFFF; border-color: #7E9F8B; }
QPushButton#stopButton:focus, QPushButton#clearButton:focus { border: 2px solid #356953; }
QPushButton:focus, QPlainTextEdit:focus { border: 2px solid #356953; }
QCheckBox#overlayCheckbox { color: #405D51; spacing: 9px; font-size: 14px; font-weight: 600; padding: 5px 4px; }
QCheckBox#overlayCheckbox::indicator { width: 20px; height: 20px; border: 2px solid #82998B; border-radius: 6px; background: #F8FAF7; }
QCheckBox#overlayCheckbox::indicator:checked { background: #477D68; border-color: #477D68; }
QCheckBox#overlayCheckbox:focus { color: #263F3A; }
QSlider::groove:horizontal { height: 6px; background: #C5D2C8; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #6F9A82; border-radius: 3px; }
QSlider::handle:horizontal { width: 20px; margin: -7px 0; border-radius: 10px; background: #F8FAF7; border: 2px solid #5B866F; }
QSlider::handle:horizontal:focus { border-color: #244E40; }
QToolTip { background: #304B42; color: #F8FAF7; border: 1px solid #6F8C7C; padding: 5px 8px; }
"""


def launch_gui() -> int:
	install_qt_message_handler()
	app = QApplication(sys.argv)
	app.setApplicationName("声幕")
	app.setQuitOnLastWindowClosed(False)
	app.aboutToQuit.connect(lambda: log_event("qt_about_to_quit"))
	lock_path = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)) / "qwen-asr-local.lock"
	instance_lock = QLockFile(str(lock_path))
	if not instance_lock.tryLock(0):
		QMessageBox.information(None, "客户端已运行", "检测到另一个声幕客户端正在运行，请使用已有窗口。")
		return 1
	window = SubtitleWindow()
	window.show()
	log_event("main_window_shown")
	return app.exec()
