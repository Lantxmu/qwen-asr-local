import faulthandler
import json
import logging
import os
import sys
import tempfile
import threading
import traceback as traceback_module
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType


LOGGER = logging.getLogger("qwen_asr_local")
_CRASH_STREAM = None
_INSTALLED = False


def _log_directory() -> Path:
	if os.name == "nt":
		base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
	else:
		base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
	return base / "QwenASRLocal" / "logs"


def install_diagnostics() -> Path:
	global _CRASH_STREAM, _INSTALLED
	if _INSTALLED:
		return _log_directory()

	log_directory = _log_directory()
	try:
		log_directory.mkdir(parents=True, exist_ok=True)
	except OSError:
		log_directory = Path(tempfile.gettempdir()) / "QwenASRLocal" / "logs"
		log_directory.mkdir(parents=True, exist_ok=True)

	file_handler = RotatingFileHandler(
		log_directory / "application.log",
		maxBytes=2_000_000,
		backupCount=4,
		encoding="utf-8",
	)
	file_handler.setFormatter(
		logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
	)
	LOGGER.setLevel(logging.INFO)
	LOGGER.addHandler(file_handler)
	LOGGER.propagate = False

	previous_exception_hook = sys.excepthook

	def handle_uncaught_exception(
		exception_type: type[BaseException],
		exception: BaseException,
		traceback: TracebackType | None,
	) -> None:
		if issubclass(exception_type, KeyboardInterrupt):
			previous_exception_hook(exception_type, exception, traceback)
			return
		LOGGER.critical(
			"event=uncaught_exception",
			exc_info=(exception_type, exception, traceback),
		)
		previous_exception_hook(exception_type, exception, traceback)

	def handle_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
		LOGGER.critical(
			"event=uncaught_thread_exception thread=%s",
			args.thread.name if args.thread is not None else "unknown",
			exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
		)

	sys.excepthook = handle_uncaught_exception
	threading.excepthook = handle_uncaught_thread_exception
	_CRASH_STREAM = (log_directory / "crash.log").open("a", encoding="utf-8", buffering=1)
	faulthandler.enable(file=_CRASH_STREAM, all_threads=True)
	_INSTALLED = True
	log_event("diagnostics_started", log_directory=str(log_directory))
	return log_directory


def install_qt_message_handler() -> None:
	from PySide6.QtCore import QtMsgType, qInstallMessageHandler

	def handle_qt_message(message_type, context, message: str) -> None:
		levels = {
			QtMsgType.QtDebugMsg: logging.DEBUG,
			QtMsgType.QtInfoMsg: logging.INFO,
			QtMsgType.QtWarningMsg: logging.WARNING,
			QtMsgType.QtCriticalMsg: logging.ERROR,
			QtMsgType.QtFatalMsg: logging.CRITICAL,
		}
		location = ""
		if context.file:
			location = f" ({context.file}:{context.line})"
		LOGGER.log(levels.get(message_type, logging.INFO), "qt%s %s", location, message)

	qInstallMessageHandler(handle_qt_message)


def log_event(event: str, **details: object) -> None:
	serialized = json.dumps(details, ensure_ascii=False, default=str, sort_keys=True)
	LOGGER.info("event=%s details=%s", event, serialized)


def log_exception(event: str, error: BaseException, *, redact: str = "") -> None:
	formatted = "".join(
		traceback_module.format_exception(type(error), error, error.__traceback__)
	)
	if redact:
		formatted = formatted.replace(redact, "[REDACTED]")
	LOGGER.error("event=%s\n%s", event, formatted.rstrip())