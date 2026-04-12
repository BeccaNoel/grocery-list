from __future__ import annotations

import argparse
import importlib
import inspect
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import requests

from admin_ui import start_admin_ui
from config import ConfigError, ENV_FILE, Settings, load_settings, validate_required_env
from health import record_event, set_mode_running, update_connection
from notifier import configure_logging, log_action, log_exception, notify
from skylight import SkylightError, authenticate


class StartupError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Home grocery automation system")
    parser.add_argument(
        "--mode",
        choices=("passive", "gesture", "both"),
        required=True,
        help="Which runtime mode to start",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()

    try:
        settings = load_runtime_settings()
        configure_logging(settings=settings)

        log_action(
            "Application startup initiated",
            action="startup_begin",
            metadata={"mode": args.mode},
        )

        check_env_file_protection(settings)
        verify_ollama(settings)
        verify_skylight(settings)
        print_startup_summary(settings, args.mode)
        admin_url = start_admin_ui(settings)
        record_event("admin-ui", "Administrator UI started", {"url": admin_url})
        print(f"Admin UI: {admin_url}")
        run_mode(args.mode, settings)
        return 0
    except KeyboardInterrupt:
        log_action("Application stopped by user", action="shutdown_keyboard_interrupt")
        print("Stopped by user.")
        set_mode_running("passive", False)
        set_mode_running("gesture", False)
        return 130
    except (StartupError, ConfigError, SkylightError) as exc:
        log_action(
            str(exc),
            action="startup_failed",
            level=40,
            metadata={"error_type": type(exc).__name__},
        )
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "Unhandled exception during application startup or runtime",
            action="startup_unhandled_exception",
            metadata={"error_type": type(exc).__name__},
        )
        print(f"Unhandled error: {exc}", file=sys.stderr)
        return 1


def load_runtime_settings() -> Settings:
    missing_env_vars = validate_required_env()
    if missing_env_vars:
        joined = ", ".join(missing_env_vars)
        raise StartupError(f"Missing required environment variables: {joined}")

    try:
        settings = load_settings()
    except ConfigError as exc:
        raise StartupError(str(exc)) from exc

    log_action(
        "Configuration validation succeeded",
        action="config_validation_success",
        metadata={"env_file": str(ENV_FILE)},
    )
    record_event("startup", "Configuration validation succeeded", {"env_file": str(ENV_FILE)})
    return settings


def check_env_file_protection(settings: Settings) -> None:
    gitignore_path = settings_path(".gitignore")
    git_exclude_path = settings_path(".git/info/exclude")
    tracked_locations = [path for path in (gitignore_path, git_exclude_path) if path.exists()]

    if not tracked_locations:
        log_action(
            "No git ignore files found while checking .env protection",
            action="env_ignore_check_skipped",
            level=30,
            metadata={"env_file": str(ENV_FILE)},
        )
        return

    env_ignored = any(_file_contains_env_ignore(path) for path in tracked_locations)
    if env_ignored:
        log_action(
            ".env is protected by git ignore rules",
            action="env_ignore_check_success",
            metadata={"checked_files": [str(path) for path in tracked_locations]},
        )
        return

    log_action(
        ".env is not ignored by git configuration",
        action="env_ignore_check_failed",
        level=30,
        metadata={"checked_files": [str(path) for path in tracked_locations]},
    )


