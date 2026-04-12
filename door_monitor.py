from __future__ import annotations

from dataclasses import dataclass
import time

import cv2
import numpy as np

from config import Settings
from health import record_event, report_issue, resolve_issue, update_connection
from notifier import log_action, log_exception


@dataclass(frozen=True)
class DoorOpenEvent:
    timestamp: float
    changed_ratio: float
    motion_duration_seconds: float


class DoorEventDetector:
    def __init__(
        self,
        *,
        motion_ratio_threshold: float,
        intensity_threshold: int,
        min_motion_seconds: float,
        settle_seconds: float,
        cooldown_seconds: float,
        resize_width: int = 320,
    ) -> None:
        self.motion_ratio_threshold = motion_ratio_threshold
        self.intensity_threshold = intensity_threshold
        self.min_motion_seconds = min_motion_seconds
        self.settle_seconds = settle_seconds
        self.cooldown_seconds = cooldown_seconds
        self.resize_width = resize_width

        self._previous_gray: np.ndarray | None = None
        self._motion_started_at: float | None = None
        self._last_motion_at: float | None = None
        self._last_event_at: float | None = None
        self._pending_settle = False

    def process_frame(self, frame: np.ndarray, now: float | None = None) -> DoorOpenEvent | None:
        current_time = now if now is not None else time.monotonic()
        gray_frame = self._prepare_frame(frame)

        if self._previous_gray is None:
            self._previous_gray = gray_frame
            return None

        delta = cv2.absdiff(self._previous_gray, gray_frame)
        _, threshold_frame = cv2.threshold(delta, self.intensity_threshold, 255, cv2.THRESH_BINARY)
        changed_ratio = float(np.count_nonzero(threshold_frame)) / float(threshold_frame.size)
        self._previous_gray = gray_frame

        if changed_ratio >= self.motion_ratio_threshold:
            if self._motion_started_at is None:
                self._motion_started_at = current_time
            self._last_motion_at = current_time
            self._pending_settle = True
            return None

        if not self._pending_settle or self._last_motion_at is None:
            return None

        if current_time - self._last_motion_at < self.settle_seconds:
            return None

        motion_duration_seconds = 0.0
        if self._motion_started_at is not None:
            motion_duration_seconds = self._last_motion_at - self._motion_started_at

        cooldown_ok = self._last_event_at is None or (current_time - self._last_event_at) >= self.cooldown_seconds
        event: DoorOpenEvent | None = None
        if motion_duration_seconds >= self.min_motion_seconds and cooldown_ok:
            event = DoorOpenEvent(
                timestamp=current_time,
                changed_ratio=changed_ratio,
                motion_duration_seconds=motion_duration_seconds,
            )
            self._last_event_at = current_time

        self._motion_started_at = None
        self._last_motion_at = None
        self._pending_settle = False
        return event

    def _prepare_frame(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        if width > self.resize_width:
            resized_height = max(1, int(height * (self.resize_width / width)))
            frame = cv2.resize(frame, (self.resize_width, resized_height))
        gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray_frame, (9, 9), 0)


def open_camera_stream(settings: Settings) -> cv2.VideoCapture:
    capture = cv2.VideoCapture(settings.camera_source)
    if not capture.isOpened():
        update_connection("camera", "disconnected", error_message="Door monitor could not open camera stream")
        report_issue(
            "door_monitor_camera_unavailable",
            severity="critical",
            message="Door monitor could not open the camera stream",
            recommended_action="Verify the camera source and ensure the device is available for continuous monitoring.",
        )
        raise RuntimeError(f"Unable to open camera source {settings.camera_source!r} for door monitoring")

    update_connection("camera", "healthy")
    resolve_issue("door_monitor_camera_unavailable")
    log_action(
        "Door monitor camera stream opened",
        action="door_monitor_camera_opened",
        metadata={"camera_source": str(settings.camera_source)},
    )
    return capture


def monitor_loop(*, settings: Settings, stop_event, on_door_open) -> None:
    detector = DoorEventDetector(
        motion_ratio_threshold=settings.door_open_motion_ratio_threshold,
        intensity_threshold=settings.door_open_intensity_threshold,
        min_motion_seconds=settings.door_open_min_motion_seconds,
        settle_seconds=settings.door_open_settle_seconds,
        cooldown_seconds=float(settings.door_open_cooldown_seconds),
    )
    capture = open_camera_stream(settings)
    frame_sleep_seconds = 1.0 / float(settings.door_open_sample_fps)

    try:
        while not stop_event.is_set():
            success, frame = capture.read()
            if not success or frame is None:
                update_connection("camera", "degraded", error_message="Door monitor received an empty frame")
                report_issue(
                    "door_monitor_empty_frames",
                    severity="warning",
                    message="Door monitor is receiving empty frames",
                    recommended_action="Check camera stability and whether the stream is dropping frames during continuous monitoring.",
                )
                time.sleep(frame_sleep_seconds)
                continue

            resolve_issue("door_monitor_empty_frames")
            event = detector.process_frame(frame)
            if event is not None:
                record_event(
                    "door-open",
                    "Door-open motion event detected",
                    {
                        "changed_ratio": round(event.changed_ratio, 4),
                        "motion_duration_seconds": round(event.motion_duration_seconds, 3),
                    },
                )
                log_action(
                    "Door-open motion event detected",
                    action="door_open_detected",
                    metadata={
                        "changed_ratio": round(event.changed_ratio, 4),
                        "motion_duration_seconds": round(event.motion_duration_seconds, 3),
                    },
                )
                on_door_open(frame.copy(), event)

            time.sleep(frame_sleep_seconds)
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "Door monitor loop failed",
            action="door_monitor_failed",
            metadata={"error_type": type(exc).__name__},
        )
        report_issue(
            "door_monitor_failed",
            severity="critical",
            message="Door monitor loop stopped unexpectedly",
            recommended_action="Review camera logs and restart passive mode after correcting the camera or stream issue.",
        )
        raise
    finally:
        capture.release()
        log_action("Door monitor camera stream closed", action="door_monitor_camera_closed")