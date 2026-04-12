from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import cv2
import schedule

from camera import CameraError, capture_frame_to_memory
from config import Settings, load_settings
from door_monitor import DoorOpenEvent, monitor_loop
from health import mark_mode_timestamp, record_event, report_issue, resolve_issue, set_mode_running
from notifier import log_action, log_exception, notify
from skylight import add_item
from vision import VisionError, check_staples


@dataclass
class PassiveModeRunner:
    settings: Settings
    stop_event: threading.Event | None = None
    flag_counts: dict[str, int] = field(default_factory=dict)
    last_scan_started_at: float | None = None
    scan_lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self) -> None:
        scheduler = schedule.Scheduler()
        scheduler.every(self.settings.scan_interval_hours).hours.do(self.run_scan)
        set_mode_running("passive", True)

        if self.stop_event is None:
            self.stop_event = threading.Event()

        door_monitor_thread: threading.Thread | None = None
        if self.settings.door_open_detection_enabled:
            door_monitor_thread = threading.Thread(
                target=monitor_loop,
                kwargs={
                    "settings": self.settings,
                    "stop_event": self.stop_event,
                    "on_door_open": self._handle_door_open_event,
                },
                daemon=True,
                name="door-monitor-thread",
            )
            door_monitor_thread.start()

        log_action(
            "Passive mode scheduler started",
            action="passive_mode_scheduler_started",
            metadata={
                "scan_interval_hours": self.settings.scan_interval_hours,
                "door_open_detection_enabled": self.settings.door_open_detection_enabled,
            },
        )
        record_event(
            "mode",
            "Passive mode started",
            {
                "scan_interval_hours": self.settings.scan_interval_hours,
                "door_open_detection_enabled": self.settings.door_open_detection_enabled,
            },
        )

        self.run_scan()

        while not self._should_stop:
            self._check_scheduler_health()
            scheduler.run_pending()
            time.sleep(1)

        log_action("Passive mode scheduler stopped", action="passive_mode_scheduler_stopped")
        set_mode_running("passive", False)
        if self.stop_event is not None:
            self.stop_event.set()
        if door_monitor_thread is not None:
            door_monitor_thread.join(timeout=2)

    def run_scan(self, *, trigger: str = "schedule", image_bytes: bytes | None = None) -> None:
        if not self.scan_lock.acquire(blocking=False):
            log_action(
                "Passive scan skipped because another scan is already running",
                action="passive_scan_skipped_busy",
                metadata={"trigger": trigger},
            )
            return

        try:
            self.last_scan_started_at = time.monotonic()
            log_action(
                "Passive scan started",
                action="passive_scan_started",
                metadata={"staples_count": len(self.settings.staples), "trigger": trigger},
            )
            mark_mode_timestamp("passive", "last_passive_scan_timestamp")

            flagged_items: list[str] = []
            added_items: list[str] = []

            try:
                scan_image_bytes = image_bytes if image_bytes is not None else capture_frame_to_memory(settings=self.settings)
                flagged_items = check_staples(scan_image_bytes, self.settings.staples, settings=self.settings)
            except (CameraError, VisionError) as exc:
                report_issue(
                    "passive_scan_failures",
                    severity="warning",
                    message="Passive scan failed",
                    recommended_action="Review camera and vision logs to determine whether capture or model analysis is failing.",
                )
                log_exception(
                    "Passive scan failed during capture or vision analysis",
                    action="passive_scan_failed",
                    metadata={"error_type": type(exc).__name__, "trigger": trigger},
                )
                notify(
                    f"Passive scan failed: {exc}",
                    level=40,
                    send_desktop=False,
                    action="passive_scan_failed_notification",
                    metadata={"error_type": type(exc).__name__, "trigger": trigger},
                )
                return

            resolve_issue("passive_scan_failures")

            current_flagged = set(flagged_items)
            for item in list(self.flag_counts):
                if item not in current_flagged:
                    self.flag_counts.pop(item, None)

            for item in flagged_items:
                self.flag_counts[item] = self.flag_counts.get(item, 0) + 1
                log_action(
                    f"Flagged {item} during passive scan",
                    action="passive_scan_item_flagged",
                    metadata={"item_name": item, "flag_count": self.flag_counts[item], "trigger": trigger},
                )

                if self.flag_counts[item] < self.settings.flag_threshold:
                    continue

                added = add_item(item, settings=self.settings)
                if added:
                    added_items.append(item)
                    log_action(
                        f"Added {item} after repeated passive scan flags",
                        action="passive_scan_item_added",
                        metadata={
                            "item_name": item,
                            "flag_threshold": self.settings.flag_threshold,
                            "flag_count": self.flag_counts[item],
                            "trigger": trigger,
                        },
                    )
                else:
                    log_action(
                        f"Passive mode did not add {item}",
                        action="passive_scan_item_not_added",
                        metadata={"item_name": item, "flag_count": self.flag_counts[item], "trigger": trigger},
                    )

                self.flag_counts[item] = 0

            summary = f"Scan complete. Trigger: {trigger}. Flagged: {flagged_items}. Added: {added_items}."
            print(summary)
            record_event(
                "passive-scan",
                summary,
                {"flagged_items": flagged_items, "added_items": added_items, "trigger": trigger},
            )
            notify(
                summary,
                send_desktop=False,
                action="passive_scan_summary",
                metadata={"flagged_items": flagged_items, "added_items": added_items, "trigger": trigger},
            )
        finally:
            self.scan_lock.release()

    def _handle_door_open_event(self, frame: np.ndarray, event: DoorOpenEvent) -> None:
        try:
            success, encoded = cv2.imencode(".jpg", frame)
            if not success:
                raise CameraError("Failed to encode door-open snapshot")

            record_event(
                "door-open",
                "Door-open snapshot captured for passive scan",
                {
                    "changed_ratio": round(event.changed_ratio, 4),
                    "motion_duration_seconds": round(event.motion_duration_seconds, 3),
                },
            )
            self.run_scan(trigger="door-open", image_bytes=encoded.tobytes())
        except Exception as exc:  # noqa: BLE001
            report_issue(
                "door_open_snapshot_failed",
                severity="warning",
                message="Door-open event was detected but snapshot handling failed",
                recommended_action="Review camera encoding and passive scan logs for door-triggered scans.",
            )
            log_exception(
                "Door-open snapshot handling failed",
                action="door_open_snapshot_failed",
                metadata={"error_type": type(exc).__name__},
            )

    def _check_scheduler_health(self) -> None:
        if self.last_scan_started_at is None:
            return

        expected_interval = float(self.settings.scan_interval_hours) * 3600.0
        grace_period = min(120.0, max(15.0, expected_interval * 0.1))
        overdue_by = time.monotonic() - self.last_scan_started_at - expected_interval

        if overdue_by > grace_period:
            report_issue(
                "passive_scheduler_late",
                severity="critical",
                message="Passive mode scheduler is not triggering on time",
                recommended_action="Check whether the passive loop is blocked, crashed, or stalled by a long-running task.",
            )
        else:
            resolve_issue("passive_scheduler_late")

    @property
    def _should_stop(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()


def run(settings: Settings | None = None, stop_event: threading.Event | None = None) -> None:
    runner = PassiveModeRunner(settings=settings or load_settings(), stop_event=stop_event)
    runner.run()


def run_passive_mode(settings: Settings | None = None, stop_event: threading.Event | None = None) -> None:
    run(settings=settings, stop_event=stop_event)