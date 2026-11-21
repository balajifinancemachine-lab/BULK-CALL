# ADB Device Alert & Bulk Call Console PRO

A high-performance, robust Android device automation and bulk calling console engineered for telecommunication testing, automated dialer verification, and multi-SIM device quality assurance via Android Debug Bridge (ADB) and scrcpy.

---

## 🌟 Key Features

- **Carrier-Level Call Divert Guard**:
  - Dials network MMI codes (`*21*<number>#`) on start to unconditionally forward all incoming calls at the carrier tower level.
  - Automatically cancels diverts (`##21#`) on completion or stop.
  - Dedicated **"Skip Divert (Direct)"** mode for testing on dedicated SIMs without call forwarding.
  - RFC 3986 URI encoding (`%23`) for `#` ensures full MMI string execution without Android shell comment truncation.
- **Auto-Recovery & Stuck Call Resolution**:
  - Automatically diagnoses lingering modem states and stuck telecom sessions.
  - Auto-rejects incoming calls when waiting for IDLE or during dialer sequence to prevent queue interruption.
  - Solves the `Device radio modem did not clear to IDLE` exception with proactive cleanup (`telecom cleanup-stuck-calls`, `KEYCODE_ENDCALL`, `KEYCODE_HOME`).
- **Multi-SIM & Modern Android Telemetry**:
  - Full telemetry extraction across Android 10/11/12/13/14/15/16.
  - Multi-SIM support (`Phone Id=0`, `Phone Id=1`) ensuring accurate `DIALING`, `ALERTING`, `ACTIVE`, `HOLDING`, `DISCONNECTING`, and `IDLE` state detection without slot collision.
- **Zero-Setup Bootstrapping**:
  - Automatically detects, downloads, and configures Android Platform-Tools (`adb`) and `scrcpy` if not already present on the host system.
- **Customizable Dialing Cycles**:
  - Granular control over dial timeout, connected call duration, post-call delay, wait-for-idle delay, and speakerphone/volume levels.
  - Real-time progress bar, queue pause/resume, and instant stop controls.
- **Multi-Tier Structured Logging**:
  - `logs/operations.log`: Operational events, queue execution, call completions.
  - `logs/error.log`: Warnings and exceptions with full stack traces.
  - `logs/terminal.log`: Complete verbatim mirror of console streams with Windows UTF-8 safety.
  - `logs/scrcpy/`: Individual session logs for screen mirroring.

---

## 📋 Prerequisites

- **Python 3.10+**
- **Android Device** with USB Debugging enabled.
- **CustomTkinter** GUI library:
  ```bash
  pip install -r requirements.txt
  ```

---

## 🚀 Quick Start

1. **Clone the repository**:
   ```bash
   git clone https://github.com/balajifinancemachine-lab/BULK-CALL.git
   cd BULK-CALL
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Launch the Console**:
   ```bash
   python adb_alert_call_manager_pro.pyw
   ```
   *(Or double-click `adb_alert_call_manager_pro.pyw` on Windows)*

4. **Connect Device**:
   - Plug in your Android device via USB.
   - Accept the USB Debugging authorization prompt on your phone screen.
   - Click **Refresh (F5)** to select the device.

---

## ⚙️ Configuration (`config.json`)

The application automatically creates and manages `config.json`:

```json
{
  "platform_tools_url_windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
  "scrcpy_url_windows": "https://github.com/Genymobile/scrcpy/releases/latest/download/scrcpy-win64-v3.3.3.zip",
  "auto_download_tools": true,
  "scrcpy_fullscreen": false,
  "scrcpy_always_on_top": false,
  "scrcpy_show_touches": false,
  "country_code": "91",
  "adb_timeout": 10,
  "last_divert_number": "",
  "dial_timeout_seconds": 15.0,
  "call_duration_seconds": 4.0,
  "post_call_delay_seconds": 1.0,
  "wait_for_idle_seconds": 2.0,
  "speaker_on": true,
  "call_volume_percent": 80,
  "media_volume_percent": 60,
  "auto_reject_incoming_calls": true
}
```

---

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| `F5` | Refresh ADB connected devices |
| `Ctrl + O` | Import numbers file (.txt, .csv, .vcf) |
| `Esc` | Instant STOP all calls and cancel diverts |

---

## 🧪 Testing

Run the automated test suite:
```bash
python test_suite.py
```

---

## 🛡️ License

MIT License. See LICENSE for details.
