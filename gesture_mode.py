from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np

from config import Settings, load_settings
from health import mark_mode_timestamp, record_event, report_issue, resolve_issue, set_mode_running
from gesture import (
    ClearGestureProgress,
    HoldProgress,
    create_clear_gesture_tracker,
    create_hand_tracker,
    create_pointing_hold_tracker,
    crop_target_region,
    get_pointing_direction,
    is_clear_gesture,
    is_pointing,
)
from notifier import log_action, log_exception, notify, notify_clear
from skylight import add_item, clear_list, get_list_items
from vision import VisionError, identify_item


WINDOW_NAME = "Grocery AI Gesture Mode"
TARGET_FPS = 30
SUCCESS_OVERLAY_SECONDS = 3.0
ERROR_OVERLAY_SECONDS = 2.0


@dataclass
class StatusOverlay:
    message: str
    color: tuple[int, int, int]
    expires_at: float


class GestureModeRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.hand_tracker = create_hand_tracker(settings)
        self.pointing_hold_tracker = create_pointing_hold_tracker(settings)
        self.clear_gesture_tracker = create_clear_gesture_tracker()
        self._last_detections = []
        self._frame_index = 0
        self._status_overlay: StatusOverlay | None = None
        self._last_gesture_success_at: float | None = None
        self._low_fps_started_at: float | None = None

    def run(self) -> None:
        capture = cv2.VideoCapture(self.settings.camera_source)
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open camera source {self.settings.camera_source!r}")

        log_action(
            "Gesture mode camera stream opened",
            action="gesture_mode_camera_opened",
            metadata={"camera_source": str(self.settings.camera_source)},
        )
        set_mode_running("gesture", True)
        record_event("mode", "Gesture mode started", {"camera_source": str(self.settings.camera_source)})

        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        frame_interval = 1.0 / TARGET_FPS

        try:
            while True:
                loop_started_at = time.monotonic()
                success, frame = capture.read()
                if not success or frame is None:
                    log_action(
                        "Gesture mode camera returned empty frame",
                        action="gesture_mode_empty_frame",
                        level=40,
                        metadata={"frame_index": self._frame_index},
                    )
                    self._set_status_overlay("Camera frame unavailable", (0, 0, 255), ERROR_OVERLAY_SECONDS)
                    continue

                display_frame = frame.copy()
                detections = self._get_detections(frame)
                active_detection = detections[0] if detections else None

                pointing_hold = self._handle_pointing(active_detection, frame, display_frame)
                clear_progress = self._handle_clear_gesture(active_detection)

                self._draw_guidance(display_frame, pointing_hold, clear_progress)
                self._draw_status_overlay(display_frame)

                cv2.imshow(WINDOW_NAME, display_frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q")):
                    log_action("Gesture mode stopped by keyboard input", action="gesture_mode_keyboard_exit")
                    break

                elapsed = time.monotonic() - loop_started_at
                if elapsed < frame_interval:
                    time.sleep(frame_interval - elapsed)
                self._monitor_fps(time.monotonic() - loop_started_at)
        finally:
            capture.release()
            self.hand_tracker.close()
            cv2.destroyAllWindows()
            log_action("Gesture mode stopped", action="gesture_mode_stopped")
            set_mode_running("gesture", False)

    def _get_detections(self, frame: np.ndarray):
        self._frame_index += 1
        if self._frame_index % 2 == 1:
            self._last_detections = self.hand_tracker.process(frame)
        return self._last_detections

    def _handle_pointing(self, detection, frame: np.ndarray, display_frame: np.ndarray) -> HoldProgress:
        now = time.monotonic()
        if detection is None or not is_pointing(detection.hand_landmarks):
            return self.pointing_hold_tracker.update(False, now=now)

        target_xy = get_pointing_direction(detection.hand_landmarks, frame.shape)
        cv2.arrowedLine(
            display_frame,
            self._landmark_to_pixel(detection.hand_landmarks.landmark[0], frame.shape),
            target_xy,
            (0, 255, 255),
            3,
            tipLength=0.2,
        )

        hold_progress = self.pointing_hold_tracker.update(True, now=now)
        if hold_progress.triggered:
            mark_mode_timestamp("gesture", "last_successful_gesture_detection_timestamp")
            record_event("gesture", "Pointing gesture confirmed", {"target_xy": list(target_xy)})
            self._identify_and_add(frame, target_xy)
        return hold_progress

    def _handle_clear_gesture(self, detection) -> ClearGestureProgress:
        now = time.monotonic()
        if detection is None or not is_clear_gesture(detection.hand_landmarks):
            return self.clear_gesture_tracker.update(False, now=now)

        clear_progress = self.clear_gesture_tracker.update(True, now=now)
        if clear_progress.confirmed:
            self._clear_list()
            self.clear_gesture_tracker.reset()
        return clear_progress

    def _identify_and_add(self, frame: np.ndarray, target_xy: tuple[int, int]) -> None:
        try:
            cropped = crop_target_region(frame, target_xy)
            success, encoded = cv2.imencode(".jpg", cropped)
            if not success:
                raise VisionError("Failed to encode cropped target region")

            item_name = identify_item(encoded.tobytes(), settings=self.settings)
            if item_name:
                added = add_item(item_name, settings=self.settings)
                if added:
                    self._last_gesture_success_at = time.monotonic()
                    mark_mode_timestamp("gesture", "last_successful_item_add_timestamp")
                    log_action(
                        f"Gesture mode added {item_name}",
                        action="gesture_mode_item_added",
                        metadata={"item_name": item_name},
                    )
                    record_event("gesture", "Gesture item added", {"item_name": item_name})
                    notify(
                        f"Added {item_name} from gesture",
                        send_desktop=False,
                        action="gesture_mode_item_added_notification",
                        metadata={"item_name": item_name},
                    )
                    self._set_status_overlay(f"Added: {item_name}", (0, 180, 0), SUCCESS_OVERLAY_SECONDS)
                else:
                    self._set_status_overlay(f"Skipped: {item_name} already on list", (0, 165, 255), ERROR_OVERLAY_SECONDS)
            else:
                report_issue(
                    "vision_unknown_threshold",
                    severity="warning",
                    message="Gesture mode received an unknown item result",
                    recommended_action="Reframe the target item and improve lighting or crop accuracy.",
                )
                log_action("Gesture mode could not identify pointed item", action="gesture_mode_item_unknown")
                self._set_status_overlay("Unknown item", (0, 0, 255), ERROR_OVERLAY_SECONDS)
        except Exception as exc:  # noqa: BLE001
            log_exception(
                "Gesture mode failed while identifying or adding an item",
                action="gesture_mode_identify_failed",
                metadata={"error_type": type(exc).__name__},
            )
            self._set_status_overlay("Gesture action failed", (0, 0, 255), ERROR_OVERLAY_SECONDS)
        finally:
            self.pointing_hold_tracker.reset()

    def _clear_list(self) -> None:
        try:
            existing_items = get_list_items(settings=self.settings)
            removed_count = len(existing_items)
            cleared = clear_list(settings=self.settings)
            if cleared:
                notify_clear(removed_count, send_desktop=False)
                record_event("gesture", "List cleared by gesture", {"removed_count": removed_count})
                self._set_status_overlay("List cleared", (0, 0, 255), SUCCESS_OVERLAY_SECONDS)
            else:
                self._set_status_overlay("List clear incomplete", (0, 165, 255), ERROR_OVERLAY_SECONDS)
        except Exception as exc:  # noqa: BLE001
            log_exception(
                "Gesture mode failed to clear the list",
                action="gesture_mode_clear_failed",
                metadata={"error_type": type(exc).__name__},
            )
            self._set_status_overlay("List clear failed", (0, 0, 255), ERROR_OVERLAY_SECONDS)

    def _draw_guidance(
        self,
        frame: np.ndarray,
        pointing_hold: HoldProgress,
        clear_progress: ClearGestureProgress,
    ) -> None:
        if clear_progress.active:
            color = (0, 0, 255) if clear_progress.stage == 1 else (0, 64, 255)
            self._draw_progress_bar(
                frame,
                progress=clear_progress.progress,
                color=color,
                y=frame.shape[0] - 40,
                label=(clear_progress.message if clear_progress.show_warning else "Hold open hand to clear list"),
            )
            return

        if pointing_hold.active:
            self._draw_progress_bar(
                frame,
                progress=pointing_hold.progress,
                color=(0, 255, 255),
                y=frame.shape[0] - 40,
                label=f"Hold pointing gesture: {pointing_hold.remaining_seconds:.1f}s",
            )

    def _draw_progress_bar(
        self,
        frame: np.ndarray,
        *,
        progress: float,
        color: tuple[int, int, int],
        y: int,
        label: str | None,
    ) -> None:
        margin = 24
        bar_width = frame.shape[1] - (margin * 2)
        bar_height = 20
        x = margin
        progress_width = int(bar_width * max(0.0, min(1.0, progress)))

        cv2.rectangle(frame, (x, y), (x + bar_width, y + bar_height), (255, 255, 255), 2)
        cv2.rectangle(frame, (x, y), (x + progress_width, y + bar_height), color, -1)
        if label:
            cv2.putText(frame, label, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)

    def _draw_status_overlay(self, frame: np.ndarray) -> None:
        if self._status_overlay is None:
            return
        if time.monotonic() > self._status_overlay.expires_at:
            self._status_overlay = None
            return

        text = self._status_overlay.message
        color = self._status_overlay.color
        cv2.rectangle(frame, (20, 20), (frame.shape[1] - 20, 90), (20, 20, 20), -1)
        cv2.rectangle(frame, (20, 20), (frame.shape[1] - 20, 90), color, 2)
        cv2.putText(frame, text, (40, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2, cv2.LINE_AA)

    def _set_status_overlay(self, message: str, color: tuple[int, int, int], duration_seconds: float) -> None:
        self._status_overlay = StatusOverlay(
            message=message,
            color=color,
            expires_at=time.monotonic() + duration_seconds,
        )

    def _monitor_fps(self, loop_elapsed_seconds: float) -> None:
        fps = 0.0 if loop_elapsed_seconds <= 0 else 1.0 / loop_elapsed_seconds
        now = time.monotonic()
        if fps < 10.0:
            if self._low_fps_started_at is None:
                self._low_fps_started_at = now
            elif now - self._low_fps_started_at >= 30.0:
                report_issue(
                    "gesture_low_fps",
                    severity="warning",
                    message="Gesture loop FPS dropped below 10 for more than 30 seconds",
                    recommended_action="Reduce workload, camera resolution, or model frequency in the gesture loop.",
                )
        else:
            self._low_fps_started_at = None
            resolve_issue("gesture_low_fps")

    @staticmethod
    def _landmark_to_pixel(landmark, frame_shape: tuple[int, ...]) -> tuple[int, int]:
        frame_height, frame_width = frame_shape[:2]
        return int(landmark.x * frame_width), int(landmark.y * frame_height)


def run(settings: Settings | None = None) -> None:
    runner = GestureModeRunner(settings or load_settings())
    runner.run()


def run_gesture_mode(settings: Settings | None = None) -> None:
    run(settings=settings)