from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import serial.tools.list_ports
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget

PRIMARY_CONFIG_URL = "https://raw.githubusercontent.com/terrafirma2021/MAKCM_v2_files/main/config.json"
FALLBACK_CONFIG_URL = "https://gitee.com/terrafirma/MAKCM_v2_files/raw/main/config.json"

# Optional manual override. If not set, URLs are read from remote config.json.
LEFT_BIN_URL_OVERRIDE = os.getenv("MAKCU_LEFT_BIN_URL", "").strip()
RIGHT_BIN_URL_OVERRIDE = os.getenv("MAKCU_RIGHT_BIN_URL", "").strip()

DOWNLOAD_DIR = Path("firmware")
BAUDRATE = "921600"
CHIP = "esp32s3"
FLASH_OFFSET = "0x0"

# Default mapping used by many MAKCU flashing flows.
# Override with env vars if your left/right mapping is different.
LEFT_FLASH_PID = int(os.getenv("MAKCU_LEFT_FLASH_PID", "0x0009"), 16)
RIGHT_FLASH_PID = int(os.getenv("MAKCU_RIGHT_FLASH_PID", "0x1001"), 16)
ESP_FLASH_VID = int(os.getenv("MAKCU_FLASH_VID", "0x303A"), 16)
FLASH_PIDS = {LEFT_FLASH_PID, RIGHT_FLASH_PID}


@dataclass
class DeviceInfo:
    port: str
    vid: int
    pid: int
    hwid: str


