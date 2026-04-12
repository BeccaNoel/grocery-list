from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

try:
    from plyer import notification as plyer_notification
except ImportError:
    plyer_notification = None

try:
    from config import BASE_DIR, Settings
except ImportError:
    BASE_DIR = Path(__file__).resolve().parent
    Settings = Any  # type: ignore[assignment]


DEFAULT_LOGGER_NAME = "grocery_ai"
DEFAULT_LOG_DIR = BASE_DIR / "logs"
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "grocery_ai.log"
MAX_RECENT_EVENTS = 500
HIGH_PRIORITY_TIMEOUT_SECONDS = 10

_recent_events: deque["LogEvent"] = deque(maxlen=MAX_RECENT_EVENTS)
_recent_events_lock = threading.Lock()
_configured_loggers: set[str] = set()


@dataclass(frozen=True)
class LogEvent:
    timestamp: str
    level: str
    logger_name: str
    module: str
    action: str
    message: str
    metadata: dict[str, Any]


class SafeExtraFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "action"):
            record.action = "log"
        if not hasattr(record, "metadata"):
            record.metadata = {}

        if isinstance(record.metadata, dict) and record.metadata:
            metadata_text = json.dumps(record.metadata, sort_keys=True, default=str)
            record.metadata_text = f" | metadata={metadata_text}"
        else:
            record.metadata_text = ""

        return super().format(record)


class InMemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        event = LogEvent(
            timestamp=datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds"),
            level=record.levelname,
            logger_name=record.name,
            module=record.module,
            action=getattr(record, "action", "log"),
            message=record.getMessage(),
            metadata=_normalize_metadata(getattr(record, "metadata", {})),
        )

        with _recent_events_lock:
            _recent_events.appendleft(event)

        try:
            from health import record_log_event

            record_log_event(event)
        except Exception:
            pass


def configure_logging(
    *,
    settings: Settings | None = None,
    logger_name: str = DEFAULT_LOGGER_NAME,
    log_file: str | Path | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    logger = logging.getLogger(logger_name)
    if logger_name in _configured_loggers:
        logger.setLevel(level)
        return logger

    logger.setLevel(level)
    logger.propagate = False

    log_path = Path(log_file) if log_file is not None else DEFAULT_LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = SafeExtraFormatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(module)s | %(action)s | %(message)s%(metadata_text)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=_get_setting_value(settings, "log_file_max_bytes", 5_242_880),
        backupCount=_get_setting_value(settings, "log_backup_count", 10),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    memory_handler = InMemoryLogHandler()
    memory_handler.setLevel(logging.DEBUG)

    logger.handlers.clear()
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.addHandler(memory_handler)

    _configured_loggers.add(logger_name)
    logger.info(
        "Logging configured",
        extra={
            "action": "logging_configured",
            "metadata": {
                "log_file": str(log_path),
                "log_backup_count": _get_setting_value(settings, "log_backup_count", 10),
            },
        },
    )
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name or DEFAULT_LOGGER_NAME)


def notify(
    message: str,
    *,
    title: str = "Grocery AI",
    level: int = logging.INFO,
    send_desktop: bool = True,
    logger_name: str = DEFAULT_LOGGER_NAME,
    action: str = "notify",
    metadata: dict[str, Any] | None = None,
) -> None:
    logger = configure_logging(logger_name=logger_name)
    logger.log(
        level,
        message,
        extra={
            "action": action,
            "metadata": _normalize_metadata(metadata),
        },
    )

    if send_desktop and plyer_notification is not None:
        plyer_notification.notify(title=title, message=message, app_name=title)


def notify_clear(
    item_count: int,
    *,
    logger_name: str = DEFAULT_LOGGER_NAME,
    send_desktop: bool = True,
) -> None:
    message = f"Grocery list cleared — {item_count} items removed. Ready for next delivery!"
    logger = configure_logging(logger_name=logger_name)
    logger.info(
        message,
        extra={
            "action": "clear_list_completed",
            "metadata": {"item_count": item_count},
        },
    )

    if send_desktop and plyer_notification is not None:
        plyer_notification.notify(
            title="Grocery AI",
            message=message,
            app_name="Grocery AI",
            timeout=HIGH_PRIORITY_TIMEOUT_SECONDS,
        )


def log_action(
    message: str,
    *,
    action: str,
    level: int = logging.INFO,
    logger_name: str = DEFAULT_LOGGER_NAME,
    metadata: dict[str, Any] | None = None,
) -> None:
    logger = configure_logging(logger_name=logger_name)
    logger.log(
        level,
        message,
        extra={
            "action": action,
            "metadata": _normalize_metadata(metadata),
        },
    )


def log_exception(
    message: str,
    *,
    action: str,
    logger_name: str = DEFAULT_LOGGER_NAME,
    metadata: dict[str, Any] | None = None,
) -> None:
    logger = configure_logging(logger_name=logger_name)
    logger.exception(
        message,
        extra={
            "action": action,
            "metadata": _normalize_metadata(metadata),
        },
    )


def get_recent_log_entries(
    *,
    limit: int = 100,
    level: str | None = None,
    module: str | None = None,
    search_text: str | None = None,
) -> list[dict[str, Any]]:
    normalized_level = level.upper() if level else None
    normalized_module = module.lower() if module else None
    normalized_search = search_text.lower() if search_text else None

    with _recent_events_lock:
        events = list(_recent_events)

    filtered: list[dict[str, Any]] = []
    for event in events:
        if normalized_level and event.level != normalized_level:
            continue
        if normalized_module and event.module.lower() != normalized_module:
            continue
        if normalized_search and normalized_search not in _search_blob(event):
            continue
        filtered.append(asdict(event))
        if len(filtered) >= limit:
            break
    return filtered


def get_log_file_path(log_file: str | Path | None = None) -> Path:
    return Path(log_file) if log_file is not None else DEFAULT_LOG_FILE


def list_recent_actions(limit: int = 50) -> list[dict[str, Any]]:
    return get_recent_log_entries(limit=limit)


def _search_blob(event: LogEvent) -> str:
    metadata = json.dumps(event.metadata, sort_keys=True, default=str).lower()
    return " ".join((event.message.lower(), event.action.lower(), event.module.lower(), metadata))


def _normalize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}

    normalized: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, Path):
            normalized[key] = str(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            normalized[key] = value
        elif isinstance(value, dict):
            normalized[key] = _normalize_metadata(value)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
            normalized[key] = [str(item) for item in value]
        else:
            normalized[key] = str(value)
    return normalized


def _get_setting_value(settings: Settings | None, attribute: str, default: Any) -> Any:
    if settings is None:
        return default
    return getattr(settings, attribute, default)