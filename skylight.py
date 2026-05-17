from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

import requests

from config import Settings, load_settings
from health import mark_mode_timestamp, record_event, report_issue, resolve_issue, update_connection
from notifier import log_action, log_exception


BASE_URL = "https://app.ourskylight.com"
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CLIENT_NAME = "grocery-ai"
MCP_CLIENT_VERSION = "1.0.0"
MAX_MCP_STDERR_LINES = 50

_default_client: "BaseSkylightClient | None" = None
_default_client_key: tuple[Any, ...] | None = None
_duplicate_skip_count = 0


class SkylightError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkylightItem:
    item_id: str
    name: str


@dataclass
class _PendingResponse:
    event: threading.Event
    payload: dict[str, Any] | None = None


class BaseSkylightClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def authenticate(self, *, force_refresh: bool = False) -> str:
        raise NotImplementedError

    def get_list_items(self) -> list[str]:
        raise NotImplementedError

    def add_item(self, item_name: str) -> bool:
        raise NotImplementedError

    def clear_list(self) -> bool:
        raise NotImplementedError


class ApiSkylightClient(BaseSkylightClient):
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        super().__init__(settings)
        self.session = session or requests.Session()
        self._basic_auth_token: str | None = None

    def authenticate(self, *, force_refresh: bool = False) -> str:
        if self._basic_auth_token and not force_refresh:
            return self._basic_auth_token

        payload = {
            "data": {
                "type": "session",
                "attributes": {
                    "email": self.settings.skylight_email,
                    "password": self.settings.skylight_password,
                },
            }
        }

        log_action(
            "Authenticating with Skylight API",
            action="skylight_authenticate_start",
            metadata={"base_url": BASE_URL, "backend": "api"},
        )

        try:
            response = self.session.post(
                f"{BASE_URL}/api/sessions",
                json=payload,
                timeout=self.settings.skylight_timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            error_metadata = {
                "error_type": type(exc).__name__,
                "backend": "api",
                **_get_response_error_metadata(exc),
            }
            update_connection(
                "skylight_auth",
                "disconnected",
                error_message=_build_error_message(exc, error_metadata),
            )
            report_issue(
                "skylight_auth_failures",
                severity="critical",
                message="Skylight authentication failed",
                recommended_action="Verify Skylight credentials and external connectivity.",
            )
            log_exception(
                "Skylight authentication failed",
                action="skylight_authenticate_failed",
                metadata=error_metadata,
            )
            raise SkylightError("Failed to authenticate with Skylight") from exc

        session_data = response.json().get("data", {})
        user_id = self._extract_attribute(session_data, "user_id")
        token = self._extract_attribute(session_data, "token")
        encoded_token = base64.b64encode(f"{user_id}:{token}".encode("utf-8")).decode("ascii")

        self._basic_auth_token = encoded_token
        self.session.headers.update(
            {
                "Authorization": f"Basic {encoded_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

        log_action(
            "Skylight authentication succeeded",
            action="skylight_authenticate_success",
            metadata={"user_id": user_id, "backend": "api"},
        )
        update_connection("skylight_auth", "healthy")
        resolve_issue("skylight_auth_failures")
        record_event("skylight", "Skylight authentication succeeded", {"user_id": user_id, "backend": "api"})
        return encoded_token

    def get_list_items(self) -> list[str]:
        items = self._get_list_item_records()
        normalized = [item.name.strip().lower() for item in items if item.name.strip()]
        log_action(
            "Fetched Skylight list items",
            action="skylight_get_list_items_success",
            metadata={"item_count": len(normalized), "backend": "api"},
        )
        update_connection("skylight_api", "healthy")
        return normalized

    def add_item(self, item_name: str) -> bool:
        normalized_name = item_name.strip()
        if not normalized_name:
            raise SkylightError("item_name cannot be empty")

        existing_items = self.get_list_items()
        if normalized_name.lower() in existing_items:
            global _duplicate_skip_count
            _duplicate_skip_count += 1
            if _duplicate_skip_count >= 3:
                report_issue(
                    "skylight_duplicate_adds",
                    severity="warning",
                    message="Duplicate add attempts are occurring repeatedly",
                    recommended_action="Review state synchronization between gesture/passive detections and current list items.",
                )
            log_action(
                f"Skipped adding {normalized_name} because it already exists on the Skylight list",
                action="skylight_add_item_skipped_duplicate",
                metadata={"item_name": normalized_name, "backend": "api"},
            )
            return False
        _duplicate_skip_count = 0
        resolve_issue("skylight_duplicate_adds")

        payload = {
            "data": {
                "type": "list_item",
                "attributes": {
                    "name": normalized_name,
                },
            }
        }

        try:
            response = self._request(
                "POST",
                self._items_endpoint,
                json=payload,
            )
        except SkylightError:
            report_issue(
                "skylight_add_failures",
                severity="warning",
                message="Skylight add item requests are failing",
                recommended_action="Verify Skylight API reachability and review the request logs for repeated failures.",
            )
            log_action(
                f"Failed to add {normalized_name} to Skylight",
                action="skylight_add_item_failed",
                level=40,
                metadata={"item_name": normalized_name, "backend": "api"},
            )
            return False

        if response.status_code not in (200, 201):
            report_issue(
                "skylight_add_failures",
                severity="warning",
                message="Skylight add item requests are failing",
                recommended_action="Verify Skylight API reachability and review the request logs for repeated failures.",
            )
            log_action(
                f"Failed to add {normalized_name} to Skylight",
                action="skylight_add_item_failed",
                level=40,
                metadata={"item_name": normalized_name, "status_code": response.status_code, "backend": "api"},
            )
            return False

        log_action(
            f"Added {normalized_name} to Skylight",
            action="skylight_add_item_success",
            metadata={"item_name": normalized_name, "status_code": response.status_code, "backend": "api"},
        )
        update_connection("skylight_api", "healthy")
        resolve_issue("skylight_add_failures")
        mark_mode_timestamp("passive", "last_successful_item_add_timestamp")
        mark_mode_timestamp("gesture", "last_successful_item_add_timestamp")
        record_event("skylight", "Item added to Skylight", {"item_name": normalized_name, "backend": "api"})
        return True

    def clear_list(self) -> bool:
        items = self._get_list_item_records()
        if not items:
            log_action(
                "List already empty",
                action="skylight_clear_list_empty",
                metadata={"item_count": 0, "backend": "api"},
            )
            return True

        deleted_count = 0
        all_succeeded = True
        for item in items:
            try:
                response = self._request("DELETE", f"{self._items_endpoint}/{item.item_id}")
            except SkylightError:
                all_succeeded = False
                log_action(
                    f"Failed to delete Skylight list item {item.name}",
                    action="skylight_delete_item_failed",
                    level=40,
                    metadata={"item_id": item.item_id, "item_name": item.name, "backend": "api"},
                )
                continue

            if response.status_code not in (200, 202, 204):
                all_succeeded = False
                log_action(
                    f"Failed to delete Skylight list item {item.name}",
                    action="skylight_delete_item_failed",
                    level=40,
                    metadata={
                        "item_id": item.item_id,
                        "item_name": item.name,
                        "status_code": response.status_code,
                        "backend": "api",
                    },
                )
                continue

            deleted_count += 1

        log_action(
            "Finished clearing Skylight list",
            action="skylight_clear_list_complete",
            metadata={
                "requested_count": len(items),
                "deleted_count": deleted_count,
                "success": all_succeeded,
                "backend": "api",
            },
        )
        record_event(
            "skylight",
            "Skylight list clear completed",
            {"deleted_count": deleted_count, "success": all_succeeded, "backend": "api"},
        )
        return all_succeeded

    def _get_list_item_records(self) -> list[SkylightItem]:
        response = self._request("GET", self._list_endpoint)
        payload = response.json()
        included = payload.get("included", [])
        items: list[SkylightItem] = []

        for entry in included:
            if entry.get("type") != "list_item":
                continue

            item_id = str(entry.get("id", "")).strip()
            item_name = str(self._extract_attribute(entry, "name", default="")).strip()
            if item_id and item_name:
                items.append(SkylightItem(item_id=item_id, name=item_name))

        return items

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.authenticate()

        timeout = kwargs.pop("timeout", self.settings.skylight_timeout_seconds)
        try:
            response = self.session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            update_connection("skylight_api", "degraded", error_message=str(exc))
            log_exception(
                "Skylight request failed",
                action="skylight_request_exception",
                metadata={"method": method, "url": url, "error_type": type(exc).__name__, "backend": "api"},
            )
            raise SkylightError(f"Skylight request failed for {method} {url}") from exc

        if response.status_code == 401:
            report_issue(
                "skylight_token_refresh_loop",
                severity="warning",
                message="Skylight token refresh was required after unauthorized response",
                recommended_action="Review Skylight session stability and whether tokens are expiring unexpectedly.",
            )
            log_action(
                "Skylight token expired or unauthorized, retrying after re-authentication",
                action="skylight_request_unauthorized",
                metadata={"method": method, "url": url, "backend": "api"},
            )
            self.authenticate(force_refresh=True)
            try:
                response = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                update_connection("skylight_api", "degraded", error_message=str(exc))
                log_exception(
                    "Skylight retry request failed",
                    action="skylight_retry_request_exception",
                    metadata={"method": method, "url": url, "error_type": type(exc).__name__, "backend": "api"},
                )
                raise SkylightError(f"Skylight retry request failed for {method} {url}") from exc

            update_connection("skylight_api", "healthy")
        return response

    @property
    def _list_endpoint(self) -> str:
        if not self.settings.skylight_list_id:
            raise SkylightError("SKYLIGHT_LIST_ID is required when SKYLIGHT_BACKEND=api")
        return (
            f"{BASE_URL}/api/frames/{self.settings.skylight_frame_id}/lists/"
            f"{self.settings.skylight_list_id}"
        )

    @property
    def _items_endpoint(self) -> str:
        return f"{self._list_endpoint}/items"

    @staticmethod
    def _extract_attribute(payload: dict[str, Any], key: str, *, default: Any | None = None) -> Any:
        attributes = payload.get("attributes", {})
        if isinstance(attributes, dict) and key in attributes:
            return attributes[key]
        if key in payload:
            return payload[key]
        if default is not None:
            return default
        raise SkylightError(f"Skylight response is missing required field: {key}")


class _McpProcess:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._process: subprocess.Popen[bytes] | None = None
        self._pending: dict[int, _PendingResponse] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._stderr_lines: deque[str] = deque(maxlen=MAX_MCP_STDERR_LINES)
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._request_id = 0
        self._initialized = False

    def restart(self) -> None:
        with self._lifecycle_lock:
            self.close()
            self.start()

    def close(self) -> None:
        with self._lifecycle_lock:
            process = self._process
            self._process = None
            self._initialized = False
            if process is None:
                return

            try:
                process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            except OSError:
                pass

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._process is not None and self._process.poll() is None:
                return

            command = self.settings.skylight_mcp_command
            if shutil.which(command) is None:
                raise SkylightError(
                    f"Skylight MCP command '{command}' was not found. Install Node.js 18+ and ensure the command is available on PATH."
                )

            argv = [command, *self.settings.skylight_mcp_args]
            env = os.environ.copy()
            if self.settings.skylight_email:
                env["SKYLIGHT_EMAIL"] = self.settings.skylight_email
            if self.settings.skylight_password:
                env["SKYLIGHT_PASSWORD"] = self.settings.skylight_password
            if self.settings.skylight_frame_id:
                env["SKYLIGHT_FRAME_ID"] = self.settings.skylight_frame_id
            if self.settings.skylight_token:
                env["SKYLIGHT_TOKEN"] = self.settings.skylight_token
            env["SKYLIGHT_AUTH_TYPE"] = self.settings.skylight_auth_type
            env["SKYLIGHT_TIMEZONE"] = self.settings.skylight_timezone

            try:
                self._process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
            except OSError as exc:
                raise SkylightError(f"Failed to start Skylight MCP server with command: {' '.join(argv)}") from exc

            if self._process.stdin is None or self._process.stdout is None or self._process.stderr is None:
                self.close()
                raise SkylightError("Skylight MCP server did not provide stdio pipes")

            self._stderr_lines.clear()
            self._initialized = False
            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True, name="skylight-mcp-reader")
            self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True, name="skylight-mcp-stderr")
            self._reader_thread.start()
            self._stderr_thread.start()

            log_action(
                "Skylight MCP server process started",
                action="skylight_mcp_process_started",
                metadata={"command": command, "args": self.settings.skylight_mcp_args},
            )

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.start()
        self._initialize()
        return self._send_request(method, params or {}, timeout_seconds=self.settings.skylight_mcp_timeout_seconds)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def _initialize(self) -> None:
        with self._lifecycle_lock:
            if self._initialized:
                return

            result = self._send_request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION},
                },
                timeout_seconds=self.settings.skylight_mcp_timeout_seconds,
                skip_initialize=True,
            )
            if not result:
                raise SkylightError("Skylight MCP initialize returned an empty result")
            self._send_notification("notifications/initialized", {})
            self._initialized = True

    def _send_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: int,
        skip_initialize: bool = False,
    ) -> dict[str, Any]:
        if not skip_initialize and not self._initialized:
            self._initialize()

        process = self._require_process()
        request_id = self._next_request_id()
        pending = _PendingResponse(event=threading.Event())
        with self._pending_lock:
            self._pending[request_id] = pending

        try:
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            if not pending.event.wait(timeout_seconds):
                stderr_tail = list(self._stderr_lines)
                raise SkylightError(
                    "Timed out waiting for Skylight MCP server response"
                    + (f". Recent server output: {' | '.join(stderr_tail[-3:])}" if stderr_tail else "")
                )

            assert pending.payload is not None
            if "error" in pending.payload:
                error = pending.payload["error"]
                message = str(error.get("message", "Unknown MCP error"))
                raise SkylightError(f"Skylight MCP request failed for {method}: {message}")
            result = pending.payload.get("result")
            if not isinstance(result, dict):
                raise SkylightError(f"Skylight MCP returned an invalid result for {method}")
            return result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            if process.poll() is not None and method != "initialize":
                self._initialized = False

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    def _write_message(self, payload: dict[str, Any]) -> None:
        process = self._require_process()
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                assert process.stdin is not None
                process.stdin.write(body)
                process.stdin.flush()
            except OSError as exc:
                self._initialized = False
                raise SkylightError("Failed to write to Skylight MCP server process") from exc

    def _reader_loop(self) -> None:
        while True:
            process = self._process
            if process is None or process.stdout is None:
                return

            try:
                message = self._read_message(process.stdout)
            except EOFError:
                return
            except Exception as exc:  # noqa: BLE001
                log_exception(
                    "Failed to read from Skylight MCP server",
                    action="skylight_mcp_read_failed",
                    metadata={"error_type": type(exc).__name__},
                )
                return

            response_id = message.get("id")
            if response_id is None:
                continue

            with self._pending_lock:
                pending = self._pending.get(int(response_id))
            if pending is None:
                continue

            pending.payload = message
            pending.event.set()

    def _stderr_loop(self) -> None:
        while True:
            process = self._process
            if process is None or process.stderr is None:
                return

            raw_line = process.stderr.readline()
            if not raw_line:
                return

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            self._stderr_lines.append(line)

            level = 30 if "error" in line.lower() else 20
            log_action(
                "Skylight MCP server output",
                action="skylight_mcp_server_output",
                level=level,
                metadata={"line": line},
            )

    @staticmethod
    def _read_message(stream: Any) -> dict[str, Any]:
        while True:
            line = stream.readline()
            if not line:
                raise EOFError
            stripped = line.strip()
            if not stripped:
                continue

            decoded = stripped.decode("utf-8", errors="replace")
            if decoded.lower().startswith("content-length:"):
                _, value = decoded.split(":", 1)
                content_length = int(value.strip())
                while True:
                    header_line = stream.readline()
                    if not header_line:
                        raise EOFError
                    if header_line in (b"\r\n", b"\n"):
                        break

                body = stream.read(content_length)
                if len(body) != content_length:
                    raise EOFError
                return json.loads(body.decode("utf-8"))

            return json.loads(decoded)

    def _next_request_id(self) -> int:
        with self._pending_lock:
            self._request_id += 1
            return self._request_id

    def _require_process(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None or process.poll() is not None:
            self._initialized = False
            raise SkylightError("Skylight MCP server is not running")
        return process


class McpSkylightClient(BaseSkylightClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._process = _McpProcess(settings)

    def authenticate(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self._process.restart()

        log_action(
            "Authenticating with Skylight MCP backend",
            action="skylight_authenticate_start",
            metadata={
                "backend": "mcp",
                "command": self.settings.skylight_mcp_command,
                "args": self.settings.skylight_mcp_args,
            },
        )

        try:
            result = self._process.call_tool("get_frame_info", {})
            text = _extract_tool_text(result)
        except SkylightError as exc:
            update_connection("skylight_auth", "disconnected", error_message=str(exc))
            report_issue(
                "skylight_auth_failures",
                severity="critical",
                message="Skylight MCP authentication failed",
                recommended_action="Verify MCP server runtime, Skylight credentials or token, and frame ID configuration.",
            )
            log_exception(
                "Skylight MCP authentication failed",
                action="skylight_authenticate_failed",
                metadata={"error_type": type(exc).__name__, "backend": "mcp"},
            )
            raise

        update_connection("skylight_auth", "healthy")
        resolve_issue("skylight_auth_failures")
        record_event("skylight", "Skylight MCP authentication succeeded", {"backend": "mcp"})
        log_action(
            "Skylight MCP authentication succeeded",
            action="skylight_authenticate_success",
            metadata={"backend": "mcp", "frame_info": text},
        )
        return text

    def get_list_items(self) -> list[str]:
        arguments: dict[str, Any] = {"includeCompleted": False}
        if self.settings.skylight_list_name:
            arguments["listName"] = self.settings.skylight_list_name

        try:
            result = self._process.call_tool("get_list_items", arguments)
            text = _extract_tool_text(result)
            items = _parse_list_items_text(text)
        except SkylightError as exc:
            update_connection("skylight_api", "degraded", error_message=str(exc))
            log_exception(
                "Skylight MCP get_list_items failed",
                action="skylight_get_list_items_failed",
                metadata={"error_type": type(exc).__name__, "backend": "mcp"},
            )
            raise

        update_connection("skylight_api", "healthy")
        resolve_issue("skylight_add_failures")
        log_action(
            "Fetched Skylight list items via MCP",
            action="skylight_get_list_items_success",
            metadata={
                "item_count": len(items),
                "backend": "mcp",
                "list_name": self.settings.skylight_list_name or "default grocery list",
            },
        )
        return items

    def add_item(self, item_name: str) -> bool:
        normalized_name = item_name.strip()
        if not normalized_name:
            raise SkylightError("item_name cannot be empty")

        existing_items = self.get_list_items()
        if normalized_name.lower() in existing_items:
            global _duplicate_skip_count
            _duplicate_skip_count += 1
            if _duplicate_skip_count >= 3:
                report_issue(
                    "skylight_duplicate_adds",
                    severity="warning",
                    message="Duplicate add attempts are occurring repeatedly",
                    recommended_action="Review state synchronization between gesture/passive detections and current list items.",
                )
            log_action(
                f"Skipped adding {normalized_name} because it already exists on the Skylight list",
                action="skylight_add_item_skipped_duplicate",
                metadata={"item_name": normalized_name, "backend": "mcp"},
            )
            return False
        _duplicate_skip_count = 0
        resolve_issue("skylight_duplicate_adds")

        arguments: dict[str, Any] = {"label": normalized_name}
        if self.settings.skylight_list_name:
            arguments["listName"] = self.settings.skylight_list_name

        try:
            result = self._process.call_tool("create_list_item", arguments)
            _extract_tool_text(result)
        except SkylightError:
            report_issue(
                "skylight_add_failures",
                severity="warning",
                message="Skylight add item requests are failing",
                recommended_action="Verify the MCP server and review Skylight list tool errors.",
            )
            log_action(
                f"Failed to add {normalized_name} to Skylight via MCP",
                action="skylight_add_item_failed",
                level=40,
                metadata={"item_name": normalized_name, "backend": "mcp"},
            )
            return False

        log_action(
            f"Added {normalized_name} to Skylight via MCP",
            action="skylight_add_item_success",
            metadata={"item_name": normalized_name, "backend": "mcp"},
        )
        update_connection("skylight_api", "healthy")
        resolve_issue("skylight_add_failures")
        mark_mode_timestamp("passive", "last_successful_item_add_timestamp")
        mark_mode_timestamp("gesture", "last_successful_item_add_timestamp")
        record_event("skylight", "Item added to Skylight", {"item_name": normalized_name, "backend": "mcp"})
        return True

    def clear_list(self) -> bool:
        items = self.get_list_items()
        if not items:
            log_action(
                "List already empty",
                action="skylight_clear_list_empty",
                metadata={"item_count": 0, "backend": "mcp"},
            )
            return True

        report_issue(
            "skylight_clear_unsupported_mcp",
            severity="warning",
            message="Skylight MCP backend cannot clear list items with the current MCP tool surface",
            recommended_action="Use the API backend for clear-list operations or clear the list manually in Skylight.",
        )
        log_action(
            "Skylight MCP backend does not expose enough item identifiers to clear the list safely",
            action="skylight_clear_list_unsupported",
            level=30,
            metadata={
                "backend": "mcp",
                "item_count": len(items),
                "list_name": self.settings.skylight_list_name or "default grocery list",
            },
        )
        return False


def get_client(settings: Settings | None = None, *, reset: bool = False) -> BaseSkylightClient:
    global _default_client
    global _default_client_key

    resolved_settings = settings or load_settings()
    key = _settings_cache_key(resolved_settings)
    if reset or _default_client is None or _default_client_key != key:
        if resolved_settings.skylight_backend == "mcp":
            _default_client = McpSkylightClient(resolved_settings)
        else:
            _default_client = ApiSkylightClient(resolved_settings)
        _default_client_key = key
    return _default_client


def authenticate(settings: Settings | None = None, *, force_refresh: bool = False) -> str:
    client = get_client(settings, reset=force_refresh)
    return client.authenticate(force_refresh=force_refresh)


def get_list_items(settings: Settings | None = None) -> list[str]:
    return get_client(settings).get_list_items()


def add_item(item_name: str, settings: Settings | None = None) -> bool:
    return get_client(settings).add_item(item_name)


def clear_list(settings: Settings | None = None) -> bool:
    return get_client(settings).clear_list()


def _settings_cache_key(settings: Settings) -> tuple[Any, ...]:
    return (
        settings.skylight_backend,
        settings.skylight_email,
        settings.skylight_password,
        settings.skylight_frame_id,
        settings.skylight_list_id,
        settings.skylight_list_name,
        settings.skylight_token,
        settings.skylight_auth_type,
        settings.skylight_timezone,
        settings.skylight_mcp_command,
        tuple(settings.skylight_mcp_args),
    )


def _extract_tool_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        raise SkylightError("Skylight MCP tool result did not contain content")

    parts: list[str] = []
    for entry in content:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "text":
            parts.append(str(entry.get("text", "")))
    text = "\n".join(part for part in parts if part).strip()

    if result.get("isError"):
        raise SkylightError(text or "Skylight MCP tool call failed")
    return text


def _parse_list_items_text(text: str) -> list[str]:
    if not text:
        return []

    lines = [line.rstrip() for line in text.splitlines()]
    if not lines:
        return []
    if " is empty" in lines[0].lower():
        return []

    items: list[str] = []
    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.endswith(":") and not stripped.startswith(("[ ]", "[x]")):
            continue

        match = re.match(r"^\[(?: |x)\]\s+(?P<label>.+?)\s*$", stripped, flags=re.IGNORECASE)
        if match:
            items.append(match.group("label").strip().lower())
    return items


def _get_response_error_metadata(exc: requests.RequestException) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    if response is None:
        return {}

    body_preview = response.text.strip()
    if len(body_preview) > 500:
        body_preview = f"{body_preview[:500]}..."

    metadata: dict[str, Any] = {
        "status_code": response.status_code,
        "reason": response.reason,
    }
    if body_preview:
        metadata["response_body"] = body_preview
    return metadata


def _build_error_message(exc: requests.RequestException, metadata: dict[str, Any]) -> str:
    status_code = metadata.get("status_code")
    reason = metadata.get("reason")
    if status_code and reason:
        return f"{status_code} {reason}"
    if status_code:
        return f"HTTP {status_code}"
    return str(exc)
