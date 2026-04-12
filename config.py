from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

DEFAULT_STAPLES = [
    "fairlife milk",
    "organiceggs",
    "olive oil",
    "whole wheatbread",
    "coffee",
    "yellow onions",
    "chickpea pasta",
    "brown rice",
    "chicken breasts",
    "stringcheese",
    "plain greek yogurt",
    "raspberries",
    "apples",
    "bananas",
    "toastedsesame oil",
    "no scentdish soap",
    "carrots",
    "green beans",
    "snap pea crisps",
    "cashews",
    "triscuts",
    "cheerios",
    "granola",
    "gluten free oatmeal",
    "no sugar justin's peanut butter",
    "peanut butter",
    "black olives", 
    "plastic sandwhich lunch bags", 
    "gallon ziplock bags",
    "baking parchment paper",
    "organic ground turkey",                                    
    "frozen banza pizza crust",
    "pasta sauce",
    "yellow mustard", 
    "primal kitchen ketchup",
    "cucumber",
    "zucchini", 
    "broccoli",                    
]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    skylight_email: str
    skylight_password: str
    skylight_frame_id: str
    skylight_list_id: str
    camera_index: str
    ollama_host: str
    ollama_model: str
    scan_interval_hours: int
    door_open_detection_enabled: bool
    door_open_sample_fps: int
    door_open_motion_ratio_threshold: float
    door_open_intensity_threshold: int
    door_open_settle_seconds: float
    door_open_cooldown_seconds: int
    door_open_min_motion_seconds: float
    pointing_hold_seconds: int
    flag_threshold: int
    unknown_item_threshold: int
    camera_retry_attempts: int
    camera_retry_delay_seconds: int
    ollama_timeout_seconds: int
    skylight_timeout_seconds: int
    admin_ui_refresh_seconds: int
    admin_ui_host: str
    admin_ui_port: int
    log_retention_days: int
    log_file_max_bytes: int
    log_backup_count: int
    staples: list[str]

    @property
    def camera_source(self) -> int | str:
        value = self.camera_index.strip()
        if value.isdigit():
            return int(value)
        return value


def load_settings(dotenv_path: Path | None = None) -> Settings:
    load_dotenv(dotenv_path or ENV_FILE)

    return Settings(
        skylight_email=_get_required_env("SKYLIGHT_EMAIL"),
        skylight_password=_get_required_env("SKYLIGHT_PASSWORD"),
        skylight_frame_id=_get_required_env("SKYLIGHT_FRAME_ID"),
        skylight_list_id=_get_required_env("SKYLIGHT_LIST_ID"),
        camera_index=os.getenv("CAMERA_INDEX", "0"),
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llava"),
        scan_interval_hours=_get_int_env("SCAN_INTERVAL_HOURS", 4, minimum=1),
        door_open_detection_enabled=_get_bool_env("DOOR_OPEN_DETECTION_ENABLED", True),
        door_open_sample_fps=_get_int_env("DOOR_OPEN_SAMPLE_FPS", 5, minimum=1),
        door_open_motion_ratio_threshold=_get_float_env("DOOR_OPEN_MOTION_RATIO_THRESHOLD", 0.08, minimum=0.001),
        door_open_intensity_threshold=_get_int_env("DOOR_OPEN_INTENSITY_THRESHOLD", 25, minimum=1),
        door_open_settle_seconds=_get_float_env("DOOR_OPEN_SETTLE_SECONDS", 1.25, minimum=0.1),
        door_open_cooldown_seconds=_get_int_env("DOOR_OPEN_COOLDOWN_SECONDS", 20, minimum=1),
        door_open_min_motion_seconds=_get_float_env("DOOR_OPEN_MIN_MOTION_SECONDS", 0.35, minimum=0.1),
        pointing_hold_seconds=_get_int_env("POINTING_HOLD_SECONDS", 2, minimum=1),
        flag_threshold=_get_int_env("FLAG_THRESHOLD", 2, minimum=1),
        unknown_item_threshold=_get_int_env("UNKNOWN_ITEM_THRESHOLD", 3, minimum=1),
        camera_retry_attempts=_get_int_env("CAMERA_RETRY_ATTEMPTS", 3, minimum=1),
        camera_retry_delay_seconds=_get_int_env("CAMERA_RETRY_DELAY_SECONDS", 5, minimum=1),
        ollama_timeout_seconds=_get_int_env("OLLAMA_TIMEOUT_SECONDS", 30, minimum=1),
        skylight_timeout_seconds=_get_int_env("SKYLIGHT_TIMEOUT_SECONDS", 10, minimum=1),
        admin_ui_refresh_seconds=_get_int_env("ADMIN_UI_REFRESH_SECONDS", 5, minimum=1),
        admin_ui_host=os.getenv("ADMIN_UI_HOST", "127.0.0.1").strip() or "127.0.0.1",
        admin_ui_port=_get_int_env("ADMIN_UI_PORT", 8765, minimum=1),
        log_retention_days=_get_int_env("LOG_RETENTION_DAYS", 7, minimum=1),
        log_file_max_bytes=_get_int_env("LOG_FILE_MAX_BYTES", 5_242_880, minimum=1),
        log_backup_count=_get_int_env("LOG_BACKUP_COUNT", 10, minimum=1),
        staples=_get_staples(),
    )


def validate_required_env(dotenv_path: Path | None = None) -> list[str]:
    load_dotenv(dotenv_path or ENV_FILE)

    missing = []
    for key in (
        "SKYLIGHT_EMAIL",
        "SKYLIGHT_PASSWORD",
        "SKYLIGHT_FRAME_ID",
        "SKYLIGHT_LIST_ID",
    ):
        if not os.getenv(key, "").strip():
            missing.append(key)
    return missing


def _get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _get_int_env(name: str, default: int, minimum: int | None = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        value = default
    else:
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ConfigError(f"Environment variable {name} must be an integer") from exc

    if minimum is not None and value < minimum:
        raise ConfigError(f"Environment variable {name} must be >= {minimum}")
    return value


def _get_float_env(name: str, default: float, minimum: float | None = None) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        value = default
    else:
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ConfigError(f"Environment variable {name} must be a float") from exc

    if minimum is not None and value < minimum:
        raise ConfigError(f"Environment variable {name} must be >= {minimum}")
    return value


def _get_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Environment variable {name} must be a boolean")


def _get_staples() -> list[str]:
    raw_value = os.getenv("STAPLES")
    if not raw_value:
        return list(DEFAULT_STAPLES)

    staples = [item.strip().lower() for item in raw_value.split(",") if item.strip()]
    if not staples:
        raise ConfigError("STAPLES must contain at least one item when provided")
    return staples