from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import podcast_player


class FakePythonCom:
    def __init__(self) -> None:
        self.pumps = 0
        self.uninitialized = 0

    def PumpWaitingMessages(self) -> None:
        self.pumps += 1

    def CoUninitialize(self) -> None:
        self.uninitialized += 1


class FakeSettings:
    def __init__(self, available: bool = True, clamp: float | None = None) -> None:
        self.autoStart = False
        self.enableErrorDialogs = False
        self.volume = 80
        self._rate = 1.0
        self.available = available
        self.clamp = clamp

    def setMode(self, _name: str, _value: bool) -> None:
        pass

    def isAvailable(self, name: str) -> bool:
        return name == "Rate" and self.available

    @property
    def rate(self) -> float:
        return self._rate

    @rate.setter
    def rate(self, value: float) -> None:
        self._rate = self.clamp if self.clamp is not None else value


class FakeErrorItem:
    errorDescription = "fake decode error"


class FakeError:
    def __init__(self) -> None:
        self.errorCount = 0

    def item(self, _index: int) -> FakeErrorItem:
        return FakeErrorItem()

    def clearErrorQueue(self) -> None:
        self.errorCount = 0


class FakeMedia:
    duration = 120.0


class FakeControls:
    def __init__(self, owner: "FakePlayer") -> None:
        self.owner = owner
        self.currentPosition = 0.0

    def play(self) -> None:
        self.owner.playState = 3

    def pause(self) -> None:
        self.owner.playState = 2

    def stop(self) -> None:
        self.owner.playState = 1


class FakePlayer:
    def __init__(self, settings: FakeSettings | None = None) -> None:
        self.settings = settings or FakeSettings()
        self.error = FakeError()
        self.controls = FakeControls(self)
        self.currentMedia = FakeMedia()
        self.openState = 0
        self.playState = 0
        self.closed = 0
        self._url = ""

    @property
    def URL(self) -> str:
        return self._url

    @URL.setter
    def URL(self, value: str) -> None:
        self._url = value
        self.openState = 13

    def close(self) -> None:
        self.closed += 1
        self.playState = 1


class AudioPlayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "voice.mp3"
        self.path.write_bytes(b"fake")
        self.com = FakePythonCom()
        self.wmp = FakePlayer()
        self.factory = patch(
            "podcast_player._create_windows_player",
            return_value=(self.wmp, self.com),
        )
        self.platform = patch.object(podcast_player.sys, "platform", "win32")
        self.factory.start()
        self.platform.start()

    def tearDown(self) -> None:
        self.platform.stop()
        self.factory.stop()
        self.tmp.cleanup()

    def test_playback_rate_is_applied_when_media_becomes_ready(self) -> None:
        player = podcast_player.AudioPlayer()
        player.set_rate(1.75)
        player.play(self.path)
        self.wmp.controls.currentPosition = 12.5
        snapshot = player.snapshot()
        self.assertEqual(snapshot.state, "playing")
        self.assertEqual(self.wmp.settings.rate, 1.75)
        self.assertEqual(self.wmp.controls.currentPosition, 12.5)
        self.assertEqual(self.wmp.URL, str(self.path.resolve()))

    def test_hot_rate_change_does_not_reload_or_seek(self) -> None:
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.snapshot()
        self.wmp.controls.currentPosition = 37.25
        player.set_rate(0.65)
        self.assertEqual(self.wmp.settings.rate, 0.65)
        self.assertEqual(self.wmp.controls.currentPosition, 37.25)
        self.assertEqual(self.wmp.URL, str(self.path.resolve()))

    def test_rate_range_is_strict(self) -> None:
        player = podcast_player.AudioPlayer()
        for value in (0.49, 2.01, float("nan"), float("inf")):
            with self.assertRaises(podcast_player.PlaybackError):
                player.set_rate(value)

    def test_unsupported_rate_rolls_back_and_keeps_player_alive(self) -> None:
        self.wmp.settings.available = False
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.snapshot()
        closed_before = self.wmp.closed
        with self.assertRaises(podcast_player.PlaybackError):
            player.set_rate(1.5)
        self.assertEqual(player.rate, 1.0)
        self.assertEqual(player.path, self.path.resolve())
        self.assertEqual(self.wmp.closed, closed_before)

    def test_player_rate_readback_mismatch_rolls_back(self) -> None:
        self.wmp.settings.clamp = 1.0
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.snapshot()
        player.set_rate(1.5)
        self.assertEqual(player.rate, 1.5)
        player._rate_check_deadline = time.monotonic() - 1
        with self.assertRaises(podcast_player.PlaybackError):
            player.snapshot()
        self.assertEqual(player.rate, 1.0)
        self.assertEqual(self.wmp.settings.rate, 1.0)

    def test_stop_then_replay_keeps_selected_rate(self) -> None:
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.snapshot()
        player.set_rate(1.5)
        player.stop()
        player.play(self.path)
        player.snapshot()
        self.assertEqual(self.wmp.settings.rate, 1.5)

    def test_missing_file_is_rejected_without_initializing_player(self) -> None:
        player = podcast_player.AudioPlayer()
        with self.assertRaises(podcast_player.PlaybackError):
            player.play(Path(self.tmp.name) / "missing.mp3")
        self.assertIsNone(player._player)

    def test_com_error_releases_player_for_same_path_retry(self) -> None:
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        self.wmp.error.errorCount = 1
        with self.assertRaises(podcast_player.PlaybackError):
            player.snapshot()
        self.assertIsNone(player.path)
        self.assertGreaterEqual(self.wmp.closed, 1)

    def test_close_is_idempotent_and_uninitializes_com(self) -> None:
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.close()
        player.close()
        self.assertEqual(self.com.uninitialized, 1)

    def test_delayed_rate_acknowledgement_does_not_fail_or_reload(self) -> None:
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.snapshot()
        closed_before = self.wmp.closed
        self.wmp.settings.clamp = 1.0
        player.set_rate(0.5)
        self.assertTrue(player.snapshot().rate_pending)
        self.assertEqual(player.rate, 0.5)
        self.wmp.settings.clamp = None
        self.wmp.settings.rate = 0.5  # Simulate the asynchronous acknowledgement.
        self.assertFalse(player.snapshot().rate_pending)
        self.assertEqual(self.wmp.closed, closed_before)

    def test_last_rate_wins_when_slider_changes_rapidly(self) -> None:
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.snapshot()
        self.wmp.settings.clamp = 1.0
        for rate in (0.5, 1.5, 0.8, 2.0, 1.25):
            player.set_rate(rate)
        self.wmp.settings.clamp = None
        self.wmp.settings.rate = 1.25
        self.assertFalse(player.snapshot().rate_pending)
        self.assertEqual(player.rate, 1.25)

    def test_loading_does_not_apply_rate_until_ready(self) -> None:
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        self.wmp.openState = 21
        self.wmp.playState = 9
        player.set_rate(1.5)
        self.assertEqual(player.snapshot().state, "loading")
        self.assertEqual(self.wmp.settings.rate, 1.0)
        self.wmp.openState = 13
        self.wmp.playState = 3
        self.assertEqual(player.snapshot().state, "playing")
        self.assertEqual(self.wmp.settings.rate, 1.5)

    def test_loading_timeout_closes_media(self) -> None:
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        self.wmp.playState = 9
        player._deadline = time.monotonic() - 1
        with self.assertRaises(podcast_player.PlaybackError):
            player.snapshot()
        self.assertIsNone(player.path)
        self.assertIsNone(player._player)
        self.assertEqual(self.com.uninitialized, 1)

    def test_stop_during_loading_cancels_delayed_start(self) -> None:
        player = podcast_player.AudioPlayer()
        player.set_rate(1.75)
        player.play(self.path)
        self.wmp.playState = 9
        player.stop()
        self.assertIsNone(player.path)
        self.assertEqual(player.snapshot().state, "idle")
        self.assertEqual(player.rate, 1.75)

    def test_pause_resume_does_not_seek_or_replace_file(self) -> None:
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.snapshot()
        self.wmp.controls.currentPosition = 30.0
        player.pause()
        player.set_rate(1.5)
        self.assertEqual(player.snapshot().state, "paused")
        closed_before = self.wmp.closed
        player.play(self.path)
        self.assertEqual(player.snapshot().position, 30.0)
        self.assertEqual(self.wmp.closed, closed_before)

    def test_switching_files_keeps_rate_and_releases_previous_file(self) -> None:
        other = Path(self.tmp.name) / "另一段音频.mp3"
        other.write_bytes(b"fake")
        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.snapshot()
        player.set_rate(1.5)
        closed_before = self.wmp.closed
        player.play(other)
        player.snapshot()
        self.assertEqual(self.wmp.closed, closed_before + 1)
        self.assertEqual(player.path, other.resolve())
        self.assertEqual(self.wmp.settings.rate, 1.5)

    def test_invalid_type_is_reported_as_playback_error(self) -> None:
        player = podcast_player.AudioPlayer()
        for value in (None, "invalid", [], -1, 0):
            with self.assertRaises(podcast_player.PlaybackError):
                player.set_rate(value)

    def test_state_read_failure_resets_rate_and_releases_player(self) -> None:
        from unittest.mock import PropertyMock

        player = podcast_player.AudioPlayer()
        player.play(self.path)
        player.snapshot()
        with patch.object(FakePlayer, "openState", new_callable=PropertyMock, create=True) as state:
            state.side_effect = RuntimeError("COM failed")
            with self.assertRaises(podcast_player.PlaybackError):
                player.set_rate(1.75)
        self.assertEqual(player.rate, 1.0)
        self.assertIsNone(player.path)


if __name__ == "__main__":
    unittest.main()