def verify_ollama(settings: Settings) -> None:
    endpoint = f"{settings.ollama_host}/api/tags"
    log_action(
        "Checking Ollama connectivity",
        action="ollama_healthcheck_start",
        metadata={"endpoint": endpoint, "model": settings.ollama_model},
    )

    try:
        response = requests.get(endpoint, timeout=settings.ollama_timeout_seconds)
        response.raise_for_status()
    except requests.RequestException as exc:
        log_exception(
            "Failed to reach Ollama",
            action="ollama_healthcheck_failed",
            metadata={"endpoint": endpoint, "error_type": type(exc).__name__},
        )
        raise StartupError(f"Unable to reach Ollama at {endpoint}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise StartupError("Ollama /api/tags returned invalid JSON") from exc

    available_models = _extract_ollama_models(payload)
    requested_model = settings.ollama_model.lower()
    matched = any(model == requested_model or model.startswith(f"{requested_model}:") for model in available_models)
    if not matched:
        raise StartupError(
            f"Ollama model '{settings.ollama_model}' is not available. Found: {', '.join(sorted(available_models)) or 'none'}"
        )

    log_action(
        "Ollama connectivity verified",
        action="ollama_healthcheck_success",
        metadata={"endpoint": endpoint, "available_models": sorted(available_models)},
    )
    update_connection("ollama", "healthy")
    update_connection("llava", "healthy")


def verify_skylight(settings: Settings) -> None:
    try:
        authenticate(settings)
    except SkylightError as exc:
        raise StartupError("Unable to authenticate with Skylight") from exc

    log_action(
        "Skylight connectivity verified",
        action="skylight_healthcheck_success",
        metadata={"frame_id": settings.skylight_frame_id, "list_id": settings.skylight_list_id},
    )
    update_connection("skylight_auth", "healthy")
    update_connection("skylight_api", "healthy")


def print_startup_summary(settings: Settings, mode: str) -> None:
    summary = (
        "Startup summary\n"
        f"  Mode: {mode}\n"
        f"  Camera source: {settings.camera_source}\n"
        f"  Scan interval (hours): {settings.scan_interval_hours}\n"
        f"  Staples count: {len(settings.staples)}\n"
        f"  Staples: {', '.join(settings.staples)}"
    )
    print(summary)
    notify(
        f"Startup complete in {mode} mode",
        send_desktop=False,
        action="startup_summary",
        metadata={
            "mode": mode,
            "camera_source": str(settings.camera_source),
            "scan_interval_hours": settings.scan_interval_hours,
            "staples_count": len(settings.staples),
        },
    )


def run_mode(mode: str, settings: Settings) -> None:
    if mode == "passive":
        run_passive_mode(settings)
        return

    if mode == "gesture":
        run_gesture_mode(settings)
        return

    passive_thread = threading.Thread(
        target=run_passive_mode,
        args=(settings,),
        daemon=True,
        name="passive-mode-thread",
    )
    passive_thread.start()
    log_action("Passive mode started in background thread", action="passive_mode_thread_started")
    run_gesture_mode(settings)


def run_passive_mode(settings: Settings) -> None:
    runner = load_mode_runner("passive_mode", ("run", "run_passive_mode", "main"))
    log_action("Starting passive mode", action="passive_mode_start")
    invoke_runner(runner, settings)


def run_gesture_mode(settings: Settings) -> None:
    runner = load_mode_runner("gesture_mode", ("run", "run_gesture_mode", "main"))
    log_action("Starting gesture mode", action="gesture_mode_start")
    invoke_runner(runner, settings)


def load_mode_runner(module_name: str, candidate_names: tuple[str, ...]) -> Callable[..., Any]:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise StartupError(
                f"Mode module '{module_name}.py' has not been built yet. Build it before running this mode."
            ) from exc
        raise

    for name in candidate_names:
        runner = getattr(module, name, None)
        if callable(runner):
            return runner

    raise StartupError(
        f"Module '{module_name}.py' does not expose a runnable entry point. Expected one of: {', '.join(candidate_names)}"
    )


def invoke_runner(runner: Callable[..., Any], settings: Settings) -> None:
    signature = inspect.signature(runner)
    parameters = signature.parameters

    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        runner(settings=settings)
        return

    if "settings" in parameters:
        runner(settings=settings)
        return

    positional_parameters = [
        parameter
        for parameter in parameters.values()
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if positional_parameters:
        runner(settings)
        return

    runner()


def settings_path(relative_path: str) -> Path:
    return ENV_FILE.resolve().parent / relative_path


def _file_contains_env_ignore(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    normalized_entries = {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}
    return ".env" in normalized_entries or "*.env" in normalized_entries


def _extract_ollama_models(payload: dict[str, Any]) -> set[str]:
    models = payload.get("models", [])
    discovered: set[str] = set()
    if not isinstance(models, list):
        return discovered

    for entry in models:
        if not isinstance(entry, dict):
            continue
        for key in ("name", "model"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                discovered.add(value.strip().lower())
    return discovered


if __name__ == "__main__":
    raise SystemExit(main())