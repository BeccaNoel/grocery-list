from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
REQUIREMENTS_FILE = REPO_ROOT / "requirements.txt"


def main() -> int:
    if not REQUIREMENTS_FILE.exists():
        print(f"Missing requirements file: {REQUIREMENTS_FILE}", file=sys.stderr)
        return 1

    print("Running dependency consistency checks...")
    pip_check_result = run_command([sys.executable, "-m", "pip", "check"])

    print("Running vulnerability audit...")
    pip_audit_command = resolve_pip_audit_command()
    if pip_audit_command is None:
        print(
            "pip-audit is not installed. Install it with 'python -m pip install pip-audit' to enable vulnerability scanning.",
            file=sys.stderr,
        )
        return 1 if pip_check_result != 0 else 2

    audit_result = run_command([*pip_audit_command, "-r", str(REQUIREMENTS_FILE)])
    if pip_check_result != 0 or audit_result != 0:
        return 1

    print("Dependency audit passed.")
    return 0


def resolve_pip_audit_command() -> list[str] | None:
    module_spec = subprocess.run(
        [sys.executable, "-c", "import importlib.util; print(int(importlib.util.find_spec('pip_audit') is not None))"],
        capture_output=True,
        text=True,
        check=False,
    )
    if module_spec.returncode == 0 and module_spec.stdout.strip() == "1":
        return [sys.executable, "-m", "pip_audit"]

    executable = shutil.which("pip-audit")
    if executable:
        return [executable]
    return None


def run_command(command: list[str]) -> int:
    result = subprocess.run(command, cwd=REPO_ROOT, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())