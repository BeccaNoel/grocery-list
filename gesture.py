from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import cv2
import mediapipe as mp
import numpy as np

from config import Settings, load_settings


HANDS_MODULE = mp.solutions.hands

WRIST = HANDS_MODULE.HandLandmark.WRIST
THUMB_IP = HANDS_MODULE.HandLandmark.THUMB_IP
THUMB_TIP = HANDS_MODULE.HandLandmark.THUMB_TIP
INDEX_FINGER_MCP = HANDS_MODULE.HandLandmark.INDEX_FINGER_MCP
INDEX_FINGER_PIP = HANDS_MODULE.HandLandmark.INDEX_FINGER_PIP
INDEX_FINGER_TIP = HANDS_MODULE.HandLandmark.INDEX_FINGER_TIP
MIDDLE_FINGER_PIP = HANDS_MODULE.HandLandmark.MIDDLE_FINGER_PIP
MIDDLE_FINGER_TIP = HANDS_MODULE.HandLandmark.MIDDLE_FINGER_TIP
MIDDLE_FINGER_MCP = HANDS_MODULE.HandLandmark.MIDDLE_FINGER_MCP
RING_FINGER_PIP = HANDS_MODULE.HandLandmark.RING_FINGER_PIP
RING_FINGER_TIP = HANDS_MODULE.HandLandmark.RING_FINGER_TIP
PINKY_PIP = HANDS_MODULE.HandLandmark.PINKY_PIP
PINKY_TIP = HANDS_MODULE.HandLandmark.PINKY_TIP
PINKY_MCP = HANDS_MODULE.HandLandmark.PINKY_MCP

DEFAULT_CLEAR_CONFIRMATION_MESSAGE = (
    "Clear entire list? Hold gesture 3 more seconds to confirm. Drop hand to cancel."
)


@dataclass(frozen=True)
class HandDetection:
    hand_landmarks: Any
    handedness_label: str | None = None
    handedness_score: float | None = None


@dataclass(frozen=True)
class HoldProgress:
    active: bool
    elapsed_seconds: float
    remaining_seconds: float
    progress: float
    triggered: bool


@dataclass(frozen=True)
class ClearGestureProgress:
    active: bool
    stage: int
    elapsed_seconds: float
    remaining_seconds: float
    progress: float
    show_warning: bool
    confirmed: bool
    message: str | None = None


class HandTracker:
    def __init__(
        self,
        *,
        max_num_hands: int = 1,
        min_detection_confidence: float = 0.8,
        min_tracking_confidence: float = 0.5,
    ) -> None:
        self._hands = HANDS_MODULE.Hands(
            static_image_mode=False,
            max_num_hands=max_num_hands,
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
        )

    def process(self, frame: np.ndarray) -> list[HandDetection]:
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self._hands.process(rgb_frame)

        multi_hand_landmarks = results.multi_hand_landmarks or []
        multi_handedness = results.multi_handedness or []
        detections: list[HandDetection] = []

        for index, hand_landmarks in enumerate(multi_hand_landmarks):
            handedness_label = None
            handedness_score = None
            if index < len(multi_handedness):
                classification = multi_handedness[index].classification
                if classification:
                    handedness_label = classification[0].label
                    handedness_score = classification[0].score

            detections.append(
                HandDetection(
                    hand_landmarks=hand_landmarks,
                    handedness_label=handedness_label,
                    handedness_score=handedness_score,
                )
            )

        return detections

    def close(self) -> None:
        self._hands.close()


class GestureHoldTracker:
    def __init__(self, required_seconds: float) -> None:
        self.required_seconds = required_seconds
        self._started_at: float | None = None
        self._has_triggered = False

    def update(self, is_active: bool, now: float | None = None) -> HoldProgress:
        current_time = now if now is not None else time.monotonic()
        if not is_active:
            self.reset()
            return HoldProgress(False, 0.0, self.required_seconds, 0.0, False)

        if self._started_at is None:
            self._started_at = current_time
            self._has_triggered = False

        elapsed_seconds = max(0.0, current_time - self._started_at)
        progress = min(1.0, elapsed_seconds / self.required_seconds)
        remaining_seconds = max(0.0, self.required_seconds - elapsed_seconds)
        triggered = elapsed_seconds >= self.required_seconds and not self._has_triggered

        if triggered:
            self._has_triggered = True

        return HoldProgress(True, elapsed_seconds, remaining_seconds, progress, triggered)

    def reset(self) -> None:
        self._started_at = None
        self._has_triggered = False


