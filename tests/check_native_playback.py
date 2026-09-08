"""Opt-in, muted Windows integration check; never regenerates the input.

Run from the project root:
  .venv\Scripts\python.exe tests/check_native_playback.py path/to/audio.mp3
Use --gui to open an isolated, muted UI for manual acceptance testing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from podcast_player import AudioPlayer


def run_check(path: Path) -> None:
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    player = AudioPlayer()

    def wait_for(condition, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = player.snapshot()
            if condition(snapshot):
                return snapshot
            time.sleep(0.02)
        raise AssertionError(f"Playback did not settle: {snapshot}")

    def advance(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            player.snapshot()
            time.sleep(0.02)
        return player.snapshot()

    try:
        player._ensure_player().settings.mute = True
        player.play(path)
        initial = wait_for(lambda s: s.state == "playing" and not s.rate_pending)
        assert initial.duration >= 15, "Use an audio file at least 15 seconds long."
        rates = []
        for rate in (0.5, 1.0, 1.5, 2.0):
            position_before = player.snapshot().position
            player.set_rate(rate)
            wait_for(lambda s: not s.rate_pending)
            before = advance(0.4)  # Allow the audio renderer's clock to settle.
            assert before.position >= position_before - 0.2, "Rate change restarted the file"
            start = time.monotonic()
            after = advance(1.0)
            measured = (after.position - before.position) / (time.monotonic() - start)
            assert abs(measured - rate) < 0.3, (rate, measured)
            assert player.path == path.resolve(), "Rate change selected a different file"
            rates.append({"requested": rate, "measured": round(measured, 3)})

        player.pause()
        paused = wait_for(lambda s: s.state == "paused")
        player.set_rate(1.25)
        wait_for(lambda s: not s.rate_pending)
        assert abs(advance(0.3).position - paused.position) < 0.1
        player.play(path)
        wait_for(lambda s: s.state == "playing" and not s.rate_pending)
        resumed = advance(0.4)
        assert paused.position <= resumed.position < paused.position + 2

        player.stop()
        wait_for(lambda s: s.state in ("stopped", "idle"))
        player.play(path)
        replayed = wait_for(lambda s: s.state == "playing" and not s.rate_pending)
        assert replayed.position < 2
        assert player.rate == 1.25
        assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
        print(json.dumps({"native_playback": "PASS", "rates": rates,
                          "pause_resume": "PASS", "replay": "PASS",
                          "source_unchanged": True}, ensure_ascii=True))
    finally:
        player.close()


def show_gui(path: Path) -> None:
    import tkinter as tk
    from podcast_gui import PodcastApp

    root = tk.Tk()
    app = PodcastApp(root)
    root.title("播放语速 · 验收预览（静音）")
    app.audio_player._ensure_player().settings.mute = True
    app.playback_target = path.resolve()
    app.playback_file_var.set(path.name)
    root.after(180_000, app._on_close)
    root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()
    if args.gui:
        show_gui(args.audio)
    else:
        run_check(args.audio)
