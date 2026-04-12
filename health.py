from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
import threading
from typing import Any

import requests

from config import Settings, load_settings


DEFAULT_CONNECTIONS = ("camera", "ollama", "llava", "skylight_auth", "skylight_api")
DEFAULT_MODES = ("passive", "gesture")
MAX_RECENT_EVENTS = 200


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ConnectionStatus:
    name: str
    state: str = "unknown"
    last_success_timestamp: str | None = None
    last_failure_timestamp: str | None = None
    error_message: str | None = None
    consecutive_failures: int = 0


@dataclass
class ModeStatus:
    name: str
    running: bool = False
    last_updated_timestamp: str | None = None
    last_passive_scan_timestamp: str | None = None
    last_successful_gesture_detection_timestamp: str | None = None
    last_successful_item_add_timestamp: str | None = None


@dataclass
class IssueStatus:
    code: str
    severity: str
    message: str
    recommended_action: str
    first_detected_timestamp: str
    last_seen_timestamp: str
    active: bool = True
    acknowledged: bool = False
    occurrence_count: int = 1


@dataclass
class HealthEvent:
    timestamp: str
    category: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


class HealthSnapshot:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.connections = {name: ConnectionStatus(name=name) for name in DEFAULT_CONNECTIONS}
        self.modes = {name: ModeStatus(name=name) for name in DEFAULT_MODES}
        self.issues: dict[str, IssueStatus] = {}
        self.recent_events: deque[HealthEvent] = deque(maxlen=MAX_RECENT_EVENTS)

    def update_connection(self, name: str, state: str, *, error_message: str | None = None) -> None:
        with self._lock:
            connection = self.connections.setdefault(name, ConnectionStatus(name=name))
            connection.state = state
            if state == "healthy":
                connection.last_success_timestamp = _now()
                connection.error_message = None
                connection.consecutive_failures = 0
            else:
                connection.last_failure_timestamp = _now()
                connection.error_message = error_message
                connection.consecutive_failures += 1

    def set_mode_running(self, name: str, running: bool) -> None:
        with self._lock:
            mode = self.modes.setdefault(name, ModeStatus(name=name))
            mode.running = running
            mode.last_updated_timestamp = _now()

    def mark_mode_timestamp(self, name: str, field_name: str) -> None:
        with self._lock:
            mode = self.modes.setdefault(name, ModeStatus(name=name))
            setattr(mode, field_name, _now())
            mode.last_updated_timestamp = _now()

    def report_issue(
        self,
        code: str,
        *,
        severity: str,
        message: str,
        recommended_action: str,
    ) -> None:
        timestamp = _now()
        with self._lock:
            issue = self.issues.get(code)
            if issue is None:
                self.issues[code] = IssueStatus(
                    code=code,
                    severity=severity,
                    message=message,
                    recommended_action=recommended_action,
                    first_detected_timestamp=timestamp,
                    last_seen_timestamp=timestamp,
                )
            else:
                issue.severity = severity
                issue.message = message
                issue.recommended_action = recommended_action
                issue.last_seen_timestamp = timestamp
                issue.active = True
                issue.occurrence_count += 1

    def resolve_issue(self, code: str) -> None:
        with self._lock:
            issue = self.issues.get(code)
            if issue is None:
                return
            issue.active = False
            issue.last_seen_timestamp = _now()

    def acknowledge_issue(self, code: str) -> bool:
        with self._lock:
            issue = self.issues.get(code)
            if issue is None:
                return False
            issue.acknowledged = True
            issue.last_seen_timestamp = _now()
            return True

    def record_event(self, category: str, message: str, metadata: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.recent_events.appendleft(
                HealthEvent(timestamp=_now(), category=category, message=message, metadata=metadata or {})
            )

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            overall_status = self._compute_overall_status()
            return {
                "overall_status": overall_status,
                "connections": {key: asdict(value) for key, value in self.connections.items()},
                "modes": {key: asdict(value) for key, value in self.modes.items()},
                "issues": {key: asdict(value) for key, value in self.issues.items()},
                "recent_events": [asdict(event) for event in self.recent_events],
            }

    def _compute_overall_status(self) -> dict[str, str]:
        active_issues = [issue for issue in self.issues.values() if issue.active]
        critical_issues = [issue for issue in active_issues if issue.severity == "critical"]
        warning_issues = [issue for issue in active_issues if issue.severity == "warning"]

        passive_running = self.modes.get("passive", ModeStatus(name="passive")).running
        skylight_auth = self.connections.get("skylight_auth", ConnectionStatus(name="skylight_auth")).state
        skylight_api = self.connections.get("skylight_api", ConnectionStatus(name="skylight_api")).state

        if critical_issues or not passive_running or skylight_auth != "healthy" or skylight_api != "healthy":
            return {
                "state": "unsafe",
                "message": "System is not safe to trust. Check passive mode, Skylight connectivity, or critical issues.",
            }

        if warning_issues or any(connection.state == "degraded" for connection in self.connections.values()):
            return {
                "state": "warning",
                "message": "System is running with warnings. Review active issues before relying on automation.",
            }

        return {
            "state": "healthy",
            "message": "System health looks good.",
        }


_snapshot = HealthSnapshot()


def get_snapshot() -> HealthSnapshot:
    return _snapshot


def update_connection(name: str, state: str, *, error_message: str | None = None) -> None:
    _snapshot.update_connection(name, state, error_message=error_message)


def set_mode_running(name: str, running: bool) -> None:
    _snapshot.set_mode_running(name, running)


def mark_mode_timestamp(name: str, field_name: str) -> None:
    _snapshot.mark_mode_timestamp(name, field_name)


def report_issue(code: str, *, severity: str, message: str, recommended_action: str) -> None:
    _snapshot.report_issue(code, severity=severity, message=message, recommended_action=recommended_action)


def resolve_issue(code: str) -> None:
    _snapshot.resolve_issue(code)


def acknowledge_issue(code: str) -> bool:
    return _snapshot.acknowledge_issue(code)


def record_event(category: str, message: str, metadata: dict[str, Any] | None = None) -> None:
    _snapshot.record_event(category, message, metadata)


def record_log_event(event: Any) -> None:
    _snapshot.record_event("log", getattr(event, "message", ""), getattr(event, "metadata", {}) or {})


def run_health_checks(settings: Settings | None = None) -> dict[str, Any]:
    resolved_settings = settings or load_settings()
    results: dict[str, Any] = {}

    from camera import CameraError, capture_frame
    from skylight import SkylightError, authenticate, get_list_items

    try:
        capture_frame(settings=resolved_settings)
        update_connection("camera", "healthy")
        resolve_issue("camera_reconnect_failures")
        results["camera"] = {"ok": True}
    except CameraError as exc:
        update_connection("camera", "disconnected", error_message=str(exc))
        report_issue(
            "camera_reconnect_failures",
            severity="critical",
            message="Camera health check failed",
            recommended_action="Verify the camera source, power, and local network connectivity.",
        )
        results["camera"] = {"ok": False, "error": str(exc)}

    endpoint = f"{resolved_settings.ollama_host}/api/tags"
    try:
        response = requests.get(endpoint, timeout=resolved_settings.ollama_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        models = [str(entry.get("name", "")).lower() for entry in payload.get("models", []) if isinstance(entry, dict)]
        requested = resolved_settings.ollama_model.lower()
        model_available = any(model == requested or model.startswith(f"{requested}:") for model in models)
        update_connection("ollama", "healthy")
        update_connection("llava", "healthy" if model_available else "degraded", error_message=None if model_available else "Model unavailable")
        if model_available:
            resolve_issue("llava_model_unavailable")
        else:
            report_issue(
                "llava_model_unavailable",
                severity="critical",
                message="Configured LLaVA model is unavailable in Ollama",
                recommended_action=f"Pull or configure the model '{resolved_settings.ollama_model}' in Ollama.",
            )
        results["ollama"] = {"ok": True, "model_available": model_available}
    except requests.RequestException as exc:
        update_connection("ollama", "disconnected", error_message=str(exc))
        update_connection("llava", "unknown", error_message="Ollama unavailable")
        report_issue(
            "ollama_unreachable",
            severity="critical",
            message="Ollama health check failed",
            recommended_action="Verify that Ollama is running locally and listening on the configured host.",
        )
        results["ollama"] = {"ok": False, "error": str(exc)}

    try:
        authenticate(resolved_settings, force_refresh=True)
        update_connection("skylight_auth", "healthy")
        get_list_items(resolved_settings)
        update_connection("skylight_api", "healthy")
        resolve_issue("skylight_auth_failures")
        results["skylight"] = {"ok": True}
    except SkylightError as exc:
        update_connection("skylight_auth", "degraded", error_message=str(exc))
        update_connection("skylight_api", "degraded", error_message=str(exc))
        report_issue(
            "skylight_auth_failures",
            severity="critical",
            message="Skylight health check failed",
            recommended_action="Verify Skylight credentials, frame/list IDs, and external reachability.",
        )
        results["skylight"] = {"ok": False, "error": str(exc)}

    record_event("health-check", "Manual health check completed", results)
    return results