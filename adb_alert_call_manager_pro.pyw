"""
ADB Device Alert & Bulk Call Console PRO

Controlled internal device-testing & bulk calling console.

Key Features:
- Multi-Tier Dedicated Log Files:
  * logs/operations.log: All call attempts, progress, states, and completions
  * logs/error.log: WARNING, ERROR, and CRITICAL exceptions with stack traces
  * logs/terminal.log: Full verbatim mirror of terminal stdout/stderr
  * logs/app.log: General rotating application log
  * logs/scrcpy/: Per-session screen mirroring logs
- Zero-Setup Automatic Bootstrap of Android Platform-Tools and scrcpy
- Binary UTF-8 Subprocess Safety (prevents Windows CP1252 charmap decode crashes)
- Carrier-Level Call Divert Guard:
  * On START: Prompts for a divert-to number, then sets unconditional call divert (*21*<number>#)
    so ALL incoming calls are forwarded at the network/tower level—no phone rings, no interruption.
  * On STOP (manual) or completion: Automatically cancels all diverts (##21#).
  * Duplicate numbers in the queue are automatically removed before each run.
- Precise Android 14/15/16 multi-SIM call state telemetry (ALERTING, DIALING, ACTIVE, IDLE)
- Bulk Call Runner with queue sync, progress tracking, pause/resume, and delay cooldown
"""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import customtkinter as ctk
    CTK = True
except (ImportError, ModuleNotFoundError):
    CTK = False
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

if CTK:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
ADB_DIR = TOOLS / "platform-tools"
SCRCPY_DIR = TOOLS / "scrcpy"
LOG_DIR = ROOT / "logs"
CONFIG_FILE = ROOT / "config.json"
RESULTS_FILE = ROOT / "test_results.csv"
SCRCPY_LOG_DIR = LOG_DIR / "scrcpy"

