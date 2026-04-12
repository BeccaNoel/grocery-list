Build a home grocery automation system with the following requirements:

## OVERVIEW
A Python-based system that uses a network camera to automatically manage a Skylight grocery list. 
It has two modes:
1. PASSIVE MODE - Periodically scans the camera feed and detects low/missing staple items automatically
2. GESTURE MODE - Detects when a user points at an item and adds it to the list in real time
3. Build one file at a time then ask the user to review before moving on to build the next file. 

---

## TECH STACK
- Python 3.11+
- OpenCV (cv2) for camera feed capture and frame processing
- MediaPipe for hand landmark detection and gesture recognition
- Ollama with LLaVA model for fully local image analysis (no cloud, no data leaves the network)
- Requests library for Skylight API calls
- python-dotenv for environment variable management
- Schedule library for passive mode timing

---

## INSTALLATION AND DEPENDENCY AUDIT
- Use a Python 3.11+ virtual environment
- Upgrade pip before installing dependencies
- Install approved pinned dependencies only from requirements.txt
- Default install command:
  - `python -m pip install --upgrade pip`
  - `python -m pip install -r requirements.txt`
- Desktop notifications are optional and must not be part of the core install path
  - Optional command: `python -m pip install plyer==2.1.0`
- Before release or after dependency changes, run the dependency audit script:
  - `python audit_dependencies.py`
- The dependency audit should check installed package consistency and report known vulnerable packages when the audit tooling is available
- Dependency upgrades must be reviewed against SECURITY.md before changing pinned versions

---

## PROJECT STRUCTURE
grocery-ai/
├── .env                   # Secrets - never commit this
├── .gitignore             # Must include .env, frames/, __pycache__
├── requirements.txt       # All pip dependencies
├── config.py              # Loads env vars and constants
├── camera.py              # Camera feed capture and frame grabbing
├── door_monitor.py        # Detects refrigerator/cabinet door-open motion events
├── gesture.py             # MediaPipe hand tracking and pointing detection
├── vision.py              # Ollama/LLaVA image analysis
├── skylight.py            # Skylight API auth and list management
├── notifier.py            # Console + optional desktop notifications
├── passive_mode.py        # Scheduled staple item scanning
├── gesture_mode.py        # Real-time pointing detection loop
└── main.py                # Entry point, mode selection

---

## ENVIRONMENT VARIABLES (.env file)
SKYLIGHT_EMAIL=your_email@example.com
SKYLIGHT_PASSWORD=your_password
SKYLIGHT_FRAME_ID=your_frame_id
SKYLIGHT_LIST_ID=your_grocery_list_id
CAMERA_INDEX=0                        # or RTSP URL, e.g. Reolink E1 Pro: rtsp://user:pass@camera-ip:554/Preview_01_sub
DOOR_OPEN_DETECTION_ENABLED=true      # Trigger scans when cabinet/fridge doors open
DOOR_OPEN_SAMPLE_FPS=5                # Continuous monitoring sample rate
DOOR_OPEN_MOTION_RATIO_THRESHOLD=0.08 # Fraction of frame that must change to count as door motion
DOOR_OPEN_INTENSITY_THRESHOLD=25      # Pixel delta threshold for motion mask
DOOR_OPEN_SETTLE_SECONDS=1.25         # Wait for view to stabilize after opening
DOOR_OPEN_COOLDOWN_SECONDS=20         # Minimum time between door-triggered snapshots
DOOR_OPEN_MIN_MOTION_SECONDS=0.35     # Ignore tiny scene changes that do not resemble a door opening
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llava
SCAN_INTERVAL_HOURS=4                 # How often passive mode runs
POINTING_HOLD_SECONDS=2              # How long user must point to confirm
FLAG_THRESHOLD=2                     # How many scans item must be flagged before adding

---

