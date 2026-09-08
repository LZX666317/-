"""Withdrawn-Tk GUI contracts using a mocked player, with no real playback."""

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import podcast_gui
from podcast_player import AudioPlayer, PlaybackError, PlaybackSnapshot


class PodcastGuiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory(prefix="podcast-gui-test-")
        self.addCleanup(self.tempdir.cleanup)
        root_dir = Path(self.tempdir.name)
        self.preview = root_dir / "preview-british-gentle.mp3"
        self.existing = root_dir / "already-generated.mp3"
        self.preview.write_bytes(b"preview-audio-bytes")
        self.existing.write_bytes(b"existing-audio-bytes")

        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

        self.player = Mock(spec=AudioPlayer)
        self.player.path = None
        self.player.rate = 1.0
        self.player.snapshot.return_value = PlaybackSnapshot()

        for item in (
            patch.object(podcast_gui.PodcastApp, "_find_latest_output", return_value=None),
            patch.object(podcast_gui.PodcastApp, "_find_latest_preview", return_value=None),
            patch.object(podcast_gui.PodcastApp, "_load_script", return_value=None),
            patch.object(podcast_gui, "AudioPlayer", return_value=self.player),
        ):
            item.start()
            self.addCleanup(item.stop)

        self.app = podcast_gui.PodcastApp(self.root)

    def _destroy_root(self):
        try:
            if self.root.winfo_exists():
                for timer in self.root.tk.call("after", "info"):
                    self.root.after_cancel(timer)
                self.root.destroy()
        except tk.TclError:
            pass

    def _assert_root_destroyed(self):
        try:
            exists = self.root.winfo_exists()
        except tk.TclError:
            # Some Tcl builds remove winfo with the application's interpreter.
            exists = 0
        self.assertEqual(exists, 0)

    def _play_sets_current_path(self, target):
        self.player.path = Path(target).resolve()

    def test_choose_existing_then_pause_and_resume_stays_on_selected_file(self):
        self.app.preview_output = self.preview
        self.app.last_output = self.preview
        self.player.play.side_effect = self._play_sets_current_path

        with patch.object(
            podcast_gui.filedialog, "askopenfilename", return_value=str(self.existing)
        ):
            self.app.choose_audio()

        self.assertEqual(self.player.play.call_args.args[0], self.existing)
        self.assertEqual(self.player.path, self.existing.resolve())
        self.assertEqual(self.app.playback_target, self.existing.resolve())
        self.assertEqual(self.app.playback_file_var.get(), self.existing.name)

        self.player.snapshot.side_effect = [
            PlaybackSnapshot("playing", 4.0, 20.0),
            PlaybackSnapshot("paused", 4.0, 20.0),
        ]
        self.app.play_preview()
        self.player.pause.assert_called_once_with()
        self.assertEqual(self.player.play.call_count, 1)
        self.app.play_preview()
        self.assertEqual(self.player.play.call_count, 2)
        self.assertEqual(self.player.play.call_args.args[0], self.existing.resolve())
        self.assertEqual(self.app.preview_output, self.preview)

    def test_playback_rate_is_independent_from_generation_rate(self):
        self.app.rate_var.set(8)
        # Scale writes its linked variable before invoking its callback.
        self.app.playback_rate_var.set(1.35)
        self.app._on_playback_rate_change("1.35")

        self.player.set_rate.assert_called_once_with(1.35)
        self.assertEqual(self.app.rate_var.get(), 8)
        self.assertEqual(self.app.playback_rate_var.get(), 1.35)
        self.assertEqual(self.app.playback_rate_text.get(), "1.35×")

    def test_american_warm_profile_uses_approved_slow_unpitched_defaults(self):
        self.assertEqual(podcast_gui.AMERICAN_REFERENCE_VOICE, "美式风2x")
        self.assertIn("美式风2x", podcast_gui.VOICES)
        self.app.rate_var.set(12)
        self.app.pitch_var.set(8)
        self.app.voice_var.set(podcast_gui.AMERICAN_REFERENCE_VOICE)

        self.app._apply_voice_defaults()

        self.assertEqual(self.app.rate_var.get(), -10)
        self.assertEqual(self.app.rate_text.get(), "-10%")
        self.assertEqual(self.app.pitch_var.get(), 0)
        self.assertEqual(self.app.pitch_text.get(), "+0 Hz")

    def test_playback_rate_can_be_preset_without_audio(self):
        self.assertIsNone(self.player.path)
        original_generation_rate = self.app.rate_var.get()
        self.app.playback_rate_var.set(0.5)
        self.app._on_playback_rate_change("0.50")

        self.player.set_rate.assert_called_once_with(0.5)
        self.player.play.assert_not_called()
        self.assertEqual(self.app.playback_rate_var.get(), 0.5)
        self.assertEqual(self.app.playback_rate_text.get(), "0.50×")
        self.assertEqual(self.app.rate_var.get(), original_generation_rate)

    def test_canceling_file_picker_preserves_selection_and_does_not_play(self):
        self.app.playback_target = self.existing.resolve()
        self.app.playback_file_var.set(self.existing.name)
        self.app.playback_status_var.set("已暂停")

        with patch.object(podcast_gui.filedialog, "askopenfilename", return_value=""):
            self.app.choose_audio()

        self.player.play.assert_not_called()
        self.assertEqual(self.app.playback_target, self.existing.resolve())
        self.assertEqual(self.app.playback_file_var.get(), self.existing.name)
        self.assertEqual(self.app.playback_status_var.get(), "已暂停")

    def test_save_copies_original_preview_without_baking_playback_rate(self):
        output_dir = Path(self.tempdir.name) / "audio-output"
        self.app.preview_output = self.preview
        self.app.preview_saved = False
        self.app.preview_style = ""
        self.app.playback_rate_var.set(1.75)
        self.player.rate = 1.75
        original_hash = hashlib.sha256(self.preview.read_bytes()).digest()
        original_mtime = self.preview.stat().st_mtime_ns

        with patch.object(podcast_gui, "OUTPUT_DIR", output_dir), patch.object(
            podcast_gui.subprocess, "Popen"
        ) as popen, patch.object(podcast_gui.messagebox, "showinfo"), patch.object(
            podcast_gui.subprocess, "run"
        ) as run:
            self.app.save_preview()

        destination = self.app.last_output
        self.assertIsNotNone(destination)
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.read_bytes(), self.preview.read_bytes())
        self.assertEqual(hashlib.sha256(self.preview.read_bytes()).digest(), original_hash)
        self.assertEqual(self.preview.stat().st_mtime_ns, original_mtime)
        self.assertTrue(self.app.preview_saved)
        self.player.set_rate.assert_not_called()
        run.assert_not_called()
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args.args[0][0], "explorer.exe")

    def test_save_american_preview_uses_2x_filename(self):
        output_dir = Path(self.tempdir.name) / "audio-output"
        self.app.preview_output = self.preview
        self.app.preview_saved = False
        self.app.preview_style = podcast_gui.AMERICAN_REFERENCE_VOICE

        with patch.object(podcast_gui, "OUTPUT_DIR", output_dir), patch.object(
            podcast_gui.subprocess, "Popen"
        ), patch.object(podcast_gui.messagebox, "showinfo"):
            self.app.save_preview()

        self.assertIsNotNone(self.app.last_output)
        self.assertTrue(self.app.last_output.name.startswith("英语播客-美式风2x-"))

    def test_async_rate_failure_restores_slider_and_label_to_player_rate(self):
        self.app.playback_rate_var.set(1.75)
        self.app.playback_rate_text.set("1.75× · 调节中")
        self.app.playback_status_var.set("播放中")
        self.player.rate = 1.25
        self.player.path = self.existing.resolve()
        self.player.snapshot.side_effect = PlaybackError("调速未能应用，已恢复")

        with patch.object(self.root, "after", return_value="test-poll"):
            self.app._poll_playback()

        self.assertEqual(self.app.playback_rate_var.get(), 1.25)
        self.assertEqual(self.app.playback_rate_text.get(), "1.25×")
        self.assertEqual(self.app.playback_status_var.get(), "播放中")
        self.assertIn("调速未能应用", self.app.status_var.get())
        self.assertTrue(self.app._playback_error_shown)
        self.player.play.assert_not_called()

    def test_close_releases_player_cancels_poll_and_destroys_root(self):
        self.app._on_close()

        self.player.close.assert_called_once_with()
        self.assertIsNone(self.app._playback_poll_id)
        self.assertTrue(self.app._closing)
        self._assert_root_destroyed()

    def test_close_twice_and_late_poll_are_safe(self):
        self.app._on_close()
        self.app._on_close()
        self.app._poll_playback()

        self.player.close.assert_called_once_with()
        self.player.snapshot.assert_not_called()
        self._assert_root_destroyed()

    def test_layout_width_and_rate_control_are_readable(self):
        self.root.update_idletasks()

        self.assertGreaterEqual(self.root.winfo_width(), 700)
        self.assertGreaterEqual(self.root.winfo_width(), self.root.winfo_reqwidth())
        self.assertGreaterEqual(self.app.playback_scale.winfo_reqwidth(), 100)
        self.assertEqual(float(self.app.playback_scale.cget("from")), 0.5)
        self.assertEqual(float(self.app.playback_scale.cget("to")), 2.0)
        self.assertEqual(float(self.app.playback_scale.cget("resolution")), 0.05)
        self.assertEqual(self.app.playback_rate_text.get(), "1.00×")


if __name__ == "__main__":
    unittest.main()
