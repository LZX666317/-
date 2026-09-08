"""In-process Windows audio playback. Call only from the Tk/main thread."""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path


MIN_RATE = 0.5
MAX_RATE = 2.0


class PlaybackError(RuntimeError):
    """An actionable playback or playback-rate error for the UI."""


@dataclass(frozen=True)
class PlaybackSnapshot:
    state: str = "idle"
    position: float = 0.0
    duration: float = 0.0
    rate_pending: bool = False


def _create_windows_player():
    if sys.platform != "win32":
        raise PlaybackError("内置试听目前需要 Windows。")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise PlaybackError(
            "缺少内置播放组件。请在项目目录运行：\n"
            ".venv\\Scripts\\python.exe -m pip install -r requirements-podcast.txt"
        ) from exc

    pythoncom.CoInitialize()
    try:
        player = win32com.client.Dispatch("WMPlayer.OCX")
        player.settings.autoStart = False
        player.settings.enableErrorDialogs = False
        player.settings.setMode("loop", False)
        player.settings.volume = 80
        return player, pythoncom
    except Exception as exc:
        pythoncom.CoUninitialize()
        raise PlaybackError(
            "无法初始化内置播放器，请确认 Windows Media Player（旧版）组件已启用。\n"
            f"{exc}"
        ) from exc


class AudioPlayer:
    """Own one player, never launch or control the user's external player.

    Rate is applied directly to the loaded media, without rewriting the file,
    seeking, or reloading it. WMP may reset rate when opening/stopping a file,
    so the selected rate is reapplied once playback is ready.
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self.rate = 1.0
        self._player = None
        self._pythoncom = None
        self._pending_rate = False
        self._rate_check_deadline = 0.0
        self._confirmed_rate = 1.0
        self._starting = False
        self._deadline = 0.0

    def _ensure_player(self):
        if self._player is None:
            self._player, self._pythoncom = _create_windows_player()
        return self._player

    def play(self, path: Path) -> None:
        path = Path(path).resolve()
        if not path.is_file():
            raise PlaybackError(f"找不到音频文件：\n{path}")
        try:
            player = self._ensure_player()
            self._rate_check_deadline = 0.0
            if path != self.path:
                player.close()  # Release the previous file before switching.
                self.path = None
                player.error.clearErrorQueue()
                player.URL = str(path)
                self.path = path
            player.controls.play()
            self._starting = True
            self._deadline = time.monotonic() + 15.0
            self._pending_rate = True
        except PlaybackError:
            raise
        except Exception as exc:
            self.close()
            raise PlaybackError(f"无法播放音频：{exc}") from exc

    def pause(self) -> None:
        if self._player is not None and self.path is not None:
            try:
                self._player.controls.pause()
                self._starting = False
            except Exception as exc:
                raise PlaybackError(f"无法暂停音频：{exc}") from exc

    def stop(self) -> None:
        if self._player is not None and self.path is not None:
            # Cancel pending asynchronous loading as well as audible playback.
            # Retain the selected rate; the GUI retains the selected path.
            if self._starting:
                self.close()
                return
            try:
                self._player.controls.stop()
            except Exception as exc:
                self.close()
                raise PlaybackError(f"无法停止音频：{exc}") from exc
        self._starting = False
        self._pending_rate = True
        self._rate_check_deadline = 0.0

    def set_rate(self, rate: float) -> None:
        try:
            rate = float(rate)
        except (TypeError, ValueError) as exc:
            raise PlaybackError("播放语速必须在 0.50×–2.00× 之间。") from exc
        if not math.isfinite(rate) or not MIN_RATE <= rate <= MAX_RATE:
            raise PlaybackError("播放语速必须在 0.50×–2.00× 之间。")
        self.rate = rate
        self._pending_rate = True
        if self._player is not None and self.path is not None:
            try:
                if self._player.openState == 13 and self._player.playState in (2, 3):
                    self._apply_rate()
            except PlaybackError:
                raise
            except Exception as exc:
                self.rate = self._confirmed_rate
                self.close()
                raise PlaybackError(f"无法调整播放语速：{exc}") from exc

    def _apply_rate(self) -> None:
        """Send a rate request; WMP acknowledges it asynchronously."""
        previous = self._confirmed_rate
        try:
            settings = self._player.settings
            requested = self.rate
            previous = float(settings.rate)
            if not settings.isAvailable("Rate"):
                if math.isclose(requested, previous, abs_tol=0.01):
                    self._pending_rate = False
                    self._confirmed_rate = previous
                    self._rate_check_deadline = 0.0
                    return
                raise PlaybackError("此音频格式暂不支持实时调速，请使用生成的 MP3 音频。")
            settings.rate = requested
            self._pending_rate = False
            self._rate_check_deadline = time.monotonic() + 2.0
            self._confirmed_rate = previous
        except Exception as exc:
            self.rate = previous
            self._confirmed_rate = previous
            self._pending_rate = False
            self._rate_check_deadline = 0.0
            if isinstance(exc, PlaybackError):
                raise
            self.close()
            raise PlaybackError(f"无法调整播放语速：{exc}") from exc

    def snapshot(self) -> PlaybackSnapshot:
        if self._player is None or self.path is None:
            return PlaybackSnapshot()
        try:
            # Tk pumps Windows messages too; this also supports headless tests.
            self._pythoncom.PumpWaitingMessages()
            player = self._player
            errors = player.error
            if errors.errorCount:
                detail = errors.item(0).errorDescription
                errors.clearErrorQueue()
                self._starting = False
                self.close()
                raise PlaybackError(f"音频无法读取或播放：{detail}")

            code = int(player.playState)
            if code in (2, 3, 8):
                self._starting = False
            if self._starting and time.monotonic() > self._deadline:
                self._starting = False
                self.close()
                raise PlaybackError("音频加载超时，请检查文件是否可播放后重试。")
            if self._pending_rate and player.openState == 13 and code in (2, 3):
                self._apply_rate()

            if self._rate_check_deadline and player.openState == 13 and code in (2, 3):
                actual = float(player.settings.rate)
                if math.isclose(actual, self.rate, abs_tol=0.01):
                    self._confirmed_rate = actual
                    self._rate_check_deadline = 0.0
                elif time.monotonic() > self._rate_check_deadline:
                    requested = self.rate
                    self.rate = actual
                    self._confirmed_rate = actual
                    self._rate_check_deadline = 0.0
                    raise PlaybackError(
                        f"播放器未能应用 {requested:.2f}× 语速，当前继续使用 {actual:.2f}×。"
                    )

            media = player.currentMedia
            duration = max(0.0, float(media.duration)) if media is not None else 0.0
            position = max(0.0, float(player.controls.currentPosition))
            if self._starting or code in (6, 7, 9, 11):
                state = "loading"
            else:
                state = {2: "paused", 3: "playing", 8: "ended"}.get(code, "stopped")
            return PlaybackSnapshot(state, position, duration, bool(self._rate_check_deadline))
        except PlaybackError:
            raise
        except Exception as exc:
            self.close()
            raise PlaybackError(f"无法读取播放状态：{exc}") from exc

    def close(self) -> None:
        """Release file handles and the COM apartment, even after an error."""
        player, pythoncom = self._player, self._pythoncom
        self._player = None
        self._pythoncom = None
        self.path = None
        self._starting = False
        self._pending_rate = False
        self._rate_check_deadline = 0.0
        try:
            if player is not None:
                player.close()
        except Exception:
            pass
        finally:
            # Drop the COM reference before uninitializing its apartment.
            player = None
            if pythoncom is not None:
                pythoncom.CoUninitialize()