## STAPLES LIST (config.py)
Hardcoded list of items the household always needs. Example:
STAPLES = [
    "milk", "eggs", "butter", "bread", "coffee",
    "olive oil", "pasta", "rice", "chicken", "cheese",
    "yogurt", "orange juice", "apples", "bananas",
    "toilet paper", "dish soap", "laundry detergent"
]
This list should be easy to edit. Each item is a string the vision model checks for.

---

## CAMERA MODULE (camera.py)
- Support both USB webcam (cv2.VideoCapture(index)) and RTSP IP camera streams
- Reolink E1 Pro should be supported through its RTSP stream path
- For Reolink E1 Pro, prefer the lower-bandwidth sub stream for continuous monitoring when possible:
  - Example: rtsp://user:pass@camera-ip:554/Preview_01_sub
- If higher image detail is needed for item recognition, the main stream may be used instead:
  - Example: rtsp://user:pass@camera-ip:554/Preview_01_main
- Function: capture_frame() → returns a single numpy image frame
- Function: capture_frame_to_file(path) → saves frame as JPEG and returns path
- Function: capture_frame_to_memory() → returns frame as in-memory bytes (preferred, avoids disk writes)
- Always release the capture object after grabbing a frame
- Handle connection errors with retry logic (3 attempts, 5 second delay)
- Log failures clearly

---

## GESTURE MODULE (gesture.py)
Use MediaPipe Hands to detect pointing gestures in real time.

- Initialize MediaPipe with max_num_hands=1, min_detection_confidence=0.8
- Function: is_pointing(hand_landmarks) → bool
  - Returns True if index finger is extended and other fingers are curled
  - Use landmark positions: INDEX_FINGER_TIP (8), INDEX_FINGER_PIP (6), 
    MIDDLE_FINGER_TIP (12), RING_FINGER_TIP (16), PINKY_TIP (20)
  - Index tip must be above its PIP joint (finger extended)
  - Other fingertips must be below their PIP joints (fingers curled)

- Function: get_pointing_direction(hand_landmarks, frame_shape) → (x, y) pixel coordinate
  - Draw a vector from WRIST (0) through INDEX_FINGER_TIP (8)
  - Extend that vector forward to estimate what the finger is pointing at
  - Return the pixel coordinate at the end of the extended vector
  - Extend by a factor of 2.5x the wrist-to-tip distance

- Function: crop_target_region(frame, target_xy, size=150) → cropped numpy image
  - Crop a 150x150 pixel box centered on the target coordinate
  - Clamp coordinates to stay within frame bounds

- Gesture confirmation logic:
  - User must hold pointing gesture for POINTING_HOLD_SECONDS (from .env) continuously
  - Use a timer that resets if the gesture is lost
  - Only trigger item identification after the hold duration is met
  - Show a visual countdown overlay on the frame during the hold period

---

## VISION MODULE (vision.py)
Use Ollama running locally with the LLaVA model. No images are ever sent to external APIs.

- Function: identify_item(image_bytes) → string (item name) or None
  - Convert image to base64
  - Send to Ollama API at OLLAMA_HOST/api/generate
  - Prompt: "What grocery or household item is shown in this image? 
             Reply with only the item name, nothing else. 
             If you cannot identify a grocery or household item, reply with 'unknown'."
  - Parse response, strip whitespace
  - Return None if response is 'unknown' or empty

- Function: check_staples(image_bytes, staples_list) → list of missing/low items
  - Send full frame to LLaVA with the staples list
  - Prompt: "You are checking a fridge/pantry. Here is a list of items that should 
             always be stocked: {staples}. Looking at this image, which items from 
             the list appear to be missing or running very low? 
             Reply ONLY with a JSON array of item names. Example: ["milk", "eggs"]. 
             If everything looks stocked, reply with an empty array: []"
  - Parse the JSON array response safely (wrap in try/except)
  - Return empty list on parse failure, log the raw response for debugging

- All image processing happens in memory - no frames written to disk
- Set Ollama request timeout to 30 seconds
- Log model response time for performance monitoring

---

## SKYLIGHT MODULE (skylight.py)
Use the unofficial Skylight API at https://app.ourskylight.com

