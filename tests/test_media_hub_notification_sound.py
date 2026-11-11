from __future__ import annotations

import os
import wave
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from tools.mfu_media_hub import notification_sound as sound


def test_sound_settings_are_local_and_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr(sound, "SETTINGS_PATH", tmp_path / "notification_sound.json")
    assert sound.load_sound_settings() == {"enabled": False, "file_path": "", "volume": 50}

    sound.save_sound_settings({"enabled": True, "file_path": " C:/sounds/new.mp3 ", "volume": 150})
    assert sound.load_sound_settings() == {"enabled": True, "file_path": "C:/sounds/new.mp3", "volume": 100}
    assert not (tmp_path / "notification_sound.tmp").exists()

    sound.SETTINGS_PATH.write_text("{invalid", encoding="utf-8")
    assert sound.load_sound_settings()["enabled"] is False


def test_wav_and_mp3_files_are_selectable(tmp_path):
    wav = tmp_path / "chime.wav"
    with wave.open(str(wav), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 80)
    mp3 = tmp_path / "chime.mp3"
    mp3.write_bytes(b"test")

    assert sound.validate_sound_file(str(wav)) == ""
    assert sound.validate_sound_file(str(mp3)) == ""
    assert "WAVまたはMP3" in sound.validate_sound_file(str(tmp_path / "chime.txt"))
    assert "見つかりません" in sound.validate_sound_file(str(tmp_path / "gone.mp3"))


def test_settings_dialog_updates_volume_and_previews_without_saving(tmp_path):
    class FakePlayer:
        backend_error = ""

        def __init__(self):
            from PySide6.QtCore import QObject, Signal

            class Emitter(QObject):
                error_changed = Signal(str)

            self.emitter = Emitter()
            self.error_changed = self.emitter.error_changed
            self.calls = []

        def play(self, path, volume, *, preview=False):
            self.calls.append((path, volume, preview))
            return True

    QApplication.instance() or QApplication([])
    audio_file = tmp_path / "notice.mp3"
    audio_file.write_bytes(b"test")
    player = FakePlayer()
    dialog = sound.NotificationSoundSettingsDialog(sound.default_sound_settings(), player)
    dialog.file_path.setText(str(audio_file))
    dialog.volume.setValue(73)
    dialog.enabled.setChecked(True)
    dialog._preview()
    assert player.calls == [(str(audio_file), 73, True)]
    dialog._save()
    assert dialog.result_values() == {"enabled": True, "file_path": str(audio_file), "volume": 73}


def test_only_realtime_popup_path_plays_sound():
    source = (Path(__file__).resolve().parents[1] / "tools/mfu_media_hub/main.py").read_text(encoding="utf-8")
    popup_route = source.split("def _show_notification_popup(self, item: dict) -> None:", 1)[1].split(
        "def _remove_popup", 1
    )[0]
    photo_route = source.split("def _photo_saved(self, path: str) -> None:", 1)[1].split(
        "@staticmethod", 1
    )[0]
    assert "self.notification_sound_player.play(" in popup_route
    assert "self.manual_url_dialog_open" in popup_route
    assert "notification_sound_player" not in photo_route
