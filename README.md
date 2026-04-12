# Grocery AI

Grocery AI is a local-first home grocery automation system that watches a camera feed, detects missing household staples, and updates a Skylight grocery list.

The application is designed around two operating modes:

- Passive mode: runs scheduled scans of a fridge, pantry, or storage area, and can also trigger scans when refrigerator or cabinet doors open so items are visible.
- Gesture mode: watches a live camera feed, detects when a user points at an item, identifies the item, and adds it to the list in real time.

The project also includes a local administrator interface for monitoring camera, Ollama, and Skylight health, reviewing logs, and spotting likely failure conditions before they become silent problems.

## Key Features

- Local image processing with Ollama and LLaVA
- Camera support for USB webcams and RTSP streams
- Gesture-based item selection with MediaPipe
- Automated passive scanning with threshold-based item adds
- Door-open-triggered passive snapshots for cabinets and refrigerator views
- Local logging with recent event history
- Localhost-only admin dashboard for status, alerts, and logs

## Architecture Overview

- [config.py](/Users/redridingh00d/grocery-list/config.py): loads environment variables and operational settings
- [camera.py](/Users/redridingh00d/grocery-list/camera.py): captures camera frames and JPEG bytes
- [door_monitor.py](/Users/redridingh00d/grocery-list/door_monitor.py): detects door-open motion and triggers passive snapshots
- [vision.py](/Users/redridingh00d/grocery-list/vision.py): sends image prompts to Ollama/LLaVA
- [skylight.py](/Users/redridingh00d/grocery-list/skylight.py): authenticates with Skylight and manages grocery list items
- [gesture.py](/Users/redridingh00d/grocery-list/gesture.py): detects pointing and clear-list gestures
- [passive_mode.py](/Users/redridingh00d/grocery-list/passive_mode.py): scheduled low-stock scans
- [gesture_mode.py](/Users/redridingh00d/grocery-list/gesture_mode.py): live interactive gesture loop
- [notifier.py](/Users/redridingh00d/grocery-list/notifier.py): console logs, file logs, recent events, and optional desktop notifications
- [health.py](/Users/redridingh00d/grocery-list/health.py): shared in-memory health snapshot and issue tracking
- [admin_ui.py](/Users/redridingh00d/grocery-list/admin_ui.py): local admin dashboard
- [main.py](/Users/redridingh00d/grocery-list/main.py): startup checks and mode orchestration

## Requirements

- Python 3.11+
- Local Ollama instance with the configured LLaVA model available
- A working camera source
- Skylight credentials and IDs

## Installation

Create and activate a virtual environment, then install the pinned dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Optional desktop notifications:

```bash
python -m pip install plyer==2.1.0
```

## Configuration

Create a `.env` file in the repository root.

Example:

```env
SKYLIGHT_EMAIL=your_email@example.com
SKYLIGHT_PASSWORD=your_password
SKYLIGHT_FRAME_ID=your_frame_id
SKYLIGHT_LIST_ID=your_grocery_list_id
CAMERA_INDEX=rtsp://user:pass@camera-ip:554/Preview_01_sub
DOOR_OPEN_DETECTION_ENABLED=true
DOOR_OPEN_SAMPLE_FPS=5
DOOR_OPEN_MOTION_RATIO_THRESHOLD=0.08
DOOR_OPEN_INTENSITY_THRESHOLD=25
DOOR_OPEN_SETTLE_SECONDS=1.25
DOOR_OPEN_COOLDOWN_SECONDS=20
DOOR_OPEN_MIN_MOTION_SECONDS=0.35
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llava
SCAN_INTERVAL_HOURS=4
POINTING_HOLD_SECONDS=2
FLAG_THRESHOLD=2
UNKNOWN_ITEM_THRESHOLD=3
CAMERA_RETRY_ATTEMPTS=3
CAMERA_RETRY_DELAY_SECONDS=5
OLLAMA_TIMEOUT_SECONDS=30
SKYLIGHT_TIMEOUT_SECONDS=10
ADMIN_UI_REFRESH_SECONDS=5
ADMIN_UI_HOST=127.0.0.1
ADMIN_UI_PORT=8765
```

`CAMERA_INDEX` can be either a numeric webcam index such as `0` or an RTSP URL.

## Reolink E1 Pro Notes

This application is compatible with the Reolink E1 Pro through RTSP.

Recommended RTSP URL patterns:

```text
rtsp://user:pass@camera-ip:554/Preview_01_sub
rtsp://user:pass@camera-ip:554/Preview_01_main
```

Recommended usage:

- Use `Preview_01_sub` for continuous passive monitoring and door-open detection because it reduces bandwidth and decode load.
- Use `Preview_01_main` only if the lower-resolution stream is not giving enough image detail for recognition quality.
- Keep the camera on the local network only.
- Make sure RTSP is enabled in the camera configuration before testing the stream in the app.