class FlashWorker(QObject):
    status = pyqtSignal(str)
    done = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self.status.emit("開始監控裝置狀態...")
        while not self._stop_event.is_set():
            try:
                self.status.emit(self._get_current_status_text())
            except Exception as exc:
                self.status.emit(f"狀態讀取失敗: {exc}")
            time.sleep(0.6)
        self.done.emit(True)

    def _validate_urls(self) -> None:
        return

    def _resolve_firmware_urls(self) -> tuple[str, str]:
        if LEFT_BIN_URL_OVERRIDE and RIGHT_BIN_URL_OVERRIDE:
            self.status.emit("使用環境變數中的 left/right 韌體 URL。")
            return LEFT_BIN_URL_OVERRIDE, RIGHT_BIN_URL_OVERRIDE

        config = self._fetch_remote_config()
        firmware = config.get("firmware", {}) if isinstance(config, dict) else {}
        left = firmware.get("left", {}) if isinstance(firmware, dict) else {}
        right = firmware.get("right", {}) if isinstance(firmware, dict) else {}

        left_url = str(left.get("primary_url") or "").strip()
        right_url = str(right.get("primary_url") or "").strip()

        if LEFT_BIN_URL_OVERRIDE:
            left_url = LEFT_BIN_URL_OVERRIDE
        if RIGHT_BIN_URL_OVERRIDE:
            right_url = RIGHT_BIN_URL_OVERRIDE

        if not left_url or not right_url:
            raise ValueError("無法從 MAKCU config.json 取得 left/right 韌體下載網址")

        self.status.emit("已從 MAKCU 公開 config.json 取得韌體 URL。")
        return left_url, right_url

    def _fetch_remote_config(self) -> dict:
        urls = [PRIMARY_CONFIG_URL, FALLBACK_CONFIG_URL]
        last_error: Exception | None = None
        for url in urls:
            try:
                self.status.emit(f"下載設定檔: {url}")
                response = requests.get(url, timeout=15)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                continue
        raise RuntimeError(f"取得 MAKCU config.json 失敗: {last_error}")

    def _wait_for_flash_device(
        self,
        expected: str,
        phase_label: str,
        exclude_signature: str | None = None,
    ) -> DeviceInfo:
        last_snapshot = ""
        last_emit_time = 0.0
        while not self._stop_event.is_set():
            found: list[DeviceInfo] = []
            for port in serial.tools.list_ports.comports():
                vid = port.vid
                pid = port.pid
                if vid is None or pid is None:
                    continue
                if vid != ESP_FLASH_VID:
                    continue
                if pid not in FLASH_PIDS:
                    continue
                found.append(DeviceInfo(port=port.device, vid=vid, pid=pid, hwid=port.hwid))

            signatures = [f"{d.port}:{d.vid:04X}:{d.pid:04X}" for d in found]
            snapshot = ", ".join(signatures) if signatures else "none"
            now = time.time()
            if snapshot != last_snapshot or now - last_emit_time >= 1.5:
                self.status.emit(f"{phase_label} | 掃描中... 找到 Flash 裝置: {snapshot}")
                last_snapshot = snapshot
                last_emit_time = now

            for dev in found:
                sig = f"{dev.port}:{dev.vid:04X}:{dev.pid:04X}"
                if exclude_signature and sig == exclude_signature:
                    continue
                if not self._probe_flash_port(dev.port):
                    continue
                return dev
            time.sleep(0.5)

        raise RuntimeError("使用者中止")

    def _get_current_status_text(self) -> str:
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            return "狀態: 沒連接"

        flash_devices: list[DeviceInfo] = []
        normal_devices: list[DeviceInfo] = []
        for port in ports:
            vid = port.vid
            pid = port.pid
            if vid is None or pid is None:
                continue
            dev = DeviceInfo(port=port.device, vid=vid, pid=pid, hwid=port.hwid)
            if vid == ESP_FLASH_VID and pid in FLASH_PIDS:
                flash_devices.append(dev)
            if (vid == 0x1A86 and pid == 0x55D3) or (vid == 0x0403 and pid == 0x6001):
                normal_devices.append(dev)

        if flash_devices:
            # Match your requested state buckets.
            for dev in flash_devices:
                if dev.pid == LEFT_FLASH_PID:
                    return f"狀態: Left Flash mode ({dev.port}, VID:PID={dev.vid:04X}:{dev.pid:04X})"
            for dev in flash_devices:
                if dev.pid == RIGHT_FLASH_PID:
                    return f"狀態: Right Flash mode ({dev.port}, VID:PID={dev.vid:04X}:{dev.pid:04X})"
            dev = flash_devices[0]
            return f"狀態: Flash mode ({dev.port}, VID:PID={dev.vid:04X}:{dev.pid:04X})"

        if normal_devices:
            dev = normal_devices[0]
            return f"狀態: Normal mode ({dev.port}, VID:PID={dev.vid:04X}:{dev.pid:04X})"

        return "狀態: 沒連接"

    def _download_bin(self, url: str, target: Path) -> Path:
        self.status.emit(f"下載中: {url}")
        with requests.get(url, stream=True, timeout=30) as response:
            response.raise_for_status()
            with target.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 64):
                    if self._stop_event.is_set():
                        raise RuntimeError("使用者中止")
                    if chunk:
                        f.write(chunk)
        self.status.emit(f"下載完成: {target}")
        return target

    def _flash_bin(self, port: str, bin_path: Path) -> None:
        cmd = [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            CHIP,
            "--port",
            port,
            "--baud",
            BAUDRATE,
            "write_flash",
            FLASH_OFFSET,
            str(bin_path),
        ]

        self.status.emit(f"開始燒錄: {' '.join(cmd)}")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )

        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            if "Writing at" in line or "Hash of data verified" in line or "Hard resetting" in line:
                self.status.emit(line)

        code = process.wait()
        if code != 0:
            raise RuntimeError(f"esptool 失敗，exit code={code}")

    def _probe_flash_port(self, port: str) -> bool:
        cmd = [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            CHIP,
            "--port",
            port,
            "--baud",
            BAUDRATE,
            "chip_id",
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=12,
            )
            output = proc.stdout or ""
            if proc.returncode == 0 and ("Chip is" in output or "Detected" in output):
                return True
            return False
        except Exception:
            return False


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MAKCU Flash Tool")
        self.resize(720, 220)

        self.label = QLabel("準備開始...", self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.label.setWordWrap(True)
        self.label.setMargin(16)

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.addWidget(self.label)
        self.setCentralWidget(container)

        self.worker = FlashWorker()
        self.worker.status.connect(self.on_status)
        self.worker.done.connect(self.on_done)

        self.thread = threading.Thread(target=self.worker.run, daemon=True)
        self.thread.start()

    def on_status(self, text: str) -> None:
        self.label.setText(text)

    def on_done(self, ok: bool) -> None:
        if ok:
            self.label.setText("燒錄流程完成。")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.worker.stop()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
