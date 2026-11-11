"""Per-device notification sound settings and playback for MFU Media Hub."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
)


APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "MFU" / "MFU Media Hub"
SETTINGS_PATH = APP_DIR / "notification_sound.json"
SUPPORTED_SUFFIXES = {".wav", ".mp3"}
SOUND_COOLDOWN_SECONDS = 2.0


def default_sound_settings() -> dict[str, Any]:
    return {"enabled": False, "file_path": "", "volume": 50}


def normalize_sound_settings(values: object) -> dict[str, Any]:
    result = default_sound_settings()
    if not isinstance(values, dict):
        return result
    result["enabled"] = values.get("enabled") is True
    result["file_path"] = str(values.get("file_path") or "").strip()
    try:
        result["volume"] = min(100, max(0, int(values.get("volume", 50))))
    except (TypeError, ValueError):
        pass
    return result


def load_sound_settings() -> dict[str, Any]:
    try:
        return normalize_sound_settings(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return default_sound_settings()


def save_sound_settings(values: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(normalize_sound_settings(values), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)


def validate_sound_file(path: str) -> str:
    if not path:
        return "通知音ファイルを選択してください。"
    candidate = Path(path).expanduser()
    if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
        return "WAVまたはMP3ファイルを選択してください。"
    try:
        exists = candidate.is_file()
    except (OSError, ValueError):
        exists = False
    if not exists:
        return "通知音ファイルが見つかりません。場所を確認してください。"
    return ""


class NotificationSoundPlayer(QObject):
    error_changed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = None
        self._audio_output = None
        self._last_played = 0.0
        self._last_logged_error = ""
        self.backend_error = ""
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            self._audio_output = QAudioOutput(self)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.errorOccurred.connect(self._on_playback_error)
        except (ImportError, OSError, RuntimeError) as exc:
            self.backend_error = f"音声再生機能を利用できません: {exc}"
            logging.warning("notification sound backend unavailable: %s", exc)

    def _on_playback_error(self, _error: object, message: str) -> None:
        self._report_error(f"通知音を再生できません: {message}")

    def _report_error(self, message: str) -> None:
        if message != self._last_logged_error:
            logging.warning("notification sound: %s", message)
            self._last_logged_error = message
        self.error_changed.emit(message)

    def play(self, path: str, volume: int, *, preview: bool = False) -> bool:
        if self.backend_error:
            self._report_error(self.backend_error)
            return False
        problem = validate_sound_file(path)
        if problem:
            self._report_error(problem)
            return False
        now = time.monotonic()
        if not preview and now - self._last_played < SOUND_COOLDOWN_SECONDS:
            return False
        assert self._player is not None and self._audio_output is not None
        self._player.stop()
        self._audio_output.setVolume(min(100, max(0, volume)) / 100)
        self._player.setSource(QUrl.fromLocalFile(str(Path(path).expanduser().resolve())))
        self._player.play()
        if not preview:
            self._last_played = now
        self._last_logged_error = ""
        return True


class NotificationSoundSettingsDialog(QDialog):
    def __init__(self, settings: dict[str, Any], player: NotificationSoundPlayer, parent=None) -> None:
        super().__init__(parent)
        self.player = player
        self.setWindowTitle("MFU Media Hub 通知音設定")
        layout = QVBoxLayout(self)

        self.enabled = QCheckBox("通知センターの新着ポップアップで音を鳴らす")
        self.enabled.setChecked(bool(settings.get("enabled")))
        layout.addWidget(self.enabled)

        form = QFormLayout()
        file_row = QHBoxLayout()
        self.file_path = QLineEdit(str(settings.get("file_path") or ""))
        self.file_path.setReadOnly(True)
        choose = QPushButton("選択")
        choose.clicked.connect(self._choose_file)
        clear = QPushButton("解除")
        clear.clicked.connect(lambda: self.file_path.clear())
        file_row.addWidget(self.file_path)
        file_row.addWidget(choose)
        file_row.addWidget(clear)
        form.addRow("通知音（WAV / MP3）", file_row)

        volume_row = QHBoxLayout()
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(settings.get("volume", 50)))
        self.volume_value = QLabel(f"{self.volume.value()}%")
        self.volume.valueChanged.connect(lambda value: self.volume_value.setText(f"{value}%"))
        volume_row.addWidget(self.volume)
        volume_row.addWidget(self.volume_value)
        form.addRow("音量", volume_row)
        layout.addLayout(form)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.file_path.textChanged.connect(self._update_status)
        self.enabled.toggled.connect(self._update_status)
        self.player.error_changed.connect(self._show_error)
        self._update_status()

        buttons = QHBoxLayout()
        preview = QPushButton("試聴")
        preview.clicked.connect(self._preview)
        cancel = QPushButton("キャンセル")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.clicked.connect(self._save)
        buttons.addWidget(preview)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "通知音を選択", self.file_path.text(), "音声ファイル (*.wav *.mp3);;すべてのファイル (*)"
        )
        if path:
            self.file_path.setText(path)

    def _update_status(self) -> None:
        problem = validate_sound_file(self.file_path.text())
        if self.player.backend_error:
            problem = self.player.backend_error
        self.status.setText(problem if self.enabled.isChecked() or self.file_path.text() else "")

    def _show_error(self, message: str) -> None:
        self.status.setText(message)

    def _preview(self) -> None:
        if self.player.play(self.file_path.text(), self.volume.value(), preview=True):
            self.status.setText("試聴中です。")

    def _save(self) -> None:
        if self.enabled.isChecked():
            problem = self.player.backend_error or validate_sound_file(self.file_path.text())
            if problem:
                self.status.setText(problem)
                return
        self.accept()

    def result_values(self) -> dict[str, Any]:
        return normalize_sound_settings({
            "enabled": self.enabled.isChecked(),
            "file_path": self.file_path.text(),
            "volume": self.volume.value(),
        })
