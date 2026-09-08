"""Unit tests for the local reference-voice render command."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import xtts_generate


class XttsOutputProfileTests(unittest.TestCase):
    def test_warm_profile_preserves_pitch_and_uses_no_extra_eq(self):
        with TemporaryDirectory(prefix="xtts-output-test-") as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            output = root / "output.mp3"

            with patch.object(xtts_generate, "_find_ffmpeg", return_value="ffmpeg"), patch.object(
                xtts_generate.subprocess, "run"
            ) as run:
                run.return_value.returncode = 0
                run.return_value.stderr = ""
                run.return_value.stdout = ""
                xtts_generate._convert_to_mp3(
                    source,
                    output,
                    pitch_hz=0,
                    warm_magnetic=True,
                )

        command = run.call_args.args[0]
        filters = command[command.index("-af") + 1]
        self.assertNotIn("rubberband=pitch", filters)
        self.assertIn("acompressor=threshold=0.055:ratio=1.30", filters)
        self.assertTrue(filters.endswith("loudnorm=I=-24:TP=-5:LRA=5"))
        # The warm profile adds dynamics only; the established colour EQ stays
        # exactly once and no new bass/treble/equalizer stage is introduced.
        self.assertEqual(filters.count("equalizer="), 1)
        self.assertEqual(filters.count("treble="), 2)

    def test_default_profile_does_not_apply_warm_compression(self):
        with TemporaryDirectory(prefix="xtts-output-test-") as temp_dir:
            root = Path(temp_dir)
            with patch.object(xtts_generate, "_find_ffmpeg", return_value="ffmpeg"), patch.object(
                xtts_generate.subprocess, "run"
            ) as run:
                run.return_value.returncode = 0
                run.return_value.stderr = ""
                run.return_value.stdout = ""
                xtts_generate._convert_to_mp3(root / "source.wav", root / "output.mp3", 0)

        command = run.call_args.args[0]
        filters = command[command.index("-af") + 1]
        self.assertNotIn("acompressor=", filters)
        self.assertTrue(filters.endswith("loudnorm=I=-23.5:TP=-3.2:LRA=12"))


if __name__ == "__main__":
    unittest.main()
