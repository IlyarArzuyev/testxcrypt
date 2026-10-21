#!/usr/bin/env python3
"""Bounded, manual Chromium capacity experiment on a disposable macOS runner."""
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import time

GIB = 1024 ** 3


def stop_group(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        if sig == signal.SIGTERM:
            time.sleep(2)
    proc.wait(timeout=10)


def run_stage(name, args, cwd, env, reports, deadline, seconds, reserve=15 * GIB):
    started = time.monotonic()
    record = {"name": name, "command": args, "status": "running",
              "free_gib_before": round(shutil.disk_usage(cwd).free / GIB, 2)}
    log_path = reports / (name + ".log")
    with log_path.open("w") as log:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        last_heartbeat = 0
        try:
            while proc.poll() is None:
                now = time.monotonic()
                free = shutil.disk_usage(cwd).free
                reason = None
                if free < reserve:
                    reason = "low_disk"
                elif now >= deadline or now - started >= seconds:
                    reason = "time_limit"
                if reason:
                    record["status"] = reason
                    stop_group(proc)
                    break
                if now - last_heartbeat >= 30:
                    print(f"{name}: {int(now-started)}s, free {free/GIB:.1f} GiB", flush=True)
                    last_heartbeat = now
                time.sleep(2)
        finally:
            if proc.poll() is None:
                stop_group(proc)
        record["return_code"] = proc.returncode
    if record["status"] == "running":
        record["status"] = "completed" if proc.returncode == 0 else "failed"
    record["seconds"] = round(time.monotonic() - started, 1)
    record["free_gib_after"] = round(shutil.disk_usage(cwd).free / GIB, 2)
    print(json.dumps(record), flush=True)
    return record


def git_sha(path):
    try:
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def main():
    root = Path.cwd().resolve()
    reports = root / "reports" / "chromium-trial"
    reports.mkdir(parents=True, exist_ok=True)
    work = root / "chromium-work"
    started = time.monotonic()
    deadline = started + 100 * 60
    report = {"status": "starting", "stages": [], "cpu_count": os.cpu_count(),
              "architecture": platform.machine(), "free_gib_start": round(shutil.disk_usage(root).free/GIB, 2),
              "limits": {"script_minutes": 100, "compile_minutes": 30, "parallel_jobs": 2,
                         "disk_reserve_gib": 15},
              "note": "Capacity experiment, not a Google login test or a distributable XCrypt build"}
    env = os.environ.copy()

    def save():
        report["elapsed_seconds"] = round(time.monotonic() - started, 1)
        report["free_gib_end"] = round(shutil.disk_usage(root).free / GIB, 2)
        tmp = reports / "summary.tmp"
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(reports / "summary.json")

    def stage(name, args, cwd, minutes):
        result = run_stage(name, args, cwd, env, reports, deadline, minutes * 60)
        report["stages"].append(result)
        save()
        if result["status"] != "completed":
            report["status"] = name + ":" + result["status"]
            raise RuntimeError(report["status"])

    try:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("Run only on an arm64 macOS build machine")
        if "CM_BUILD_ID" not in env:
            raise RuntimeError("Run only inside the dedicated Codemagic workflow")
        if report["free_gib_start"] < 100:
            raise RuntimeError("Less than 100 GiB free: skip this capacity experiment")
        # Never delete or reuse an unknown checkout.
        work.mkdir(exist_ok=False)
        depot = work / "depot_tools"
        stage("tools", ["git", "clone", "--depth=1",
              "https://chromium.googlesource.com/chromium/tools/depot_tools.git", str(depot)], work, 5)
        env["PATH"] = str(depot) + os.pathsep + env.get("PATH", "")
        env["DEPOT_TOOLS_UPDATE"] = "0"
        env["DEPOT_TOOLS_METRICS"] = "0"
        report["depot_tools_sha"] = git_sha(depot)
        checkout = work / "chromium"
        checkout.mkdir()
        stage("fetch", [str(depot / "fetch"), "--no-history", "chromium"], checkout, 60)
        src = checkout / "src"
        report["chromium_sha"] = git_sha(src)
        for filename, source in [("VERSION", src / "chrome/VERSION"),
                                 ("mac_sdk.gni", src / "build/config/mac/mac_sdk.gni")]:
            if source.is_file():
                shutil.copy2(source, reports / filename)
        out = src / "out/CapacityTrial"
        out.mkdir(parents=True)
        args = ('target_cpu = "arm64"\nis_debug = false\nis_component_build = true\n'
                'symbol_level = 0\nuse_remoteexec = false\nuse_lld = false\n')
        (out / "args.gn").write_text(args)
        (reports / "args.gn").write_text(args)
        stage("generate", [str(depot / "gn"), "gen", "out/CapacityTrial"], src, 5)
        stage("compile", [str(depot / "autoninja"), "-C", "out/CapacityTrial", "-j", "2", "chrome"], src, 30)
        report["status"] = "chrome_target_completed"
    except Exception as exc:
        if report["status"] == "starting":
            report["status"] = "error"
        report["error"] = str(exc)
    finally:
        src = work / "chromium/src"
        report["chromium_sha"] = git_sha(src)
        ninja_log = src / "out/CapacityTrial/.ninja_log"
        if ninja_log.is_file():
            shutil.copy2(ninja_log, reports / "ninja.log")
            with ninja_log.open(errors="replace") as f:
                report["ninja_log_entries"] = sum(1 for line in f if not line.startswith("#"))
        report["browser_binary_exists"] = (src / "out/CapacityTrial/Chromium.app/Contents/MacOS/Chromium").is_file()
        save()
        print(json.dumps(report, indent=2), flush=True)
    # Incomplete trials deliberately fail; Codemagic still collects the logs/artifacts.
    return 0 if report["status"] == "chrome_target_completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