If passive mode starts showing low FPS, delayed scans, or unstable door-open detection, the first thing to try is switching from the main stream to the sub stream.

## Reolink E1 Pro Troubleshooting

If the camera is configured but the app is unstable, use this sequence:

1. Confirm the RTSP URL still works and points to the current camera IP.
2. Prefer `Preview_01_sub` for passive monitoring and door-open detection.
3. Switch to `Preview_01_main` only if recognition quality is too low on the sub stream.
4. If the admin UI shows low FPS, delayed scans, or degraded camera health, move back to the sub stream.
5. If door-open detection fires too often, increase `DOOR_OPEN_MOTION_RATIO_THRESHOLD`, `DOOR_OPEN_SETTLE_SECONDS`, or `DOOR_OPEN_COOLDOWN_SECONDS`.

Common Reolink-related symptoms:

- Camera repeatedly disconnects: verify RTSP is enabled, the password is correct, and the camera still has the same local IP.
- Passive mode is lagging: use the sub stream and reduce monitoring load before changing app logic.
- Door-open detection is too sensitive: raise the motion threshold and cooldown values.
- Items are too blurry to identify: test the main stream, but watch for FPS degradation in the admin UI.

## Running The App

Passive mode:

```bash
python main.py --mode passive
```

Gesture mode:

```bash
python main.py --mode gesture
```

Both modes together:

```bash
python main.py --mode both
```

On startup, the application will:

- validate required environment variables
- verify `.env` ignore protection
- check Ollama availability and model presence
- authenticate with Skylight
- start the admin UI
- print a startup summary and the admin UI URL

In passive mode, the application can now also watch for likely refrigerator or cabinet door openings and run a scan once the scene settles and the contents become visible.

## Administrator Interface

The admin UI runs locally and binds to `127.0.0.1` by default.

Default URL:

```text
http://127.0.0.1:8765
```

The dashboard is designed for quick operational checks. It refreshes automatically every 5 seconds.

### Main Sections

Connections

- Shows current status for camera, Ollama, LLaVA model availability, Skylight auth, and Skylight API reachability.
- Each card includes current state, recent success or failure timestamps, the latest error, and consecutive failure count.

Modes

- Shows whether passive mode and gesture mode are running.
- Displays the most recent passive scan timestamp.
- Displays the most recent successful gesture detection timestamp.
- Displays the most recent successful item add timestamp.

Active Issues

- Lists warnings and critical conditions currently being tracked.
- Each issue shows severity, issue code, message, recommended action, and timestamps.
- Use the `Acknowledge alert` button to mark a warning as reviewed without deleting the record.

Important Events

- Shows recent high-level system events such as startup checks, scan results, gesture confirmations, item additions, and list clears.

Log Viewer

- Shows recent structured application logs.
- Supports filtering by severity.
- Supports filtering by module name.
- Supports free-text search across log messages and metadata.

### Admin UI Actions

Run health check now

- Re-tests camera capture, Ollama connectivity, LLaVA model availability, Skylight authentication, and Skylight API access.
- Useful after restarting Ollama, fixing credentials, or reconnecting the camera.

Acknowledge alert

- Marks an alert as reviewed.
- The issue remains in history and can become active again if the condition reoccurs.

### How To Navigate As An Admin

If the system is not behaving correctly, use this sequence:

1. Open the Connections section and confirm camera, Ollama, and Skylight are healthy.
2. Check Active Issues for warnings or critical conditions.
3. Review Important Events to see the last successful system actions.
4. Use the Log Viewer filters to inspect the failing module in detail.
5. Click `Run health check now` after making any infrastructure or credential change.

### What To Watch For

- `camera` shows `disconnected` or repeated failures
- `ollama` shows `degraded` or `disconnected`
- `llava` shows model unavailable
- `skylight_auth` or `skylight_api` shows degraded state
- passive mode stopped unexpectedly
- repeated duplicate item add skips
- gesture loop low-FPS warnings
- repeated `unknown` item detections

## Logging

The application writes logs to:

- console output
- rotating log files in `logs/`
- in-memory recent log storage used by the admin UI

The log viewer in the admin dashboard is the easiest place for an administrator to inspect recent actions without opening raw log files.

## Dependency Audit

Run the dependency audit before release or after changing dependencies.

```bash
python audit_dependencies.py
```

This checks package consistency with `pip check` and runs `pip-audit` when available.

## Security Notes

- `.env` should never be committed
- admin UI binds to localhost by default
- images are processed locally
- avoid storing raw image payloads or secrets in logs
- review [SECURITY.md](/Users/redridingh00d/grocery-list/SECURITY.md) before changing dependencies

## Current Status

The repository contains the core application modules, passive mode, gesture mode, health snapshot, and admin UI. Real-world behavior still depends on your local camera setup, Ollama availability, and the current behavior of the unofficial Skylight API.