for d in (TOOLS, LOG_DIR, SCRCPY_LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Specialized log files
OPERATIONS_LOG_FILE = LOG_DIR / "operations.log"
ERROR_LOG_FILE = LOG_DIR / "error.log"
TERMINAL_LOG_FILE = LOG_DIR / "terminal.log"
APP_LOG_FILE = LOG_DIR / "app.log"

# Reconfigure standard streams for UTF-8 on Windows consoles
for stream in (sys.stdout, sys.stderr):
    if stream and hasattr(stream, "reconfigure"):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")

class TerminalCaptureStream:
    """
    Mirrors console output to logs/terminal.log file while passing through to stdout/stderr.
    """
    def __init__(self, original_stream, log_file_path):
        self.original_stream = original_stream
        self.log_file_path = log_file_path
        self._lock = threading.Lock()

    def write(self, data):
        if self.original_stream:
            with contextlib.suppress(Exception):
                self.original_stream.write(data)
                self.original_stream.flush()
        if data:
            with self._lock, contextlib.suppress(Exception), self.log_file_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(data)

    def flush(self):
        if self.original_stream and hasattr(self.original_stream, "flush"):
            with contextlib.suppress(Exception):
                self.original_stream.flush()

    def isatty(self):
        if self.original_stream and hasattr(self.original_stream, "isatty"):
            with contextlib.suppress(Exception):
                return bool(self.original_stream.isatty())
        return False

# Primary application logger setup with multi-file routing
LOGGER = logging.getLogger("adb_alert_pro")
LOGGER.setLevel(logging.DEBUG)

if not LOGGER.handlers:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    # 1. app.log (General combined rotating log)
    app_handler = RotatingFileHandler(APP_LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    app_handler.setLevel(logging.DEBUG)
    app_handler.setFormatter(fmt)
    LOGGER.addHandler(app_handler)

    # 2. operations.log (Only operational events)
    class OperationsFilter(logging.Filter):
        def filter(self, record):
            return record.levelno == logging.INFO

    ops_handler = RotatingFileHandler(OPERATIONS_LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    ops_handler.setLevel(logging.INFO)
    ops_handler.addFilter(OperationsFilter())
    ops_handler.setFormatter(fmt)
    LOGGER.addHandler(ops_handler)

    # 3. error.log (Only WARNING and ERROR/CRITICAL)
    err_handler = RotatingFileHandler(ERROR_LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(fmt)
    LOGGER.addHandler(err_handler)

    # 4. Console stream
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)
    LOGGER.addHandler(console_handler)

# Mirror stdout and stderr to logs/terminal.log
if not isinstance(sys.stdout, TerminalCaptureStream):
    sys.stdout = TerminalCaptureStream(sys.stdout, TERMINAL_LOG_FILE)
if not isinstance(sys.stderr, TerminalCaptureStream):
    sys.stderr = TerminalCaptureStream(sys.stderr, TERMINAL_LOG_FILE)

DEFAULT_CONFIG = {
    "platform_tools_url_windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "scrcpy_url_windows": "https://github.com/Genymobile/scrcpy/releases/latest/download/scrcpy-win64-v3.3.3.zip",
    "auto_download_tools": True,
    "scrcpy_fullscreen": False,
    "scrcpy_always_on_top": False,
    "scrcpy_show_touches": False,
    "country_code": "91",
    "adb_timeout": 10,
    "last_divert_number": "",

    # Controlled call settings (15s default ensures cellular calls have time to ring & connect)
    "dial_timeout_seconds": 15,
    "call_duration_seconds": 3,
    "post_call_delay_seconds": 2,
    "wait_for_idle_seconds": 5,
    "speaker_on": True,
    "call_volume_percent": 80,
    "media_volume_percent": 60,
}

CONFIG = DEFAULT_CONFIG.copy()
if CONFIG_FILE.exists():
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        CONFIG.update(loaded)
    except Exception:
        LOGGER.exception("Could not load config.json")

# Ensure dial timeout is positive
CONFIG["dial_timeout_seconds"] = max(1.0, float(CONFIG.get("dial_timeout_seconds", 15.0)))

CONFIG_FILE.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")

def log_exception(prefix, exc):
    LOGGER.exception("%s: %s", prefix, exc)

def run_cmd(args, timeout=30, cwd=None):
    """
    Execute subprocess command safely without Windows CP1252 charmap decoding errors.
    Captures raw bytes from subprocess.PIPE and decodes using UTF-8 with errors='replace'.
    """
    LOGGER.debug("RUN %r", args)
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout = p.stdout.decode("utf-8", errors="replace") if p.stdout else ""
        stderr = p.stderr.decode("utf-8", errors="replace") if p.stderr else ""
        return subprocess.CompletedProcess(args, p.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        LOGGER.warning("Command timeout (%ss): %r", timeout, args)
        return subprocess.CompletedProcess(args, 124, stdout, f"timeout: {exc}")
    except Exception as exc:
        LOGGER.exception("Command error on %r", args)
        return subprocess.CompletedProcess(args, 1, "", str(exc))

def exe(name):
    return name + (".exe" if platform.system() == "Windows" else "")

def locate_adb():
    """
    Locate adb binary.
    Prioritizes SCRCPY_DIR / adb.exe first to avoid daemon version conflicts.
    """
    candidates = [
        SCRCPY_DIR / exe("adb"),
        ADB_DIR / exe("adb"),
        Path(shutil.which("adb") or ""),
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")),
        Path(os.path.expandvars(r"%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe")),
    ]
    for c in candidates:
        with contextlib.suppress(OSError):
            if c and c.exists():
                return c
    return None

def locate_scrcpy():
    direct = SCRCPY_DIR / exe("scrcpy")
    if direct.exists():
        return direct
    if SCRCPY_DIR.exists():
        for c in SCRCPY_DIR.glob("**/" + exe("scrcpy")):
            return c
    p = shutil.which("scrcpy")
    return Path(p) if p else None

ADB = locate_adb()
SCRCPY = locate_scrcpy()

def download_file(url, destination, progress_cb=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Downloading %s -> %s", url, destination)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ADB-Device-Alert-Console/2.0"}
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        total = int(response.headers.get("content-length", 0))
        downloaded = 0
        with destination.open("wb") as target:
            while True:
                block = response.read(1024 * 512)
                if not block:
                    break
                target.write(block)
                downloaded += len(block)
                if progress_cb and total > 0:
                    pct = int((downloaded / total) * 100)
                    mb = downloaded / (1024 * 1024)
                    total_mb = total / (1024 * 1024)
                    progress_cb(f"Downloading tools: {pct}% ({mb:.1f}/{total_mb:.1f} MB)")

def install_platform_tools(progress_cb=None):
    global ADB
    existing = locate_adb()
    if existing:
        ADB = existing
        return True

    if platform.system() != "Windows":
        LOGGER.error("Automatic Platform-Tools bootstrap currently targets Windows.")
        return False

    archive = TOOLS / "platform-tools.zip"
    temp = TOOLS / "_platform_tools_extract"

    try:
        if progress_cb:
            progress_cb("Downloading Android Platform-Tools...")
        download_file(CONFIG["platform_tools_url_windows"], archive, progress_cb)
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        temp.mkdir(parents=True, exist_ok=True)

        if progress_cb:
            progress_cb("Extracting Platform-Tools...")
        with zipfile.ZipFile(archive) as z:
            z.extractall(temp)

        source = temp / "platform-tools"
        if not source.exists():
            raise RuntimeError("Downloaded Platform-Tools archive has unexpected structure")

        if ADB_DIR.exists():
            shutil.rmtree(ADB_DIR, ignore_errors=True)
        shutil.copytree(source, ADB_DIR)

        shutil.rmtree(temp, ignore_errors=True)
        archive.unlink(missing_ok=True)

        ADB = locate_adb()
        LOGGER.info("ADB installed: %s", ADB)
        return bool(ADB)
    except Exception as exc:  # noqa: BLE001
        log_exception("Platform-Tools bootstrap failed", exc)
        return False

def resolve_latest_scrcpy_asset():
    release_url = "https://github.com/Genymobile/scrcpy/releases/latest"
    req = urllib.request.Request(
        release_url,
        method="HEAD",
        headers={"User-Agent": "ADB-Device-Alert-Console/2.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            final = r.geturl()
    except (urllib.error.URLError, TimeoutError, OSError):
        final = ""

    m = re.search(r"/tag/v?([^/]+)$", final)
    version = m.group(1) if m else "4.1"

    candidates = [
        f"https://github.com/Genymobile/scrcpy/releases/download/v{version}/scrcpy-win64-v{version}.zip",
        f"https://github.com/Genymobile/scrcpy/releases/download/{version}/scrcpy-win64-v{version}.zip",
    ]
    return version, candidates

def install_scrcpy(progress_cb=None):
    global SCRCPY, ADB
    existing = locate_scrcpy()
    if existing:
        SCRCPY = existing
        ADB = locate_adb() or ADB
        return True

    if platform.system() != "Windows":
        LOGGER.error("Automatic scrcpy bootstrap currently targets Windows.")
        return False

    temp = TOOLS / "_scrcpy_extract"
    try:
        candidates = [
            "https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-win64-v4.1.zip",
            "https://github.com/Genymobile/scrcpy/releases/download/v3.3.3/scrcpy-win64-v3.3.3.zip",
        ]
        with contextlib.suppress(urllib.error.URLError, TimeoutError, OSError):
            _, dyn_candidates = resolve_latest_scrcpy_asset()
            candidates = dyn_candidates + candidates

        archive = TOOLS / "scrcpy-win64.zip"
        last_error = ""
        for url in candidates:
            try:
                if progress_cb:
                    progress_cb("Downloading scrcpy package...")
                download_file(url, archive, progress_cb)
                if archive.exists() and archive.stat().st_size > 1_000_000:
                    break
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_error = str(exc)
        else:
            raise RuntimeError(f"Could not download scrcpy: {last_error}")

        if progress_cb:
            progress_cb("Extracting scrcpy & bundled ADB...")
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        temp.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(archive) as z:
            z.extractall(temp)

        folders = [x for x in temp.iterdir() if x.is_dir()]
        if not folders:
            raise RuntimeError("scrcpy archive did not contain a directory")

        source = folders[0]
        if SCRCPY_DIR.exists():
            shutil.rmtree(SCRCPY_DIR, ignore_errors=True)
        shutil.copytree(source, SCRCPY_DIR)

        shutil.rmtree(temp, ignore_errors=True)
        archive.unlink(missing_ok=True)

        SCRCPY = locate_scrcpy()
        ADB = locate_adb()
        LOGGER.info("scrcpy installed: %s | ADB: %s", SCRCPY, ADB)
        return bool(SCRCPY)
    except Exception as exc:  # noqa: BLE001
        log_exception("scrcpy bootstrap failed", exc)
        return False

def bootstrap_tools(progress_cb=None):
    scrcpy_ok = bool(locate_scrcpy()) or install_scrcpy(progress_cb)
    adb_ok = bool(locate_adb()) or install_platform_tools(progress_cb)
    return adb_ok, scrcpy_ok

def adb_cmd(*args, serial=None, timeout=None):
    global ADB
    ADB = locate_adb() or ADB
    if not ADB:
        return None, "", "ADB not found"

    command = [str(ADB)]
    if serial:
        command += ["-s", serial]
    command += list(args)
    p = run_cmd(command, timeout=timeout or CONFIG.get("adb_timeout", 10))
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

def adb_devices():
    rc, out, _err = adb_cmd("devices")
    if rc is None or rc != 0:
        return []
    result = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices attached", "*")):
            continue
        bits = line.split()
        if len(bits) >= 2:
            result.append((bits[0], bits[1]))
    return result

def reconnect_device(serial=None):
    if serial:
        adb_cmd("reconnect", serial=serial, timeout=5)
    else:
        adb_cmd("reconnect", timeout=5)

def normalize_number(value):
    """
    Normalizes phone numbers to standard international format (+919820806052).
    """
    value = (value or "").strip()
    if not value:
        return None

    # MMI code pass-through (e.g. *21*..., ##21#)
    if value.startswith(("*", "#")):
        return value

    if value.startswith("+"):
        digits = "+" + re.sub(r"\D", "", value[1:])
        return digits if len(digits) >= 8 else None

    digits = re.sub(r"\D", "", value)
    if not digits:
        return None

    if digits.startswith("00") and len(digits) > 2:
        return "+" + digits[2:]

    # Remove leading 0 from 11-digit numbers (e.g. 09820806052 -> 9820806052)
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]

    cc = str(CONFIG.get("country_code", "91")).lstrip("+")
    if len(digits) == 10:
        return f"+{cc}{digits}"

    if digits.startswith(cc) and len(digits) == 10 + len(cc):
        return f"+{digits}"

    return ("+" + digits) if len(digits) >= 8 else None

def import_numbers(path):
    p = Path(path)
    raw = []

    if p.suffix.lower() == ".txt":
        raw = p.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    elif p.suffix.lower() == ".csv":
        with p.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            for row in csv.reader(f):
                raw.extend(row)
    elif p.suffix.lower() == ".vcf":
        text = p.read_text(encoding="utf-8-sig", errors="ignore")
        raw = re.findall(r"TEL[^:]*:\s*([^\r\n]+)", text, re.IGNORECASE)

    result = []
    seen = set()
    for item in raw:
        n = normalize_number(item)
        if n and n not in seen:
            seen.add(n)
            result.append(n)
    return result

def parse_numbers_from_text(text):
    result = []
    seen = set()
    for line in text.splitlines():
        parts = re.split(r"[,;\t]+", line)
        for part in parts:
            n = normalize_number(part)
            if n and n not in seen:
                seen.add(n)
                result.append(n)
    return result

def get_device_info(serial):
    props = {
        "Manufacturer": "ro.product.manufacturer",
        "Brand": "ro.product.brand",
        "Model": "ro.product.model",
        "Android": "ro.build.version.release",
        "SDK": "ro.build.version.sdk",
        "Serial": "ro.serialno",
    }
    lines = []
    for label, prop in props.items():
        rc, out, _ = adb_cmd("shell", "getprop", prop, serial=serial, timeout=4)
        lines.append(f"{label}: {out if rc == 0 and out else '?'}")

    rc, battery, _ = adb_cmd("shell", "dumpsys", "battery", serial=serial, timeout=5)
    m = re.search(r"level:\s*(\d+)", battery or "")
    lines.append(f"Battery: {m.group(1) + '%' if m else '?'}")

    return "\n".join(lines)

def is_incoming_call_ringing(text):
    """
    Reliably detects if an incoming call is currently ringing on ANY SIM slot.
    Word-boundary regexes prevent false positives from 'mprecisecallstate=ringing' or other fields.
    """
    if not text:
        return False
    t = text.lower()
    # Check ringing call state in telephony.registry
    if re.search(r"\bmringingcallstate\s*=\s*[1-9]", t):
        return True
    if re.search(r"\bringing call state\s*:\s*[1-9]", t):
        return True
    # Check general callstate=1 (RINGING)
    if re.search(r"\b(?:mcallstate|call_state)\s*=\s*1\b", t):
        return True
    # Check telecom dump state
    return bool(re.search(r"\bstate\s*=\s*ringing\b", t) or "callstate: ringing" in t)

def parse_call_state_text(text):
    """
    Parses Android dumpsys output to detect call state accurately across OEMs and multi-SIM.
    Returns: 'ACTIVE', 'DIALING', 'ALERTING', 'OFFHOOK', 'RINGING', 'HOLDING', 'DISCONNECTING', or 'IDLE'
    """
    if not text:
        return "IDLE"
    t = text.lower()

    # Incoming ring has absolute highest priority
    if is_incoming_call_ringing(t):
        return "RINGING"

    # Precise foreground call state across all SIM slots:
    # -1/0/7=IDLE/DISCONNECTED, 1=ACTIVE, 2=HOLDING, 3=DIALING, 4=ALERTING, 5/6=RINGING, 8=DISCONNECTING
    fg_matches = [int(x) for x in re.findall(r"(?:mforegroundcallstate|foreground call state)[:=]\s*(-?\d+)", t)]
    if fg_matches:
        if any(v == 1 for v in fg_matches):
            return "ACTIVE"
        if any(v == 4 for v in fg_matches):
            return "ALERTING"
        if any(v == 3 for v in fg_matches):
            return "DIALING"
        if any(v in (5, 6) for v in fg_matches):
            return "RINGING"
        if any(v == 2 for v in fg_matches):
            return "HOLDING"
        if any(v == 8 for v in fg_matches):
            return "DISCONNECTING"
        if all(v in (0, 7, -1) for v in fg_matches):
            return "IDLE"

    # Telecom dump checks
    if "state=active" in t or "callstate: active" in t:
        return "ACTIVE"
    if "state=alerting" in t or "callstate: alerting" in t:
        return "ALERTING"
    if "state=dialing" in t or "callstate: dialing" in t:
        return "DIALING"
    if "state=ringing" in t or "callstate: ringing" in t:
        return "RINGING"
    if "state=disconnected" in t or "callstate: disconnected" in t:
        return "IDLE"

    # Standard telephony.registry mCallState: 0=IDLE, 1=RINGING, 2=OFFHOOK
    call_states = [int(x) for x in re.findall(r"\b(?:callstate|mcallstate|call_state)\s*=\s*(\d+)", t)]
    if call_states:
        if any(v == 1 for v in call_states):
            return "RINGING"
        if any(v == 2 for v in call_states):
            return "OFFHOOK"
        if all(v == 0 for v in call_states):
            return "IDLE"

    return "IDLE"

def get_call_state(serial):
    _rc, out, _ = adb_cmd(
        "shell", "dumpsys", "telephony.registry",
        serial=serial, timeout=3
    )
    state = parse_call_state_text(out)
    if state != "IDLE":
        return state

    _rc2, out2, _ = adb_cmd(
        "shell", "dumpsys", "telecom",
        serial=serial, timeout=3
    )
    state2 = parse_call_state_text(out2)
    return state2 if state2 != "IDLE" else "IDLE"

def send_key(serial, key):
    rc, out, err = adb_cmd(
        "shell", "input", "keyevent", str(key),
        serial=serial, timeout=3
    )
    return rc == 0, (out or err)

def reject_current_call(serial):
    """
    Instantly terminates/rejects an incoming or active call.
    Uses multiple fallback vectors to guarantee immediate modem release.
    """
    ok1, msg = send_key(serial, "KEYCODE_ENDCALL")
    send_key(serial, "6")
    adb_cmd("shell", "service", "call", "phone", "5", serial=serial, timeout=2)
    adb_cmd("shell", "cmd", "telecom", "cleanup-stuck-calls", serial=serial, timeout=2)
    return ok1, msg

def set_volume(serial, stream, percent):
    _rc, out, _err = adb_cmd(
        "shell", "media_session", "volume", "--stream", str(stream),
        "--get", serial=serial, timeout=4
    )
    text = out or ""
    mmax = re.search(r"max:\s*(\d+)", text)
    maxv = int(mmax.group(1)) if mmax else 15

    target = max(0, min(maxv, round(maxv * (float(percent) / 100.0))))

    adb_cmd("shell", "cmd", "media_session", "volume",
            "--stream", str(stream), "--set", str(target),
            serial=serial, timeout=4)

    return True, f"stream={stream}, target={target}/{maxv}"

def set_speaker(serial, on=True):
    mode = "on" if on else "off"
    attempts = [
        ["shell", "cmd", "audio", "set-speakerphone", mode],
        ["shell", "cmd", "telecom", "set-speakerphone", mode],
    ]
    for cmd in attempts:
        rc, out, _err = adb_cmd(*cmd, serial=serial, timeout=3)
        out_clean = (out or "").strip()
        low = out_clean.lower()
        if rc == 0 and "no shell command implementation" not in low and "unknown command" not in low:
            return True, out_clean or f"speaker {mode} accepted"

    if on:
        adb_cmd("shell", "input", "keyevent", "KEYCODE_VOLUME_UP", serial=serial, timeout=2)
    return False, "Speakerphone control not supported by this Android OEM shell build"

def launch_scrcpy(serial):
    global SCRCPY
    SCRCPY = locate_scrcpy() or SCRCPY
    if not SCRCPY or not SCRCPY.exists():
        return False, "scrcpy executable not found"

    session_log = SCRCPY_LOG_DIR / (
        datetime.now().strftime("scrcpy_%Y%m%d_%H%M%S_") + f"{serial}.log"
    )

    # Wake device screen up
    adb_cmd("shell", "input", "keyevent", "KEYCODE_WAKEUP", serial=serial, timeout=3)

    args = [
        str(SCRCPY),
        "-s", serial,
        "--stay-awake",
        "--window-title", f"ADB Screen - {serial}",
    ]

    if CONFIG.get("scrcpy_fullscreen"):
        args.append("--fullscreen")
    if CONFIG.get("scrcpy_always_on_top"):
        args.append("--always-on-top")
    if CONFIG.get("scrcpy_show_touches"):
        args.append("--show-touches")

    try:
        creationflags = 0
        if platform.system() == "Windows":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        logf = session_log.open("w", encoding="utf-8", errors="ignore")
        proc = subprocess.Popen(
            args,
            cwd=str(SCRCPY.parent),
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

        def watcher():
            rc = proc.wait()
            with contextlib.suppress(Exception):
                logf.flush()
                logf.close()
            LOGGER.info("scrcpy exited pid=%s rc=%s log=%s", proc.pid, rc, session_log)
            if rc != 0:
                LOGGER.error("scrcpy failed; inspect %s", session_log)

        threading.Thread(target=watcher, daemon=True).start()
        return True, f"scrcpy PID {proc.pid}\nLog: {session_log.name}"
    except Exception as exc:
        logger_error = f"scrcpy launch failed: {exc}"
        LOGGER.exception(logger_error)
        return False, logger_error

def start_call(serial, number):
    # URI-encode '#' to '%23' so Android shell & Uri.parse do not treat '#' as a comment or fragment
    safe_number = urllib.parse.quote(str(number), safe="+*")
    uri = f"tel:{safe_number}"
    rc, out, err = adb_cmd(
        "shell", "am", "start",
        "-a", "android.intent.action.CALL",
        "-d", uri,
        serial=serial, timeout=8
    )
    if rc == 0:
        return True, out
    return False, err or out or "ACTION_CALL failed"

@dataclass
class CallTestResult:
    timestamp: str
    device: str
    number: str
    dialing_timeout_s: float
    call_duration_s: float
    post_call_delay_s: float
    speaker_enabled: bool
    call_volume_percent: int
    result: str
    message: str

def write_result(result: CallTestResult):
    has_header = RESULTS_FILE.exists() and RESULTS_FILE.stat().st_size > 0
    with RESULTS_FILE.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(result).keys()))
        if not has_header:
            writer.writeheader()
        writer.writerow(asdict(result))

def _mmi_safe_number(normalized: str) -> str:
    """
    Strips the leading '+' from a normalized E.164 number for use inside
    MMI codes (e.g. *21*...) because the '+' character inside an MMI string
    causes Android dialers to treat the whole string as a regular phone number
    and DIAL it instead of executing the MMI command.
    Example: '+919820806052' -> '919820806052'
    """
    return normalized.lstrip("+")


def _dial_mmi_and_dismiss(serial: str, mmi: str, wait_s: float = 3.5):
    """
    Dials an MMI code and then dismisses the dialer UI after a short wait
    so the phone screen is left clean and the radio modem is cleanly restored to IDLE.
    """
    start_call(serial, mmi)
    LOGGER.info("MMI sent: %s  (waiting %.1fs for network response)", mmi, wait_s)
    time.sleep(wait_s)
    # Dismiss the dialer / result dialog that Android opens after an MMI
    adb_cmd("shell", "input", "keyevent", "KEYCODE_BACK", serial=serial, timeout=3)
    time.sleep(0.3)
    adb_cmd("shell", "input", "keyevent", "KEYCODE_HOME", serial=serial, timeout=3)
    # Clear any lingering call/MMI audio session left by the dialer
    send_key(serial, "KEYCODE_ENDCALL")
    adb_cmd("shell", "cmd", "telecom", "cleanup-stuck-calls", serial=serial, timeout=2)
    # Give the modem a brief window to confirm IDLE state
    for _ in range(15):
        st = get_call_state(serial)
        if st == "IDLE":
            break
        time.sleep(0.2)


class CallDivertGuard:
    """
    Carrier-Level Call Divert Guard.
    On start: dials *21*<divert_number># to set unconditional call forwarding at
    the network level (no ringing on the device at all).
    On stop (manual or completion): automatically dials ##21# to cancel all diverts.
    The MMI codes are sent from a background thread so the UI never freezes.
    A threading.Event (_ready) is set once the divert MMI completes, so the
    bulk runner can wait for the divert to be confirmed before the first call.
    """
    def __init__(self, app):
        self.app = app
        self.active = False
        self.divert_number = ""
        self._ready = threading.Event()  # Set after divert MMI completes

    def start(self, divert_number: str):
        """Activate unconditional call divert to divert_number (runs in background)."""
        if not self.app.serial:
            self._ready.set()  # Don't block if no device
            return
        # Strip '+' so the MMI code is well-formed for all Android dialers
        safe_num = _mmi_safe_number(divert_number)
        self.divert_number = divert_number
        mmi = f"*21*{safe_num}#"
        self.app.putlog(f"📲 Setting carrier call divert: {mmi}  (forwarding to {divert_number})")
        self.app.putlog("⏳ Waiting for carrier to confirm divert before starting calls...")
        serial = self.app.serial
        self._ready.clear()

        def _do():
            try:
                _dial_mmi_and_dismiss(serial, mmi, wait_s=3.5)
                LOGGER.info("Call divert MMI complete: %s", mmi)
                self.app.putlog("✅ Carrier divert confirmed. Starting calls now.")
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Divert MMI error: %s", exc)
                self.app.putlog(f"⚠️ Divert MMI may not have completed: {exc}")
            finally:
                self._ready.set()

        threading.Thread(target=_do, daemon=True).start()
        self.active = True
        self.app.q.put(("guard_state", True))

    def wait_ready(self, timeout: float = 10.0):
        """Block until the divert MMI has completed (or timeout). Called from bulk thread."""
        self._ready.wait(timeout=timeout)

    def stop(self):
        """Cancel all call diverts via ##21# (runs in background)."""
        if not self.active:
            return
        self.active = False
        self._ready.set()  # Unblock any waiting thread
        with contextlib.suppress(Exception):
            self.app.q.put(("guard_state", False))
        if not self.app.serial:
            return
        serial = self.app.serial
        with contextlib.suppress(Exception):
            self.app.putlog("📲 Cancelling all call diverts (##21#)...")

        def _do():
            try:
                _dial_mmi_and_dismiss(serial, "##21#", wait_s=3.0)
                LOGGER.info("Call divert cancelled (##21#)")
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Cancel divert MMI error: %s", exc)

        threading.Thread(target=_do, daemon=True).start()

class App:
    def __init__(self):
        self.root = ctk.CTk() if CTK else tk.Tk()

        if CTK:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")

        self.root.title("ADB Device Alert & Bulk Call Console PRO")
        self.root.geometry("1280x920")
        self.root.minsize(1080, 760)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.q = queue.Queue()
        self.serial = None
        self.device_auth_state = "unknown"
        self.numbers = []
        self.current_call_active = False

        # Threading and control events
        self.stop_test = threading.Event()
        self.bulk_paused = threading.Event()
        self.test_thread = None
        self.bulk_thread = None

        # Carrier call divert guard
        self.call_guard = CallDivertGuard(self)

        self.build_ui()

        # Keyboard shortcuts
        self.root.bind("<F5>", lambda e: self.refresh())
        self.root.bind("<Control-O>", lambda e: self.import_file())
        self.root.bind("<Escape>", lambda e: self.stop_all())

        self.root.after(150, self.process_queue)
        threading.Thread(target=self.bootstrap_worker, daemon=True).start()

    def button(self, parent, text, command, **kw):
        if CTK:
            return ctk.CTkButton(parent, text=text, command=command, **kw)
        return ttk.Button(parent, text=text, command=command)

    def build_ui(self):
        if CTK:
            self.root.grid_columnconfigure(0, weight=1)
            self.root.grid_rowconfigure(1, weight=1)
            self.root.grid_rowconfigure(2, weight=0)

            # TOP HEADER
            head = ctk.CTkFrame(self.root, corner_radius=12)
            head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
            head.grid_columnconfigure(1, weight=1)

            title_box = ctk.CTkFrame(head, fg_color="transparent")
            title_box.grid(row=0, column=0, padx=16, pady=10, sticky="w")
            ctk.CTkLabel(
                title_box, text="ADB Alert & Bulk Call Console PRO",
                font=ctk.CTkFont(size=22, weight="bold")
            ).pack(anchor="w")

            self.tool_status = ctk.CTkLabel(
                title_box, text="Bootstrapping ADB & scrcpy…", text_color="gray70",
                font=ctk.CTkFont(size=12)
            )
            self.tool_status.pack(anchor="w")

            # Call divert guard indicator in header
            guard_box = ctk.CTkFrame(head, fg_color="transparent")
            guard_box.grid(row=0, column=1, padx=10, pady=10)
            self.guard_badge = ctk.CTkLabel(
                guard_box, text="Call Divert: ✅ OFF (Normal)",
                font=ctk.CTkFont(size=12, weight="bold"), text_color="#4ade80"
            )
            self.guard_badge.pack(side="left", padx=8)

            btn_box = ctk.CTkFrame(head, fg_color="transparent")
            btn_box.grid(row=0, column=2, padx=14, pady=10)
            self.device_status = ctk.CTkLabel(btn_box, text="0 device(s)", font=ctk.CTkFont(size=12))
            self.device_status.pack(side="left", padx=6)
            ctk.CTkButton(btn_box, text="Refresh (F5)", command=self.refresh, width=105).pack(side="left", padx=3)
            self.auth_prompt_btn = ctk.CTkButton(
                btn_box, text="Prompt Re-Auth", command=self.reconnect_action,
                width=125, fg_color="#b25d18", hover_color="#8f4a13"
            )
            self.auth_prompt_btn.pack(side="left", padx=3)

            # UNAUTHORIZED WARNING BANNER (Hidden by default)
            self.warn_banner = ctk.CTkFrame(self.root, fg_color="#7a1e1e", corner_radius=8)
            self.warn_label = ctk.CTkLabel(
                self.warn_banner,
                text="⚠️ DEVICE UNAUTHORIZED: Check phone screen, check 'Always allow from this computer' and tap 'Allow'.",
                font=ctk.CTkFont(weight="bold", size=13), text_color="#ffffff"
            )
            self.warn_label.pack(side="left", padx=16, pady=8)
            ctk.CTkButton(
                self.warn_banner, text="Click to Re-Prompt Phone",
                command=self.reconnect_action, width=160,
                fg_color="#a83232", hover_color="#822020"
            ).pack(side="right", padx=14, pady=6)

            # MAIN CONTENT GRID
            main = ctk.CTkFrame(self.root, corner_radius=12)
            main.grid(row=1, column=0, sticky="nsew", padx=14, pady=6)
            main.grid_columnconfigure(0, weight=5)
            main.grid_columnconfigure(1, weight=6)
            main.grid_rowconfigure(0, weight=1)

            # --- LEFT COLUMN ---
            left_col = ctk.CTkFrame(main, fg_color="transparent")
            left_col.grid(row=0, column=0, sticky="nsew", padx=(10, 6), pady=10)
            left_col.grid_rowconfigure(1, weight=1)
            left_col.grid_columnconfigure(0, weight=1)

            # Device Card
            dev_frame = ctk.CTkFrame(left_col, corner_radius=10)
            dev_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            ctk.CTkLabel(dev_frame, text="TARGET DEVICE", font=ctk.CTkFont(size=13, weight="bold")).pack(
                anchor="w", padx=12, pady=(8, 4)
            )
            self.device_var = tk.StringVar()
            self.device_combo = ctk.CTkComboBox(
                dev_frame, variable=self.device_var, values=[],
                command=lambda _: self.select_device(), height=32
            )
            self.device_combo.pack(fill="x", padx=12, pady=4)
            self.info = ctk.CTkLabel(dev_frame, text="No device selected", justify="left", font=ctk.CTkFont(family="Consolas", size=11))
            self.info.pack(anchor="w", padx=12, pady=4)

            act_row = ctk.CTkFrame(dev_frame, fg_color="transparent")
            act_row.pack(fill="x", padx=12, pady=(4, 6))
            self.screen_btn = ctk.CTkButton(act_row, text="Live Screen (scrcpy)", command=self.open_screen, height=36)
            self.screen_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
            self.reject_btn = ctk.CTkButton(
                act_row, text="Manual Reject / End", command=self.reject_call, height=36,
                fg_color="#8a2525", hover_color="#6e1d1d"
            )
            self.reject_btn.pack(side="left", fill="x", expand=True, padx=4)

            # Carrier call diversion helper row
            div_row = ctk.CTkFrame(dev_frame, fg_color="transparent")
            div_row.pack(fill="x", padx=12, pady=(0, 6))
            ctk.CTkButton(
                div_row, text="Manual Divert (*21*)", command=self.prompt_divert_calls,
                height=28, fg_color="#334155", hover_color="#1e293b", font=ctk.CTkFont(size=11)
            ).pack(side="left", fill="x", expand=True, padx=(0, 4))
            ctk.CTkButton(
                div_row, text="Cancel Divert (##21#)", command=self.cancel_divert_calls,
                height=28, fg_color="#334155", hover_color="#1e293b", font=ctk.CTkFont(size=11)
            ).pack(side="left", fill="x", expand=True, padx=4)

            self.state_badge = ctk.CTkLabel(
                dev_frame, text="Call state: IDLE", font=ctk.CTkFont(weight="bold", size=12),
                text_color="#4ade80"
            )
            self.state_badge.pack(anchor="w", padx=12, pady=(0, 6))

            # Numbers Card
            nums_frame = ctk.CTkFrame(left_col, corner_radius=10)
            nums_frame.grid(row=1, column=0, sticky="nsew")
            nums_frame.grid_rowconfigure(2, weight=1)
            nums_frame.grid_columnconfigure(0, weight=1)

            nums_top = ctk.CTkFrame(nums_frame, fg_color="transparent")
            nums_top.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))
            ctk.CTkLabel(nums_top, text="NUMBER QUEUE", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
            self.num_label = ctk.CTkLabel(nums_top, text="0 unique numbers", font=ctk.CTkFont(size=12, weight="bold"), text_color="cyan")
            self.num_label.pack(side="right")

            btn_nums = ctk.CTkFrame(nums_frame, fg_color="transparent")
            btn_nums.grid(row=1, column=0, sticky="ew", padx=10, pady=2)
            ctk.CTkButton(btn_nums, text="Import (TXT/CSV/VCF)", command=self.import_file, width=140, height=30).pack(side="left", padx=(0, 4))
            ctk.CTkButton(btn_nums, text="Sync from Box", command=self.sync_numbers_from_box, width=105, height=30).pack(side="left", padx=2)
            ctk.CTkButton(btn_nums, text="Clear", command=self.clear_numbers, width=65, height=30, fg_color=("gray65", "gray30")).pack(side="left", padx=2)

            self.number_box = ctk.CTkTextbox(nums_frame, font=ctk.CTkFont(family="Consolas", size=12))
            self.number_box.grid(row=2, column=0, sticky="nsew", padx=10, pady=(4, 8))

            # --- RIGHT COLUMN ---
            right_col = ctk.CTkFrame(main, corner_radius=10)
            right_col.grid(row=0, column=1, sticky="nsew", padx=(6, 10), pady=10)
            right_col.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(right_col, text="CALL SETTINGS & EXECUTION", font=ctk.CTkFont(size=14, weight="bold")).pack(
                anchor="w", padx=14, pady=(10, 6)
            )

            # Settings Grid
            self.dial_var = tk.StringVar(value=str(CONFIG["dial_timeout_seconds"]))
            self.call_dur_var = tk.StringVar(value=str(CONFIG["call_duration_seconds"]))
            self.post_var = tk.StringVar(value=str(CONFIG["post_call_delay_seconds"]))
            self.idle_var = tk.StringVar(value=str(CONFIG["wait_for_idle_seconds"]))
            self.call_vol_var = tk.StringVar(value=str(CONFIG["call_volume_percent"]))
            self.media_vol_var = tk.StringVar(value=str(CONFIG["media_volume_percent"]))
            self.country_code_var = tk.StringVar(value=str(CONFIG.get("country_code", "91")))
            self.speaker_var = tk.BooleanVar(value=bool(CONFIG["speaker_on"]))

            grid = ctk.CTkFrame(right_col, fg_color="transparent")
            grid.pack(fill="x", padx=14, pady=4)
            labels = [
                ("Dial timeout (s):", self.dial_var),
                ("Call duration (s):", self.call_dur_var),
                ("Delay after call (s):", self.post_var),
                ("Wait for idle (s):", self.idle_var),
                ("Call volume (%):", self.call_vol_var),
                ("Country code:", self.country_code_var),
            ]
            for i, (lbl, var) in enumerate(labels):
                rr = i // 2
                cc = (i % 2) * 2
                ctk.CTkLabel(grid, text=lbl, font=ctk.CTkFont(size=12)).grid(row=rr, column=cc, sticky="w", padx=(0, 4), pady=5)
                ctk.CTkEntry(grid, textvariable=var, width=80).grid(row=rr, column=cc + 1, sticky="w", padx=(0, 14), pady=5)

            opts_row = ctk.CTkFrame(right_col, fg_color="transparent")
            opts_row.pack(fill="x", padx=14, pady=4)

            ctk.CTkCheckBox(
                opts_row, text="Enable speakerphone",
                variable=self.speaker_var
            ).pack(side="left", padx=(0, 12))

            # SINGLE TEST CALL SECTION
            single_box = ctk.CTkFrame(right_col, fg_color=("gray85", "gray20"), corner_radius=8)
            single_box.pack(fill="x", padx=14, pady=6)
            ctk.CTkLabel(single_box, text="Single Test Call", font=ctk.CTkFont(weight="bold", size=12)).pack(anchor="w", padx=10, pady=(6, 2))

            callrow = ctk.CTkFrame(single_box, fg_color="transparent")
            callrow.pack(fill="x", padx=10, pady=(0, 8))
            self.test_number_var = tk.StringVar()
            ctk.CTkEntry(callrow, textvariable=self.test_number_var,
                         placeholder_text="Enter number (e.g. 9820806052)").pack(
                side="left", fill="x", expand=True, padx=(0, 8)
            )
            self.single_call_btn = ctk.CTkButton(
                callrow, text="TEST CALL", command=self.test_single_call, height=36, width=120
            )
            self.single_call_btn.pack(side="left")

            # BULK CALL RUNNER SECTION
            bulk_box = ctk.CTkFrame(right_col, fg_color=("gray85", "gray20"), corner_radius=8)
            bulk_box.pack(fill="x", padx=14, pady=6)
            ctk.CTkLabel(bulk_box, text="Bulk Call Runner", font=ctk.CTkFont(weight="bold", size=13)).pack(anchor="w", padx=10, pady=(8, 2))

            self.bulk_status_label = ctk.CTkLabel(
                bulk_box, text="Ready. Import or paste numbers to begin.",
                font=ctk.CTkFont(size=12, weight="bold"), text_color="cyan"
            )
            self.bulk_status_label.pack(anchor="w", padx=10, pady=2)

            self.bulk_progress = ctk.CTkProgressBar(bulk_box, height=14)
            self.bulk_progress.pack(fill="x", padx=10, pady=6)
            self.bulk_progress.set(0)

            bulk_actions = ctk.CTkFrame(bulk_box, fg_color="transparent")
            bulk_actions.pack(fill="x", padx=10, pady=(4, 10))

            self.start_bulk_btn = ctk.CTkButton(
                bulk_actions, text="START BULK CALLS", command=self.start_bulk_calls,
                height=38, fg_color="#1f7a42", hover_color="#186134"
            )
            self.start_bulk_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

            self.pause_bulk_btn = ctk.CTkButton(
                bulk_actions, text="Pause", command=self.toggle_pause_bulk,
                height=38, width=80, state="disabled"
            )
            self.pause_bulk_btn.pack(side="left", padx=4)

            self.stop_btn = ctk.CTkButton(
                bulk_actions, text="STOP ALL", command=self.stop_all,
                height=38, width=100, fg_color="#8a2525", hover_color="#6e1d1d", state="disabled"
            )
            self.stop_btn.pack(side="left", padx=(4, 0))

            ctk.CTkLabel(
                right_col,
                text="Shortcuts: F5 = Refresh Devices | Ctrl+O = Import | Esc = Stop All",
                text_color="gray60", font=ctk.CTkFont(size=11)
            ).pack(anchor="w", padx=14, pady=(6, 4))

            # LOGS PANEL AT BOTTOM
            logs = ctk.CTkFrame(self.root, corner_radius=12)
            logs.grid(row=2, column=0, sticky="ew", padx=14, pady=(6, 12))

            log_header = ctk.CTkFrame(logs, fg_color="transparent")
            log_header.pack(fill="x", padx=12, pady=(6, 2))
            ctk.CTkLabel(log_header, text="ACTIVITY LOG", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")

            btn_log_group = ctk.CTkFrame(log_header, fg_color="transparent")
            btn_log_group.pack(side="right")
            ctk.CTkButton(btn_log_group, text="Open Operations Log", command=lambda: self.open_log_file(OPERATIONS_LOG_FILE), width=130, height=24).pack(side="left", padx=2)
            ctk.CTkButton(btn_log_group, text="Open Error Log", command=lambda: self.open_log_file(ERROR_LOG_FILE), width=100, height=24, fg_color="#8a2525").pack(side="left", padx=2)
            ctk.CTkButton(btn_log_group, text="Open Logs Folder", command=lambda: os.startfile(str(LOG_DIR)), width=110, height=24, fg_color="#334155").pack(side="left", padx=2)
            ctk.CTkButton(btn_log_group, text="Clear Box", command=self.clear_log, width=70, height=24).pack(side="left", padx=2)

            self.logbox = ctk.CTkTextbox(logs, height=130, font=ctk.CTkFont(family="Consolas", size=11))
            self.logbox.pack(fill="both", expand=True, padx=12, pady=(2, 10))

        else:
            # Fallback Tkinter implementation
            self.root.columnconfigure(0, weight=1)
            self.root.rowconfigure(0, weight=1)
            frame = ttk.Frame(self.root, padding=10)
            frame.pack(fill="both", expand=True)

            self.tool_status = ttk.Label(frame, text="Bootstrapping ADB & scrcpy…")
            self.tool_status.pack(anchor="w", pady=2)
            self.guard_badge = ttk.Label(frame, text="Incoming Calls: ALLOWED")
            self.guard_badge.pack(anchor="w", pady=2)
            self.device_status = ttk.Label(frame, text="0 device(s) connected")
            self.device_status.pack(anchor="w", pady=2)

            self.device_var = tk.StringVar()
            self.device_combo = ttk.Combobox(frame, textvariable=self.device_var, state="readonly")
            self.device_combo.pack(fill="x", pady=4)
            self.device_combo.bind("<<ComboboxSelected>>", lambda e: self.select_device())

            self.info = ttk.Label(frame, text="No device selected")
            self.info.pack(anchor="w", pady=2)
            self.state_badge = ttk.Label(frame, text="Call state: IDLE")
            self.state_badge.pack(anchor="w", pady=2)

            row = ttk.Frame(frame)
            row.pack(fill="x", pady=4)
            self.screen_btn = ttk.Button(row, text="Open Live Screen", command=self.open_screen)
            self.screen_btn.pack(side="left", padx=2)
            self.reject_btn = ttk.Button(row, text="Manual Reject", command=self.reject_call)
            self.reject_btn.pack(side="left", padx=2)
            ttk.Button(row, text="Refresh", command=self.refresh).pack(side="left", padx=2)
            ttk.Button(row, text="Prompt Re-Auth", command=self.reconnect_action).pack(side="left", padx=2)

            self.dial_var = tk.StringVar(value=str(CONFIG["dial_timeout_seconds"]))
            self.call_dur_var = tk.StringVar(value=str(CONFIG["call_duration_seconds"]))
            self.post_var = tk.StringVar(value=str(CONFIG["post_call_delay_seconds"]))
            self.idle_var = tk.StringVar(value=str(CONFIG["wait_for_idle_seconds"]))
            self.call_vol_var = tk.StringVar(value=str(CONFIG["call_volume_percent"]))
            self.media_vol_var = tk.StringVar(value=str(CONFIG["media_volume_percent"]))
            self.country_code_var = tk.StringVar(value=str(CONFIG.get("country_code", "91")))
            self.speaker_var = tk.BooleanVar(value=bool(CONFIG["speaker_on"]))

            self.test_number_var = tk.StringVar()
            ttk.Entry(frame, textvariable=self.test_number_var).pack(fill="x", pady=2)
            self.single_call_btn = ttk.Button(frame, text="TEST CALL", command=self.test_single_call)
            self.single_call_btn.pack(anchor="w", pady=2)

            self.bulk_status_label = ttk.Label(frame, text="Ready")
            self.bulk_status_label.pack(anchor="w", pady=2)
            self.bulk_progress = ttk.Progressbar(frame)
            self.bulk_progress.pack(fill="x", pady=2)

            brow = ttk.Frame(frame)
            brow.pack(fill="x", pady=4)
            self.start_bulk_btn = ttk.Button(brow, text="START BULK CALLS", command=self.start_bulk_calls)
            self.start_bulk_btn.pack(side="left", padx=2)
            self.pause_bulk_btn = ttk.Button(brow, text="Pause", command=self.toggle_pause_bulk, state="disabled")
            self.pause_bulk_btn.pack(side="left", padx=2)
            self.stop_btn = ttk.Button(brow, text="STOP ALL", command=self.stop_all, state="disabled")
            self.stop_btn.pack(side="left", padx=2)

            self.num_label = ttk.Label(frame, text="0 unique numbers")
            self.num_label.pack(anchor="w", pady=2)
            self.number_box = tk.Text(frame, height=6)
            self.number_box.pack(fill="x", pady=2)

            self.logbox = tk.Text(frame, height=10)
            self.logbox.pack(fill="both", expand=True, pady=4)

    def putlog(self, text):
        LOGGER.info(text)
        self.q.put(("log", text))

    def clear_log(self):
        self.logbox.delete("1.0", "end")

    def open_log_file(self, path):
        try:
            if not path.exists():
                path.write_text("", encoding="utf-8")
            os.startfile(str(path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Log Error", f"Could not open {path.name}: {exc}")

    def bootstrap_worker(self):
        self.q.put(("status", "Checking ADB & scrcpy…"))
        a, s = bootstrap_tools(lambda msg: self.q.put(("status", msg)))
        self.q.put(("tools", (a, s)))
        self.refresh()

    def refresh(self):
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self):
        ds = adb_devices()
        self.q.put(("devices", ds))

    def reconnect_action(self):
        self.putlog("Triggering ADB reconnect to prompt device authorization...")
        threading.Thread(target=self._reconnect_worker, daemon=True).start()

    def _reconnect_worker(self):
        reconnect_device(self.serial)
        time.sleep(1)
        self._refresh()

    def select_device(self):
        v = self.device_var.get().strip()
        if not v:
            self.serial = None
            self.device_auth_state = "none"
            self.update_auth_banner()
            return

        parts = v.split(" | ", 1)
        self.serial = parts[0]
        self.device_auth_state = parts[1] if len(parts) > 1 else "device"
        self.update_auth_banner()

        if self.serial and self.device_auth_state != "unauthorized":
            threading.Thread(target=self._fetch_info, daemon=True).start()
        elif self.device_auth_state == "unauthorized":
            self.info.configure(text=f"Device {self.serial} is UNAUTHORIZED.\nPlease check phone screen and accept USB Debugging.")

    def update_auth_banner(self):
        if CTK and hasattr(self, "warn_banner"):
            if self.device_auth_state == "unauthorized":
                self.warn_banner.grid(row=0, column=0, sticky="ew", padx=14, pady=(0, 6))
            else:
                self.warn_banner.grid_forget()

    def _fetch_info(self):
        if self.serial:
            self.q.put(("info", get_device_info(self.serial)))

    def prompt_divert_number(self, title="Call Divert") -> str | None:
        """
        Shows a dialog asking for the divert-to phone number.
        Returns the normalized number (e.g. '+919820806052'), or None if cancelled.
        The '+' is stripped by _mmi_safe_number() when the MMI code is actually built.
        """
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("440x200")
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.transient(self.root)
        dialog.focus_force()

        # Use a neutral dark bg that works whether CTK is active or not
        bg = "#1e2433" if CTK else "SystemButtonFace"
        fg = "white" if CTK else "SystemWindowText"
        dialog.configure(bg=bg)

        tk.Label(
            dialog,
            text="Enter the number to forward ALL incoming calls to:",
            font=("Segoe UI", 11, "bold"),
            bg=bg, fg=fg, justify="center",
        ).pack(pady=(20, 2))

        tk.Label(
            dialog,
            text="Carrier sets *21*<number># divert — no incoming ring on device.",
            font=("Segoe UI", 9),
            bg=bg, fg="#94a3b8" if CTK else fg, justify="center",
        ).pack(pady=(0, 8))

        # Pre-fill last used number (show it without the '+' for clarity)
        last = CONFIG.get("last_divert_number", "")
        entry_var = tk.StringVar(value=last)
        entry = tk.Entry(
            dialog, textvariable=entry_var,
            font=("Segoe UI", 14), width=22, justify="center",
            bg="#2d3748" if CTK else "white",
            fg="white" if CTK else "black",
            insertbackground="white" if CTK else "black",
            relief="flat", bd=4,
        )
        entry.pack(pady=4, ipady=4)
        entry.focus_set()
        entry.icursor("end")

        result = {"value": None}

        def on_ok(event=None):
            val = entry_var.get().strip()
            n = normalize_number(val)
            if not n or n.startswith(("*", "#")):
                messagebox.showerror(
                    "Invalid Number",
                    f"Please enter a valid phone number (digits only, e.g. 9820806052).\nReceived: {val!r}",
                    parent=dialog,
                )
                return
            result["value"] = n
            dialog.destroy()

        def on_skip(event=None):
            result["value"] = ""
            dialog.destroy()

        def on_cancel(event=None):
            dialog.destroy()

        btn_row = tk.Frame(dialog, bg=bg)
        btn_row.pack(pady=10)
        tk.Button(
            btn_row, text="  Set Divert & Start  ", command=on_ok,
            font=("Segoe UI", 11, "bold"),
            bg="#1f7a42", fg="white", activebackground="#186134", activeforeground="white",
            relief="flat", padx=10, pady=5,
        ).pack(side="left", padx=5)
        tk.Button(
            btn_row, text="  Skip Divert (Direct)  ", command=on_skip,
            font=("Segoe UI", 11),
            bg="#334155", fg="white", activebackground="#1e293b", activeforeground="white",
            relief="flat", padx=8, pady=5,
        ).pack(side="left", padx=5)
        tk.Button(
            btn_row, text="  Cancel  ", command=on_cancel,
            font=("Segoe UI", 11),
            bg="#4b5563" if CTK else "SystemButtonFace",
            fg="white" if CTK else "SystemWindowText",
            relief="flat", padx=10, pady=5,
        ).pack(side="left", padx=5)

        dialog.bind("<Return>", on_ok)
        dialog.bind("<Escape>", on_cancel)
        self.root.wait_window(dialog)
        return result["value"]

    def prompt_divert_calls(self):
        """Manually set call divert via the Manual Divert (*21*) button."""
        if not self.serial:
            messagebox.showerror("Device", "Select an ADB device first.")
            return
        num = self.prompt_divert_number("Manual Call Divert")
        if num:
            safe_num = _mmi_safe_number(num)
            mmi = f"*21*{safe_num}#"
            self.putlog(f"Setting carrier unconditional call divert: {mmi}")
            serial = self.serial
            threading.Thread(
                target=lambda: _dial_mmi_and_dismiss(serial, mmi, wait_s=3.5),
                daemon=True,
            ).start()
            # Save last used number
            CONFIG["last_divert_number"] = num
            CONFIG_FILE.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")

    def cancel_divert_calls(self):
        if not self.serial:
            messagebox.showerror("Device", "Select an ADB device first.")
            return
        self.putlog("Cancelling all carrier call diverts (##21#)...")
        serial = self.serial
        threading.Thread(
            target=lambda: _dial_mmi_and_dismiss(serial, "##21#", wait_s=3.0),
            daemon=True,
        ).start()

    def import_file(self):
        fp = filedialog.askopenfilename(
            title="Import numbers",
            filetypes=[
                ("Supported files", "*.txt;*.csv;*.vcf"),
                ("Text files (*.txt)", "*.txt"),
                ("CSV files (*.csv)", "*.csv"),
                ("VCard files (*.vcf)", "*.vcf"),
            ]
        )
        if not fp:
            return
        try:
            self.numbers = import_numbers(fp)
            self.num_label.configure(text=f"{len(self.numbers)} unique numbers")
            self.number_box.delete("1.0", "end")
            self.number_box.insert("1.0", "\n".join(self.numbers))
            self.putlog(f"Imported {len(self.numbers)} numbers from {Path(fp).name}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Import error", str(exc))

    def sync_numbers_from_box(self):
        text = self.number_box.get("1.0", "end").strip()
        self.numbers = parse_numbers_from_text(text)
        self.num_label.configure(text=f"{len(self.numbers)} unique numbers")
        # Reformat neatly
        self.number_box.delete("1.0", "end")
        self.number_box.insert("1.0", "\n".join(self.numbers))
        self.putlog(f"Queue updated: {len(self.numbers)} valid numbers parsed from box.")

    def clear_numbers(self):
        self.numbers = []
        self.num_label.configure(text="0 unique numbers")
        self.number_box.delete("1.0", "end")
        self.putlog("Numbers queue cleared.")

    def persist_call_config(self):
        try:
            dial_s = max(1.0, float(self.dial_var.get()))
            cfg = {
                "dial_timeout_seconds": dial_s,
                "call_duration_seconds": max(0.5, float(self.call_dur_var.get())),
                "post_call_delay_seconds": max(0.0, float(self.post_var.get())),
                "wait_for_idle_seconds": max(1.0, float(self.idle_var.get())),
                "country_code": self.country_code_var.get().strip().lstrip("+") or "91",
                "speaker_on": bool(self.speaker_var.get()),
                "call_volume_percent": max(0, min(100, int(float(self.call_vol_var.get())))),
                "media_volume_percent": max(0, min(100, int(float(self.media_vol_var.get())))),
            }
            CONFIG.update(cfg)
            CONFIG_FILE.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
            return True
        except (ValueError, TypeError, OSError):
            messagebox.showerror("Settings", "Please enter valid numeric settings.")
            return False

    def open_screen(self):
        if not self.serial:
            messagebox.showerror("Device", "Select an ADB device first.")
            return
        if self.device_auth_state == "unauthorized":
            messagebox.showwarning("Device Unauthorized", "Authorize USB debugging on your phone first.")
            return
        ok, msg = launch_scrcpy(self.serial)
        self.putlog(msg if ok else f"scrcpy ERROR: {msg}")
        if not ok:
            messagebox.showerror("scrcpy", msg)

    def reject_call(self):
        if not self.serial:
            messagebox.showerror("Device", "Select a device first.")
            return
        ok, msg = reject_current_call(self.serial)
        self.putlog(f"Reject command: {'OK' if ok else 'FAILED'} | {msg}")

    def wait_for_idle(self, timeout, auto_recover=True):
        end = time.monotonic() + timeout
        tried_recovery = False
        while time.monotonic() < end and not self.stop_test.is_set():
            if not self.serial:
                return False
            _rc, out, _ = adb_cmd("shell", "dumpsys", "telephony.registry", serial=self.serial, timeout=3)
            state = parse_call_state_text(out)
            self.q.put(("state", state))
            if state == "IDLE":
                return True

            # If an incoming call is ringing while waiting for idle, reject it immediately
            if state == "RINGING" and CONFIG.get("auto_reject_incoming_calls", True):
                self.putlog("🛡️ Incoming call ringing while waiting for idle — auto-rejecting to preserve queue...")
                reject_current_call(self.serial)

            # Halfway through timeout and not idle: trigger active recovery
            if auto_recover and not tried_recovery and (time.monotonic() > end - (timeout / 2.0)):
                tried_recovery = True
                self.putlog(f"Modem busy ({state}) while waiting for idle — attempting auto-recovery...")
                reject_current_call(self.serial)
                send_key(self.serial, "KEYCODE_HOME")

            time.sleep(0.2)
        return False

    def execute_call_cycle(self, number, call_idx=1, total_calls=1):
        """
        Robust call execution with initiation grace period and precise alerting detection.
        Incoming calls are handled at the carrier level via call divert set before run starts.
        """
        self.current_call_active = True
        status = "UNKNOWN"
        message = ""
        call_start = time.monotonic()

        try:
            # 1. Wait for device to be IDLE before dialing
            if not self.wait_for_idle(CONFIG.get("wait_for_idle_seconds", 2.0)):
                self.putlog("Radio modem not IDLE — performing aggressive cleanup before dialing...")
                reject_current_call(self.serial)
                send_key(self.serial, "KEYCODE_HOME")
                time.sleep(0.4)
                if not self.wait_for_idle(3.0, auto_recover=False):
                    raise RuntimeError("Device radio modem did not clear to IDLE after auto-recovery")

            # 2. Apply requested audio settings
            if CONFIG.get("speaker_on", True):
                ok, msg = set_speaker(self.serial, True)
                self.putlog(f"Speakerphone: {'OK' if ok else 'BEST-EFFORT'} | {msg}")

            set_volume(self.serial, 0, CONFIG.get("call_volume_percent", 80))

            # 3. Start call intent
            self.putlog(f"[{call_idx}/{total_calls}] Dialing → {number}")
            ok, msg = start_call(self.serial, number)
            if not ok:
                raise RuntimeError(f"am start CALL failed: {msg}")

            call_start = time.monotonic()
            dialing_deadline = call_start + CONFIG["dial_timeout_seconds"]
            initiation_deadline = call_start + min(8.0, max(4.0, float(CONFIG.get("dial_timeout_seconds", 15.0))))
            has_initiated = False
            connected = False

            while time.monotonic() < dialing_deadline and not self.stop_test.is_set():
                _rc, out, _ = adb_cmd("shell", "dumpsys", "telephony.registry", serial=self.serial, timeout=3)
                state = parse_call_state_text(out)
                self.q.put(("state", state))

                # Handle incoming call interrupt during dialing
                if state == "RINGING" and CONFIG.get("auto_reject_incoming_calls", True):
                    self.putlog(f"🛡️ Incoming call ringing during dialing of {number} — auto-rejecting incoming call...")
                    reject_current_call(self.serial)
                    time.sleep(0.3)
                    continue

                if state in ("ACTIVE", "OFFHOOK"):
                    has_initiated = True
                    connected = True
                    break
                elif state in ("DIALING", "ALERTING"):
                    has_initiated = True
                elif state == "IDLE":
                    if has_initiated:
                        status = "BUSY_OR_DROPPED"
                        message = "Call dropped or busy before answer"
                        break
                    elif time.monotonic() > initiation_deadline:
                        status = "NOT_STARTED"
                        message = "Dialer failed to initiate call"
                        break

                time.sleep(0.25)

            if self.stop_test.is_set():
                reject_current_call(self.serial)
                status = "STOPPED"
                message = "Stopped by user"
            elif status == "UNKNOWN":
                if not connected:
                    reject_current_call(self.serial)
                    status = "DIAL_TIMEOUT"
                    message = "Call did not connect within dialing timeout"
                else:
                    self.putlog(f"[{call_idx}/{total_calls}] Connected! Holding for {CONFIG['call_duration_seconds']}s")
                    hold_end = time.monotonic() + CONFIG["call_duration_seconds"]
                    while time.monotonic() < hold_end and not self.stop_test.is_set():
                        st = get_call_state(self.serial)
                        self.q.put(("state", st))
                        if st == "IDLE":
                            # Remote party hung up
                            break
                        time.sleep(0.2)

                    reject_current_call(self.serial)
                    status = "COMPLETED" if not self.stop_test.is_set() else "STOPPED"
                    message = "Call duration elapsed" if status == "COMPLETED" else "Stopped by user"

            elapsed = round(time.monotonic() - call_start, 2)
            result = CallTestResult(
                datetime.now().isoformat(timespec="seconds"),
                self.serial, number,
                CONFIG["dial_timeout_seconds"],
                CONFIG["call_duration_seconds"],
                CONFIG["post_call_delay_seconds"],
                bool(CONFIG["speaker_on"]),
                int(CONFIG["call_volume_percent"]),
                status,
                f"{message}; elapsed={elapsed}s"
            )
            write_result(result)
            self.putlog(f"RESULT [{call_idx}/{total_calls}]: {status} | {number} | {message} ({elapsed}s)")

            # Wait for IDLE before proceeding
            self.wait_for_idle(CONFIG["wait_for_idle_seconds"])

            # Post-call delay
            if CONFIG["post_call_delay_seconds"] > 0 and not self.stop_test.is_set():
                self.putlog(f"Post-call cooldown: {CONFIG['post_call_delay_seconds']}s")
                end_cd = time.monotonic() + CONFIG["post_call_delay_seconds"]
                while time.monotonic() < end_cd and not self.stop_test.is_set():
                    time.sleep(0.2)

            return status, message, elapsed

        except Exception as exc:
            LOGGER.exception("Call cycle failed on %s", number)
            self.putlog(f"CALL ERROR on {number}: {exc}")
            reject_current_call(self.serial)
            return "ERROR", str(exc), round(time.monotonic() - call_start, 2)
        finally:
            self.current_call_active = False

    def test_single_call(self):
        if (self.test_thread and self.test_thread.is_alive()) or (self.bulk_thread and self.bulk_thread.is_alive()):
            return

        if not self.serial:
            messagebox.showerror("Device", "Select an ADB device first.")
            return

        if self.device_auth_state == "unauthorized":
            messagebox.showwarning("Device Unauthorized", "Authorize USB debugging on your phone first.")
            return

        raw_num = self.test_number_var.get().strip()
        n = normalize_number(raw_num)
        if not n:
            messagebox.showerror("Invalid Number", f"Please enter a valid phone number.\nReceived: {raw_num}")
            return

        if not self.persist_call_config():
            return

        # Ask for call divert number before starting
        divert_num = self.prompt_divert_number("Set Call Divert Before Single Call")
        if divert_num is None:
            return  # User cancelled

        self.stop_test.clear()
        self.single_call_btn.configure(state="disabled")
        self.start_bulk_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        if divert_num:
            # Save last used divert number
            CONFIG["last_divert_number"] = divert_num
            CONFIG_FILE.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
            # Activate carrier call divert, wait for it before starting
            self.call_guard.start(divert_num)
        else:
            self.putlog("Call divert skipped — placing direct single call.")

        def worker():
            if divert_num:
                # Wait for divert MMI to complete (max 10s) before placing the call
                self.call_guard.wait_ready(timeout=10.0)
            try:
                self.execute_call_cycle(n, 1, 1)
            finally:
                if divert_num:
                    self.call_guard.stop()
                self.q.put(("single_done", None))

        self.test_thread = threading.Thread(target=worker, daemon=True)
        self.test_thread.start()

    def start_bulk_calls(self):
        if (self.test_thread and self.test_thread.is_alive()) or (self.bulk_thread and self.bulk_thread.is_alive()):
            return

        if not self.serial:
            messagebox.showerror("Device", "Select an ADB device first.")
            return

        if self.device_auth_state == "unauthorized":
            messagebox.showwarning("Device Unauthorized", "Authorize USB debugging on your phone first.")
            return

        # Sync from textbox and deduplicate
        self.sync_numbers_from_box()

        if not self.numbers:
            messagebox.showwarning("Empty Queue", "No numbers to call. Please import a file or paste numbers in the box.")
            return

        if not self.persist_call_config():
            return

        # Ask for call divert number before starting
        divert_num = self.prompt_divert_number("Set Call Divert Before Bulk Calls")
        if divert_num is None:
            return  # User cancelled

        self.stop_test.clear()
        self.bulk_paused.clear()

        self.single_call_btn.configure(state="disabled")
        self.start_bulk_btn.configure(state="disabled")
        self.pause_bulk_btn.configure(state="normal", text="Pause")
        self.stop_btn.configure(state="normal")

        if divert_num:
            # Save last used divert number
            CONFIG["last_divert_number"] = divert_num
            CONFIG_FILE.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
            # Activate carrier-level call divert for entire bulk run
            self.call_guard.start(divert_num)
        else:
            self.putlog("Call divert skipped — placing direct bulk calls.")

        def bulk_worker():
            total = len(self.numbers)
            completed_count = 0
            self.putlog(f"=== STARTING BULK CALLS: {total} Numbers (duplicates removed) ===")

            if divert_num:
                # Wait for divert MMI to complete before first call (max 10s)
                self.call_guard.wait_ready(timeout=10.0)

            try:
                for idx, num in enumerate(self.numbers, 1):
                    if self.stop_test.is_set():
                        self.putlog("Bulk run interrupted by user STOP.")
                        break

                    while self.bulk_paused.is_set() and not self.stop_test.is_set():
                        time.sleep(0.4)

                    if self.stop_test.is_set():
                        break

                    self.q.put(("bulk_progress", (idx, total, num, "CALLING")))
                    status, _msg, _el = self.execute_call_cycle(num, idx, total)
                    self.q.put(("bulk_progress", (idx, total, num, status)))

                    if status == "COMPLETED":
                        completed_count += 1
            finally:
                if divert_num:
                    # Cancel all call diverts immediately on completion or stop
                    self.call_guard.stop()

            self.putlog(f"=== BULK CALLS FINISHED: {completed_count}/{total} completed ===")
            self.q.put(("bulk_done", None))

        self.bulk_thread = threading.Thread(target=bulk_worker, daemon=True)
        self.bulk_thread.start()

    def toggle_pause_bulk(self):
        if not self.bulk_thread or not self.bulk_thread.is_alive():
            return
        if self.bulk_paused.is_set():
            self.bulk_paused.clear()
            self.pause_bulk_btn.configure(text="Pause")
            self.putlog("Bulk calls RESUMED.")
        else:
            self.bulk_paused.set()
            self.pause_bulk_btn.configure(text="Resume")
            self.putlog("Bulk calls PAUSED. (Current call will finish, then wait)")

    def stop_all(self):
        self.stop_test.set()
        self.bulk_paused.clear()
        # Reject any active outgoing call immediately (fast, keyevent only)
        if self.serial:
            send_key(self.serial, "KEYCODE_ENDCALL")
            adb_cmd("shell", "cmd", "telecom", "cleanup-stuck-calls", serial=self.serial, timeout=2)
        # Cancel call divert in background if active
        if self.call_guard.active:
            threading.Thread(target=self.call_guard.stop, daemon=True).start()
        self.putlog("STOP requested — ending active call and cancelling any active divert.")

    def process_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()

                if kind == "status":
                    self.tool_status.configure(text=payload)

                elif kind == "tools":
                    a, s = payload
                    self.tool_status.configure(
                        text=f"ADB: {'READY' if a else 'FAILED'}   |   scrcpy: {'READY' if s else 'FAILED'}"
                    )
                    self.putlog(f"Tool check → ADB={a}, scrcpy={s}")

                elif kind == "devices":
                    vals = [f"{s} | {st}" for s, st in payload]
                    self.device_status.configure(text=f"{len(vals)} device(s) connected")
                    self.device_combo.configure(values=vals)

                    current = self.device_var.get().strip()
                    matching = [v for v in vals if current and current.split(" | ")[0] in v]

                    if matching:
                        self.device_combo.set(matching[0])
                        self.select_device()
                    elif vals:
                        self.device_combo.set(vals[0])
                        self.select_device()
                    else:
                        self.device_var.set("")
                        self.select_device()

                    self.putlog(f"Device scan found {len(vals)} device(s)")

                elif kind == "info":
                    self.info.configure(text=payload)

                elif kind == "state":
                    state = str(payload)
                    color = "#4ade80" if state == "IDLE" else ("#f59e0b" if state in ("DIALING", "ALERTING") else "#38bdf8")
                    with contextlib.suppress(Exception):
                        self.state_badge.configure(text=f"Call state: {state}", text_color=color)

                elif kind == "guard_state":
                    is_active = bool(payload)
                    divert_num = CONFIG.get("last_divert_number", "")
                    if is_active:
                        self.guard_badge.configure(
                            text=f"Call Divert: 📲 ACTIVE → {divert_num}",
                            text_color="#f59e0b"
                        )
                    else:
                        self.guard_badge.configure(
                            text="Call Divert: ✅ OFF (Normal)",
                            text_color="#4ade80"
                        )

                elif kind == "log":
                    stamp = datetime.now().strftime("%H:%M:%S")
                    self.logbox.insert("end", f"[{stamp}] {payload}\n")
                    self.logbox.see("end")

                elif kind == "bulk_progress":
                    idx, total, num, status = payload
                    pct = idx / max(1, total)
                    self.bulk_progress.set(pct)
                    self.bulk_status_label.configure(
                        text=f"Call {idx} of {total} ({int(pct*100)}%): {num} [{status}]"
                    )

                elif kind == "single_done":
                    self.single_call_btn.configure(state="normal")
                    self.start_bulk_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")

                elif kind == "bulk_done":
                    self.single_call_btn.configure(state="normal")
                    self.start_bulk_btn.configure(state="normal")
                    self.pause_bulk_btn.configure(state="disabled", text="Pause")
                    self.stop_btn.configure(state="disabled")
                    self.bulk_status_label.configure(text="Bulk run complete.")

        except queue.Empty:
            pass

        self.root.after(150, self.process_queue)

    def close(self):
        self.stop_test.set()
        self.bulk_paused.clear()
        # End any active call immediately
        if self.serial:
            with contextlib.suppress(Exception):
                send_key(self.serial, "KEYCODE_ENDCALL")
        # Cancel divert in background if active, give it 1s then destroy
        if self.call_guard.active:
            t = threading.Thread(target=self.call_guard.stop, daemon=True)
            t.start()
            with contextlib.suppress(Exception):
                t.join(timeout=1.0)  # Brief wait so the MMI dispatch fires
        self.root.destroy()

if __name__ == "__main__":
    App().root.mainloop()
