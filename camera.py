from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from health import record_event, report_issue, resolve_issue, update_connection

if TYPE_CHECKING:
    from config import Settings


logger = logging.getLogger(__name__)


class CameraError(RuntimeError):
    pass


def capture_frame(
    *,
    settings: Settings | None = None,
    source: int | str | None = None,
    retry_attempts: int | None = None,
    retry_delay_seconds: int | float | None = None,
) -> np.ndarray:
    camera_source, attempts, delay_seconds = _resolve_options(
        settings=settings,
        source=source,
        retry_attempts=retry_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            logger.info(
                "Capturing frame",
                extra={"action": "capture_frame", "attempt": attempt, "source": str(camera_source)},
            )
            frame = _capture_once(camera_source)
            update_connection("camera", "healthy")
            resolve_issue("camera_empty_frames")
            resolve_issue("camera_reconnect_failures")
            record_event("camera", "Camera frame captured", {"source": str(camera_source), "attempt": attempt})
            return frame
        except CameraError as exc:
            last_error = exc
            update_connection("camera", "degraded", error_message=str(exc))
            logger.warning(
                "Camera capture attempt failed: %s",
                exc,
                extra={"action": "camera_capture_failed", "attempt": attempt, "source": str(camera_source)},
            )
            if attempt < attempts:
                time.sleep(delay_seconds)

    report_issue(
        "camera_reconnect_failures",
        severity="critical",
        message="Camera reconnect loop exceeded configured retries",
        recommended_action="Verify the camera source path, device permissions, and network reachability.",
    )

    raise CameraError(
        f"Unable to capture frame from source {camera_source!r} after {attempts} attempts"
    ) from last_error


def capture_frame_to_file(
    path: str | Path,
    *,
    settings: Settings | None = None,
    source: int | str | None = None,
    retry_attempts: int | None = None,
    retry_delay_seconds: int | float | None = None,
) -> Path:
    frame = capture_frame(
        settings=settings,
        source=source,
        retry_attempts=retry_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    wrote_file = cv2.imwrite(str(destination), frame)
    if not wrote_file:
        raise CameraError(f"Failed to write frame to {destination}")

    logger.info(
        "Saved captured frame to file",
        extra={"action": "camera_frame_saved", "path": str(destination)},
    )
    return destination


def capture_frame_to_memory(
    *,
    settings: Settings | None = None,
    source: int | str | None = None,
    retry_attempts: int | None = None,
    retry_delay_seconds: int | float | None = None,
    image_format: str = ".jpg",
) -> bytes:
    frame = capture_frame(
        settings=settings,
        source=source,
        retry_attempts=retry_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )
    success, encoded = cv2.imencode(image_format, frame)
    if not success:
        raise CameraError(f"Failed to encode frame using format {image_format}")

    logger.info(
        "Encoded frame to memory",
        extra={"action": "camera_frame_encoded", "format": image_format},
    )
    return encoded.tobytes()


def _capture_once(source: int | str) -> np.ndarray:
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        capture.release()
        raise CameraError(f"Failed to open camera source {source!r}")

    try:
        success, frame = capture.read()
        if not success or frame is None:
            report_issue(
                "camera_empty_frames",
                severity="warning",
                message="Camera returned empty frames",
                recommended_action="Check camera stream stability and whether the source is producing valid frames.",
            )
            raise CameraError(f"Camera source {source!r} returned an empty frame")
        return frame
    finally:
        capture.release()


def _resolve_options(
    *,
    settings: Settings | None,
    source: int | str | None,
    retry_attempts: int | None,
    retry_delay_seconds: int | float | None,
) -> tuple[int | str, int, float]:
    camera_source = source if source is not None else _get_setting_value(settings, "camera_source", 0)
    attempts = int(retry_attempts if retry_attempts is not None else _get_setting_value(settings, "camera_retry_attempts", 3))
    delay_seconds = float(
        retry_delay_seconds
        if retry_delay_seconds is not None
        else _get_setting_value(settings, "camera_retry_delay_seconds", 5)
    )

    if attempts < 1:
        raise CameraError("retry_attempts must be at least 1")
    if delay_seconds < 0:
        raise CameraError("retry_delay_seconds cannot be negative")

    return camera_source, attempts, delay_seconds


def _get_setting_value(settings: Settings | None, attribute: str, default: Any) -> Any:
    if settings is None:
        return default
    return getattr(settings, attribute, default)