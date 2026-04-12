from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import requests

from config import Settings, load_settings
from health import mark_mode_timestamp, record_event, report_issue, resolve_issue, update_connection
from notifier import log_action, log_exception


BASE_URL = "https://app.ourskylight.com"

_default_client: "SkylightClient | None" = None
_duplicate_skip_count = 0


class SkylightError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkylightItem:
    item_id: str
    name: str


class SkylightClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
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
            "Authenticating with Skylight",
            action="skylight_authenticate_start",
            metadata={"base_url": BASE_URL},
        )

        try:
            response = self.session.post(
                f"{BASE_URL}/api/sessions",
                json=payload,
                timeout=self.settings.skylight_timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            update_connection("skylight_auth", "disconnected", error_message=str(exc))
            report_issue(
                "skylight_auth_failures",
                severity="critical",
                message="Skylight authentication failed",
                recommended_action="Verify Skylight credentials and external connectivity.",
            )
            log_exception(
                "Skylight authentication failed",
                action="skylight_authenticate_failed",
                metadata={"error_type": type(exc).__name__},
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
            metadata={"user_id": user_id},
        )
        update_connection("skylight_auth", "healthy")
        resolve_issue("skylight_auth_failures")
        record_event("skylight", "Skylight authentication succeeded", {"user_id": user_id})
        return encoded_token

    def get_list_items(self) -> list[str]:
        items = self._get_list_item_records()
        normalized = [item.name.strip().lower() for item in items if item.name.strip()]
        log_action(
            "Fetched Skylight list items",
            action="skylight_get_list_items_success",
            metadata={"item_count": len(normalized)},
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
                metadata={"item_name": normalized_name},
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
                metadata={"item_name": normalized_name},
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
                metadata={"item_name": normalized_name, "status_code": response.status_code},
            )
            return False

        log_action(
            f"Added {normalized_name} to Skylight",
            action="skylight_add_item_success",
            metadata={"item_name": normalized_name, "status_code": response.status_code},
        )
        update_connection("skylight_api", "healthy")
        resolve_issue("skylight_add_failures")
        mark_mode_timestamp("passive", "last_successful_item_add_timestamp")
        mark_mode_timestamp("gesture", "last_successful_item_add_timestamp")
        record_event("skylight", "Item added to Skylight", {"item_name": normalized_name})
        return True

    def clear_list(self) -> bool:
        items = self._get_list_item_records()
        if not items:
            log_action(
                "List already empty",
                action="skylight_clear_list_empty",
                metadata={"item_count": 0},
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
                    metadata={"item_id": item.item_id, "item_name": item.name},
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
            },
        )
        record_event("skylight", "Skylight list clear completed", {"deleted_count": deleted_count, "success": all_succeeded})
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
                metadata={"method": method, "url": url, "error_type": type(exc).__name__},
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
                metadata={"method": method, "url": url},
            )
            self.authenticate(force_refresh=True)
            try:
                response = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                update_connection("skylight_api", "degraded", error_message=str(exc))
                log_exception(
                    "Skylight retry request failed",
                    action="skylight_retry_request_exception",
                    metadata={"method": method, "url": url, "error_type": type(exc).__name__},
                )
                raise SkylightError(f"Skylight retry request failed for {method} {url}") from exc

            update_connection("skylight_api", "healthy")
        return response

    @property
    def _list_endpoint(self) -> str:
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


def get_client(settings: Settings | None = None, *, reset: bool = False) -> SkylightClient:
    global _default_client

    if reset or _default_client is None:
        resolved_settings = settings or load_settings()
        _default_client = SkylightClient(resolved_settings)
    return _default_client


def authenticate(settings: Settings | None = None, *, force_refresh: bool = False) -> str:
    return get_client(settings).authenticate(force_refresh=force_refresh)


def get_list_items(settings: Settings | None = None) -> list[str]:
    return get_client(settings).get_list_items()


def add_item(item_name: str, settings: Settings | None = None) -> bool:
    return get_client(settings).add_item(item_name)


def clear_list(settings: Settings | None = None) -> bool:
    return get_client(settings).clear_list()