Authentication:
- Function: authenticate() → token string
  - POST to /api/sessions with email and password
  - Extract user_id and token from response data
  - Generate Basic auth token: base64(user_id:token)
  - Cache the token in memory for the session
  - Re-authenticate automatically if a request returns 401

List management:
- Function: get_list_items() → list of strings (current items on the grocery list)
  - GET /api/frames/{FRAME_ID}/lists/{LIST_ID}
  - Parse the included array for list item names
  - Return lowercase stripped names for comparison

- Function: add_item(item_name) → bool (success)
  - First call get_list_items() to check if item is already on the list
  - Skip and return False if item already exists (case-insensitive match)
  - POST to /api/frames/{FRAME_ID}/lists/{LIST_ID}/items
  - Body: {"data": {"type": "list_item", "attributes": {"name": item_name}}}
  - Return True on 200/201, False on failure
  - Log success and failure clearly

- All requests use a 10 second timeout
- Handle network errors gracefully, do not crash the main loop

## CLEAR LIST FUNCTION (add to skylight.py)

- Function: clear_list() → bool (success)
  - GET /api/frames/{FRAME_ID}/lists/{LIST_ID} to retrieve all current item IDs
  - Loop through each item and DELETE /api/frames/{FRAME_ID}/lists/{LIST_ID}/items/{ITEM_ID}
  - Log how many items were deleted
  - Return True if all deletions succeeded, False if any failed
  - If list is already empty, log "List already empty" and return True


---

## PASSIVE MODE (passive_mode.py)
Runs on a schedule to automatically detect low/missing staples.

- Use the schedule library to run every SCAN_INTERVAL_HOURS hours
- Also run once immediately on startup
- Also monitor the live camera feed for refrigerator/cabinet door-open motion
- When a likely door-open event is detected:
  1. Wait for the scene to settle so the shelves are visible
  2. Capture a stabilized snapshot from the current frame
  3. Run the same staple-check logic on that snapshot
  4. Enforce a cooldown so repeated motion does not spam scans
- Each scan:
  1. Capture a frame from the camera
  2. Send to vision.check_staples() with the STAPLES list
  3. For each flagged item, increment a counter in a local dict (flag_counts)
  4. If an item's flag_count reaches FLAG_THRESHOLD, add it to Skylight and reset its counter
  5. Log what was detected and what was added
- Record what triggered the scan: startup, schedule, or door-open
- flag_counts persists in memory across scans (resets on restart)
- Print a summary after each scan: "Scan complete. Trigger: [source]. Flagged: [x, y]. Added: [z]."

---

## GESTURE MODE (gesture_mode.py)
Real-time loop that watches the camera and responds to pointing gestures.

- Open camera feed with OpenCV (live video, not single frames)
- Run MediaPipe on every frame
- When pointing gesture detected:
  - Show visual overlay: highlight the detected pointing direction with an arrow
  - Start hold timer, show countdown bar on screen
  - After POINTING_HOLD_SECONDS:
    - Crop the target region using gesture.crop_target_region()
    - Send to vision.identify_item()
    - If item identified and not 'unknown':
      - Call skylight.add_item()
      - Show confirmation overlay: green checkmark + item name for 3 seconds
    - If unknown: show red X overlay for 2 seconds
  - Reset and wait for next gesture
- Press Q to quit the gesture mode window
- Run at target 30fps, skip MediaPipe on every other frame for performance

## CLEAR LIST GESTURE (add to gesture.py)

- Function: is_clear_gesture(hand_landmarks) → bool
  - Returns True if ALL five fingers are extended and spread open (open flat hand / "stop" sign)
  - All fingertips (INDEX 8, MIDDLE 12, RING 16, PINKY 20, THUMB 4) must be above 
    their respective PIP joints
  - Hand must be roughly facing the camera (palm forward)
  - This is intentionally the opposite of the pointing gesture to avoid false triggers