class ClearGestureTracker:
    def __init__(self, stage_duration_seconds: float = 3.0) -> None:
        self.stage_duration_seconds = stage_duration_seconds
        self._stage = 0
        self._stage_started_at: float | None = None

    def update(self, is_active: bool, now: float | None = None) -> ClearGestureProgress:
        current_time = now if now is not None else time.monotonic()
        if not is_active:
            self.reset()
            return ClearGestureProgress(False, 0, 0.0, self.stage_duration_seconds, 0.0, False, False)

        if self._stage == 0 or self._stage_started_at is None:
            self._stage = 1
            self._stage_started_at = current_time

        elapsed_seconds = max(0.0, current_time - self._stage_started_at)

        if self._stage == 1 and elapsed_seconds >= self.stage_duration_seconds:
            self._stage = 2
            self._stage_started_at = current_time
            return ClearGestureProgress(
                True,
                2,
                0.0,
                self.stage_duration_seconds,
                0.0,
                True,
                False,
                DEFAULT_CLEAR_CONFIRMATION_MESSAGE,
            )

        if self._stage == 2:
            progress = min(1.0, elapsed_seconds / self.stage_duration_seconds)
            remaining_seconds = max(0.0, self.stage_duration_seconds - elapsed_seconds)
            confirmed = elapsed_seconds >= self.stage_duration_seconds
            return ClearGestureProgress(
                True,
                2,
                elapsed_seconds,
                remaining_seconds,
                progress,
                True,
                confirmed,
                DEFAULT_CLEAR_CONFIRMATION_MESSAGE,
            )

        progress = min(1.0, elapsed_seconds / self.stage_duration_seconds)
        remaining_seconds = max(0.0, self.stage_duration_seconds - elapsed_seconds)
        return ClearGestureProgress(True, 1, elapsed_seconds, remaining_seconds, progress, False, False)

    def reset(self) -> None:
        self._stage = 0
        self._stage_started_at = None


def create_hand_tracker(settings: Settings | None = None) -> HandTracker:
    _ = settings or load_settings()
    return HandTracker(max_num_hands=1, min_detection_confidence=0.8)


def create_pointing_hold_tracker(settings: Settings | None = None) -> GestureHoldTracker:
    resolved_settings = settings or load_settings()
    return GestureHoldTracker(required_seconds=float(resolved_settings.pointing_hold_seconds))


def create_clear_gesture_tracker() -> ClearGestureTracker:
    return ClearGestureTracker(stage_duration_seconds=3.0)


def is_pointing(hand_landmarks: Any) -> bool:
    index_tip = _landmark(hand_landmarks, INDEX_FINGER_TIP)
    index_pip = _landmark(hand_landmarks, INDEX_FINGER_PIP)
    middle_tip = _landmark(hand_landmarks, MIDDLE_FINGER_TIP)
    middle_pip = _landmark(hand_landmarks, MIDDLE_FINGER_PIP)
    ring_tip = _landmark(hand_landmarks, RING_FINGER_TIP)
    ring_pip = _landmark(hand_landmarks, RING_FINGER_PIP)
    pinky_tip = _landmark(hand_landmarks, PINKY_TIP)
    pinky_pip = _landmark(hand_landmarks, PINKY_PIP)

    index_extended = index_tip.y < index_pip.y
    middle_curled = middle_tip.y > middle_pip.y
    ring_curled = ring_tip.y > ring_pip.y
    pinky_curled = pinky_tip.y > pinky_pip.y
    return index_extended and middle_curled and ring_curled and pinky_curled


def get_pointing_direction(hand_landmarks: Any, frame_shape: tuple[int, ...]) -> tuple[int, int]:
    frame_height, frame_width = frame_shape[:2]
    wrist = _landmark(hand_landmarks, WRIST)
    index_tip = _landmark(hand_landmarks, INDEX_FINGER_TIP)

    wrist_x = wrist.x * frame_width
    wrist_y = wrist.y * frame_height
    tip_x = index_tip.x * frame_width
    tip_y = index_tip.y * frame_height

    vector_x = tip_x - wrist_x
    vector_y = tip_y - wrist_y

    end_x = wrist_x + (vector_x * 2.5)
    end_y = wrist_y + (vector_y * 2.5)

    clamped_x = int(np.clip(end_x, 0, frame_width - 1))
    clamped_y = int(np.clip(end_y, 0, frame_height - 1))
    return clamped_x, clamped_y


