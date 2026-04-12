from __future__ import annotations

import base64
import json
import time
from typing import Any

import requests

from config import Settings, load_settings
from health import record_event, report_issue, resolve_issue, update_connection
from notifier import log_action, log_exception


_default_client: "VisionClient | None" = None
_unknown_identification_count = 0


class VisionError(RuntimeError):
    pass


class VisionClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    def identify_item(self, image_bytes: bytes) -> str | None:
        if not image_bytes:
            raise VisionError("image_bytes cannot be empty")

        payload = {
            "model": self.settings.ollama_model,
            "prompt": (
                "What grocery or household item is shown in this image? "
                "Reply with only the item name, nothing else. "
                "If you cannot identify a grocery or household item, reply with 'unknown'."
            ),
            "images": [self._encode_image(image_bytes)],
            "stream": False,
        }

        response_text, elapsed_seconds = self._generate(payload, action="vision_identify_item")
        normalized = response_text.strip().lower()

        log_action(
            "Vision item identification completed",
            action="vision_identify_item_complete",
            metadata={
                "response_time_seconds": round(elapsed_seconds, 3),
                "identified_item": None if normalized in {"", "unknown"} else normalized,
            },
        )

        if normalized in {"", "unknown"}:
            global _unknown_identification_count
            _unknown_identification_count += 1
            if _unknown_identification_count >= self.settings.unknown_item_threshold:
                report_issue(
                    "vision_unknown_threshold",
                    severity="warning",
                    message="Vision model is repeatedly returning unknown items",
                    recommended_action="Review camera framing and prompt quality, and confirm the target region contains the intended item.",
                )
            return None
        _unknown_identification_count = 0
        resolve_issue("vision_unknown_threshold")
        return normalized

    def check_staples(self, image_bytes: bytes, staples_list: list[str]) -> list[str]:
        if not image_bytes:
            raise VisionError("image_bytes cannot be empty")
        if not staples_list:
            raise VisionError("staples_list cannot be empty")

        normalized_staples = [item.strip().lower() for item in staples_list if item.strip()]
        if not normalized_staples:
            raise VisionError("staples_list must contain at least one non-empty item")

        prompt = (
            "You are checking a fridge/pantry. Here is a list of items that should "
            f"always be stocked: {', '.join(normalized_staples)}. Looking at this image, which items from "
            "the list appear to be missing or running very low? "
            'Reply ONLY with a JSON array of item names. Example: ["milk", "eggs"]. '
            "If everything looks stocked, reply with an empty array: []"
        )

        payload = {
            "model": self.settings.ollama_model,
            "prompt": prompt,
            "images": [self._encode_image(image_bytes)],
            "stream": False,
        }

        response_text, elapsed_seconds = self._generate(payload, action="vision_check_staples")

        try:
            parsed_items = self._parse_json_array(response_text)
        except VisionError:
            report_issue(
                "vision_invalid_json",
                severity="warning",
                message="Vision staple check returned invalid JSON",
                recommended_action="Inspect the Ollama response in logs and tighten the prompt or model configuration.",
            )
            log_action(
                "Vision staple check returned invalid JSON",
                action="vision_check_staples_parse_failed",
                level=40,
                metadata={
                    "response_time_seconds": round(elapsed_seconds, 3),
                    "raw_response": response_text[:500],
                },
            )
            return []

        resolve_issue("vision_invalid_json")

        filtered_items = []
        for item in parsed_items:
            normalized_item = item.strip().lower()
            if normalized_item and normalized_item in normalized_staples and normalized_item not in filtered_items:
                filtered_items.append(normalized_item)

        log_action(
            "Vision staple check completed",
            action="vision_check_staples_complete",
            metadata={
                "response_time_seconds": round(elapsed_seconds, 3),
                "flagged_count": len(filtered_items),
                "flagged_items": filtered_items,
            },
        )
        return filtered_items

    def _generate(self, payload: dict[str, Any], *, action: str) -> tuple[str, float]:
        endpoint = f"{self.settings.ollama_host}/api/generate"
        started_at = time.perf_counter()

        log_action(
            "Sending request to Ollama",
            action=f"{action}_start",
            metadata={"endpoint": endpoint, "model": self.settings.ollama_model},
        )

        try:
            response = self.session.post(
                endpoint,
                json=payload,
                timeout=self.settings.ollama_timeout_seconds,
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            elapsed_seconds = time.perf_counter() - started_at
            update_connection("ollama", "degraded", error_message="Request timed out")
            report_issue(
                "ollama_timeout",
                severity="critical",
                message="Ollama request timed out",
                recommended_action="Check local Ollama responsiveness and reduce image/model load if needed.",
            )
            log_exception(
                "Ollama request timed out",
                action=f"{action}_timeout",
                metadata={
                    "endpoint": endpoint,
                    "response_time_seconds": round(elapsed_seconds, 3),
                },
            )
            raise VisionError("Ollama request timed out") from exc
        except requests.RequestException as exc:
            elapsed_seconds = time.perf_counter() - started_at
            update_connection("ollama", "disconnected", error_message=str(exc))
            log_exception(
                "Ollama request failed",
                action=f"{action}_request_failed",
                metadata={
                    "endpoint": endpoint,
                    "response_time_seconds": round(elapsed_seconds, 3),
                    "error_type": type(exc).__name__,
                },
            )
            raise VisionError("Ollama request failed") from exc

        elapsed_seconds = time.perf_counter() - started_at

        try:
            body = response.json()
        except ValueError as exc:
            update_connection("ollama", "degraded", error_message="Invalid JSON response")
            log_exception(
                "Ollama returned non-JSON response",
                action=f"{action}_invalid_response",
                metadata={
                    "endpoint": endpoint,
                    "response_time_seconds": round(elapsed_seconds, 3),
                },
            )
            raise VisionError("Ollama returned non-JSON response") from exc

        response_text = str(body.get("response", "")).strip()
        update_connection("ollama", "healthy")
        update_connection("llava", "healthy")
        resolve_issue("ollama_timeout")
        record_event("vision", "Ollama request completed", {"action": action, "response_time_seconds": round(elapsed_seconds, 3)})
        log_action(
            "Received response from Ollama",
            action=f"{action}_success",
            metadata={
                "endpoint": endpoint,
                "response_time_seconds": round(elapsed_seconds, 3),
                "done": body.get("done"),
            },
        )
        return response_text, elapsed_seconds

    @staticmethod
    def _encode_image(image_bytes: bytes) -> str:
        return base64.b64encode(image_bytes).decode("ascii")

    @staticmethod
    def _parse_json_array(response_text: str) -> list[str]:
        candidate = response_text.strip()
        if not candidate:
            return []

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            start = candidate.find("[")
            end = candidate.rfind("]")
            if start == -1 or end == -1 or end < start:
                raise VisionError("Vision response did not contain a JSON array")
            try:
                parsed = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError as exc:
                raise VisionError("Vision response did not contain valid JSON") from exc

        if not isinstance(parsed, list):
            raise VisionError("Vision response JSON must be an array")

        normalized_items: list[str] = []
        for item in parsed:
            if isinstance(item, str):
                normalized_items.append(item)
        return normalized_items


def get_client(settings: Settings | None = None, *, reset: bool = False) -> VisionClient:
    global _default_client

    if reset or _default_client is None:
        resolved_settings = settings or load_settings()
        _default_client = VisionClient(resolved_settings)
    return _default_client


def identify_item(image_bytes: bytes, settings: Settings | None = None) -> str | None:
    return get_client(settings).identify_item(image_bytes)


def check_staples(
    image_bytes: bytes,
    staples_list: list[str],
    settings: Settings | None = None,
) -> list[str]:
    return get_client(settings).check_staples(image_bytes, staples_list)