- Confirmation logic (MUST be strict to prevent accidental clears):
  - User must hold the open flat hand gesture for 3 seconds (hardcoded, not configurable)
  - After 3 seconds, show a WARNING overlay on screen:
    "Clear entire list? Hold gesture 3 more seconds to confirm. Drop hand to cancel."
  - User must hold for another 3 seconds to confirm (two-stage hold)
  - Total commitment required: 6 seconds of continuous gesture
  - If hand drops at any point during either stage, reset entirely with no action taken
  - Show a red progress bar during the first stage, a flashing red bar during confirmation stage

---

## NOTIFIER MODULE (notifier.py)
- Function: notify(message) 
  - Always print to console with timestamp
  - If plyer is installed, also send a desktop notification
  - Gracefully skip desktop notification if plyer is not available
- Use this for: items added, scan summaries, errors, gesture confirmations

## UPDATE TO NOTIFIER (notifier.py)

- Add a notify_clear(item_count) function specifically for list clears
  - Message: "Grocery list cleared — {item_count} items removed. Ready for next delivery!"
  - Desktop notification priority should be HIGH for clears (more important than adds)

---

## MAIN ENTRY POINT (main.py)
- Parse command line argument: python main.py --mode passive OR --mode gesture OR --mode both
- "both" runs passive mode on a background thread and gesture mode on the main thread
- On startup, validate all required env vars are present and exit with clear error if not
- On startup, verify Ollama is running and LLaVA model is available (GET /api/tags)
- On startup, authenticate with Skylight and verify connection
- Print startup summary: mode, camera source, scan interval, staples list

---

## ADMINISTRATOR USER INTERFACE
Provide a simple administrator interface for monitoring system health, connection status, and likely failure conditions.

- The admin UI should be available locally only and should not require any external cloud service
- The admin UI should show real-time or near-real-time status for all major application connections:
  - Camera connection status
  - Ollama service availability
  - LLaVA model availability
  - Skylight authentication status
  - Skylight API reachability
- Each connection should display:
  - Current state: healthy, degraded, disconnected, or unknown
  - Last successful check timestamp
  - Last failed check timestamp
  - Human-readable error message for the most recent failure
  - Consecutive failure count
- The admin UI should show operational status for each application mode:
  - Passive mode running/stopped state
  - Gesture mode running/stopped state
  - Time of last passive scan
  - Time of last successful gesture detection
  - Time of last successful item add to Skylight
- The admin UI should include an alerts/issues panel that flags potential bugs or abnormal behavior, including:
  - Camera returns empty frames repeatedly
  - Camera reconnect loop exceeds 3 consecutive failures
  - Ollama request time exceeds 30 seconds
  - Vision model returns invalid JSON for staple checks
  - Vision model returns 'unknown' repeatedly above a configurable threshold
  - Skylight authentication fails or token refresh loops repeatedly
  - Skylight add_item requests fail repeatedly
  - Passive mode scheduler stops triggering on time
  - Gesture loop frame rate drops below 10 FPS for more than 30 seconds
  - Duplicate add attempts for the same item happen repeatedly, indicating possible state mismatch
- Each flagged issue should include:
  - Severity: info, warning, or critical
  - First detected timestamp
  - Most recent occurrence timestamp
  - A short recommended action
  - Whether the issue is currently active or resolved
- The admin UI should maintain an in-memory health snapshot object updated by all modules
- Each module should report health events into that shared health snapshot instead of only printing logs
- The admin UI should show the last 50 important events, such as:
  - Startup checks
  - Connection failures and recoveries
  - Scan runs
  - Gesture confirmations
  - Item additions
  - List clear events
- The admin UI should support a manual "Run health check now" action that re-tests camera, Ollama, and Skylight connectivity
- The admin UI should support a manual "Acknowledge alert" action for warnings without deleting historical records
- The admin UI should include a log viewer so the user can see each important action the application has taken
- The admin UI should refresh automatically at least every 5 seconds
- The interface can be implemented as a lightweight local web dashboard or a desktop window, but it must remain simple and easy to run on the home network
- If a web dashboard is used, bind only to localhost by default
- The UI should make it obvious when the system is unsafe to trust, especially if passive scans are not running or Skylight writes are failing
- Handle KeyboardInterrupt cleanly (Ctrl+C to stop)

