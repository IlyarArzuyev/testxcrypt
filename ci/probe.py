#!/usr/bin/env python3
"""Small macOS CI probe. No downloads, credentials, or Chromium checkout."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile


def run(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return {"code": result.returncode, "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"code": -1, "error": str(exc)}


def main():
    gib = 1024 ** 3
    report = {
        "purpose": "Capacity probe only; not a Chromium build or Google login test",
        "os": platform.system(), "architecture": platform.machine(),
        "cpu_count": os.cpu_count(),
        "free_disk_gib": round(shutil.disk_usage(Path.cwd()).free / gib, 2),
        "billing_and_free_quota": "Not measured; verify in provider account before running",
    }
    if platform.system() == "Darwin":
        memory = run(["/usr/sbin/sysctl", "-n", "hw.memsize"])
        report["memory"] = memory
        if memory["code"] == 0 and memory["stdout"].isdigit():
            report["ram_gib"] = round(int(memory["stdout"]) / gib, 2)
        for name, command in {
            "macos_version": ["/usr/bin/sw_vers"],
            "developer_directory": ["/usr/bin/xcode-select", "-p"],
            "xcode": ["/usr/bin/xcodebuild", "-version"],
            "sdk_version": ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version"],
            "compiler": ["/usr/bin/xcrun", "clang++", "--version"],
        }.items():
            report[name] = run(command)
        with tempfile.TemporaryDirectory(prefix="xcrypt-probe-") as temp:
            source = Path(temp) / "hello.cc"
            binary = Path(temp) / "hello"
            source.write_text('#include <iostream>\nint main(){std::cout << "arm64 probe OK\\n";}\n')
            report["compile_arm64"] = run([
                "/usr/bin/xcrun", "clang++", "-arch", "arm64", "-std=c++20",
                str(source), "-o", str(binary)])
            if report["compile_arm64"]["code"] == 0:
                report["binary_format"] = run(["/usr/bin/file", str(binary)])
                if platform.machine() == "arm64":
                    report["execute_arm64"] = run([str(binary)])
    else:
        report["macos_checks"] = "Skipped: this host is not macOS"
    report["next_review"] = [
        "Compare measured resources with the selected Chromium revision and build settings",
        "Check required Xcode and SDK for that revision",
        "Check task time limit, monthly free quota, and artifact size limit",
        "A successful probe does not guarantee a complete Chromium build or Google sign-in",
    ]
    output = Path("reports/report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(output.read_text())


if __name__ == "__main__":
    main()
