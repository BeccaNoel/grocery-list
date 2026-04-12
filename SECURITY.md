## Dependency Review

This project cannot guarantee that any third-party package is permanently safe, but it can reduce risk by using a small dependency set, pinning versions, and avoiding weakly maintained packages unless they are optional.

### Approved Core Dependencies

- `requests==2.33.1`
  - Widely used, actively maintained, mature HTTP client.
  - Acceptable for Skylight and Ollama API access.

- `python-dotenv==1.2.2`
  - Actively maintained and recently released.
  - Acceptable for local `.env` loading in development.

- `schedule==1.2.2`
  - Small dependency surface and no heavy transitive dependency chain.
  - Acceptable for in-process passive-mode scheduling.

- `opencv-python==4.13.0.92`
  - Current maintained wheel package for OpenCV.
  - Acceptable for camera capture and local frame processing.
  - Only one OpenCV wheel variant should be installed in an environment.

- `mediapipe==0.10.33`
  - Actively published and suitable for on-device hand tracking.
  - Acceptable for gesture recognition.
  - Legacy solution APIs should be avoided where newer supported APIs exist.

### Restricted / Optional Dependencies

- `plyer==2.1.0`
  - Optional only.
  - Latest PyPI release is older and maintenance signals are weaker than the rest of the stack.
  - Do not make application correctness depend on it.
  - Use only for best-effort desktop notifications.

### Dependency Rules

- Install only pinned versions from [requirements.txt](requirements.txt).
- Prefer packages with active maintenance, recent releases, and clear documentation.
- Avoid adding dependencies for simple functionality that the Python standard library already covers.
- Any new dependency must be reviewed for:
  - release recency
  - maintenance quality
  - size of transitive dependency tree
  - need for native extensions or bundled binaries
  - whether the feature is core or optional
- Optional features must fail gracefully when their package is missing.

### Operational Safeguards

- Keep `pip` updated before installation.
- Use isolated virtual environments.
- Run `pip install --requirement requirements.txt` instead of ad hoc installs.
- Run a dependency audit before release, for example with `pip-audit` or an equivalent scanner.
- Review dependency changes before upgrading pinned versions.
- Do not install multiple OpenCV wheel variants in the same environment.

### Current Decision

- Continue building with `requests`, `python-dotenv`, `schedule`, `opencv-python`, and `mediapipe`.
- Keep `plyer` optional and outside the default install set.