---

## LOGGING AND AUDIT TRAIL
The system must keep a user-visible record of actions, decisions, failures, and recoveries.

- Every major action should produce a structured log entry
- Logs should be written both:
  - To the console for live visibility
  - To a local log file for later review
  - To the admin UI log viewer for non-technical users
- Create a dedicated logging module or extend notifier.py so all modules use one consistent logging path
- Log entries should include at minimum:
  - Timestamp in local time
  - Severity level: DEBUG, INFO, WARNING, ERROR
  - Module name
  - Action name
  - Human-readable message
  - Optional metadata payload as JSON-safe key/value fields
- The log file should rotate automatically so it does not grow forever
- Keep at least 7 days of recent logs or the latest 10 rotated files, whichever is easier to implement
- The admin UI log viewer should support:
  - Reverse chronological ordering (newest first)
  - Filtering by severity
  - Filtering by module
  - Searching by text
  - Viewing the most recent 100 to 500 log entries without freezing the UI
- The system should log the following actions explicitly so the user can see what happened:
  - Application startup and shutdown
  - Configuration validation success/failure
  - Camera connection attempts, failures, recoveries, and frame capture events
  - Passive scan start, completion, detected low-stock items, and add decisions
  - Gesture detection start, hold confirmation progress, identified item, and add decisions
  - Clear-list gesture stage transitions and final clear action
  - Skylight authentication, list reads, item add attempts, duplicate-skip decisions, and clear-list deletions
  - Ollama connectivity checks, model checks, request start/end, timeout, and parse failures
  - Health check results and alert state changes
- When a decision is made automatically, the reason should be logged clearly
  - Example: "Skipped adding milk because it already exists on the Skylight list"
  - Example: "Added eggs after being flagged in 2 consecutive passive scans"
- Errors should always include enough context for debugging without exposing secrets
- Never log passwords, tokens, raw authorization headers, or full image payloads
- If the application crashes due to an unhandled exception, log the stack trace to the local log file before exit when possible
- The user should be able to tell from the logs:
  - What action was attempted
  - Whether it succeeded or failed
  - Why the system made the decision it made
  - What the system did next
- Log format should be readable by humans first, but consistent enough for future machine parsing

## SECURITY REQUIREMENTS
- .env file must be in .gitignore - enforce this by checking for .env in .git/info/exclude on startup
- Never log image data, base64 strings, or raw API responses containing credentials
- Never write captured frames to disk - all image data processed in memory only
- All HTTP requests use HTTPS
- Skylight credentials loaded only from environment variables, never hardcoded
- Camera stream should be on local network only - document this clearly in README

---

## ERROR HANDLING
- Camera disconnection: retry 3 times with 5s delay, then log error and skip scan (do not crash)
- Ollama not running: log clear error "Ollama is not running. Start it with: ollama serve"
- Skylight API failure: log error, do not crash, retry on next scan
- Vision model returns unexpected format: log raw response, return safe default (empty list or None)
- All exceptions in the main loops must be caught and logged - the system should never fully crash

---

## REQUIREMENTS.TXT (generate this file with these packages)
opencv-python
mediapipe
requests
python-dotenv
schedule
plyer
Pillow

---

## README NOTES TO INCLUDE
- How to install Ollama: https://ollama.com
- How to pull LLaVA: ollama pull llava
- How to find Skylight Frame ID: log into ourskylight.com, copy number from URL
- Security note: camera should never be exposed to the internet
- How to run: python main.py --mode both


## UPDATE TO MAIN REQUIREMENTS SUMMARY

GESTURES SUMMARY (add this to README):
  - 👆 Point at item + hold 2 seconds  →  Adds item to grocery list
  - ✋ Open flat hand + hold 6 seconds (two-stage) →  Clears entire grocery list