def crop_target_region(frame: np.ndarray, target_xy: tuple[int, int], size: int = 150) -> np.ndarray:
    if size <= 0:
        raise ValueError("size must be positive")

    frame_height, frame_width = frame.shape[:2]
    crop_size = min(size, frame_width, frame_height)
    half_size = crop_size // 2
    target_x, target_y = target_xy

    left = max(0, min(target_x - half_size, frame_width - crop_size))
    top = max(0, min(target_y - half_size, frame_height - crop_size))
    right = left + crop_size
    bottom = top + crop_size
    return frame[top:bottom, left:right].copy()


def is_clear_gesture(hand_landmarks: Any) -> bool:
    thumb_tip = _landmark(hand_landmarks, THUMB_TIP)
    thumb_ip = _landmark(hand_landmarks, THUMB_IP)
    index_tip = _landmark(hand_landmarks, INDEX_FINGER_TIP)
    index_pip = _landmark(hand_landmarks, INDEX_FINGER_PIP)
    middle_tip = _landmark(hand_landmarks, MIDDLE_FINGER_TIP)
    middle_pip = _landmark(hand_landmarks, MIDDLE_FINGER_PIP)
    ring_tip = _landmark(hand_landmarks, RING_FINGER_TIP)
    ring_pip = _landmark(hand_landmarks, RING_FINGER_PIP)
    pinky_tip = _landmark(hand_landmarks, PINKY_TIP)
    pinky_pip = _landmark(hand_landmarks, PINKY_PIP)

    all_extended = (
        thumb_tip.y < thumb_ip.y
        and index_tip.y < index_pip.y
        and middle_tip.y < middle_pip.y
        and ring_tip.y < ring_pip.y
        and pinky_tip.y < pinky_pip.y
    )
    if not all_extended:
        return False

    spread_is_open = _has_open_finger_spread(hand_landmarks)
    palm_is_forward = _is_palm_facing_camera(hand_landmarks)
    return spread_is_open and palm_is_forward


def _has_open_finger_spread(hand_landmarks: Any) -> bool:
    thumb_tip = _landmark(hand_landmarks, THUMB_TIP)
    index_tip = _landmark(hand_landmarks, INDEX_FINGER_TIP)
    middle_tip = _landmark(hand_landmarks, MIDDLE_FINGER_TIP)
    ring_tip = _landmark(hand_landmarks, RING_FINGER_TIP)
    pinky_tip = _landmark(hand_landmarks, PINKY_TIP)
    index_mcp = _landmark(hand_landmarks, INDEX_FINGER_MCP)
    pinky_mcp = _landmark(hand_landmarks, PINKY_MCP)

    palm_width = max(0.001, abs(index_mcp.x - pinky_mcp.x))
    thumb_gap = abs(thumb_tip.x - index_tip.x) / palm_width
    index_middle_gap = abs(index_tip.x - middle_tip.x) / palm_width
    middle_ring_gap = abs(middle_tip.x - ring_tip.x) / palm_width
    ring_pinky_gap = abs(ring_tip.x - pinky_tip.x) / palm_width

    return (
        thumb_gap >= 0.4
        and index_middle_gap >= 0.18
        and middle_ring_gap >= 0.12
        and ring_pinky_gap >= 0.18
    )


def _is_palm_facing_camera(hand_landmarks: Any) -> bool:
    wrist = _landmark(hand_landmarks, WRIST)
    index_mcp = _landmark(hand_landmarks, INDEX_FINGER_MCP)
    middle_mcp = _landmark(hand_landmarks, MIDDLE_FINGER_MCP)
    pinky_mcp = _landmark(hand_landmarks, PINKY_MCP)
    index_tip = _landmark(hand_landmarks, INDEX_FINGER_TIP)
    middle_tip = _landmark(hand_landmarks, MIDDLE_FINGER_TIP)
    ring_tip = _landmark(hand_landmarks, RING_FINGER_TIP)
    pinky_tip = _landmark(hand_landmarks, PINKY_TIP)

    palm_width = abs(index_mcp.x - pinky_mcp.x)
    palm_height = abs(wrist.y - middle_mcp.y)
    fingertips_closer = sum(
        1
        for tip in (index_tip, middle_tip, ring_tip, pinky_tip)
        if tip.z < wrist.z
    )
    return palm_width > 0.05 and palm_height > 0.05 and fingertips_closer >= 2


def _landmark(hand_landmarks: Any, landmark_id: HANDS_MODULE.HandLandmark) -> Any:
    return hand_landmarks.landmark[int(landmark_id)]