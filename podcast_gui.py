from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from podcast_player import AudioPlayer, PlaybackError


APP_DIR = Path(__file__).resolve().parent
PYTHON = APP_DIR / ".venv" / "Scripts" / "python.exe"
EDGE_TTS = APP_DIR / ".venv" / "Scripts" / "edge-tts.exe"
XTTS_SCRIPT = APP_DIR / "xtts_generate.py"
HIGH_FIDELITY_PYTHON = APP_DIR / ".venv-chatterbox" / "Scripts" / "python.exe"
HIGH_FIDELITY_SCRIPT = APP_DIR / "chatterbox_generate.py"
SCRIPT_FILE = APP_DIR / "\u64ad\u5ba2\u6587\u7a3f.txt"
OUTPUT_DIR = APP_DIR / "\u97f3\u9891"
PREVIEW_DIR = Path(tempfile.gettempdir()) / "BritishVoiceStudio"
PREVIEW_METADATA = PREVIEW_DIR / "last-preview.json"

BRITISH_GENTLE_VOICE = "英式温柔风"
AMERICAN_REFERENCE_VOICE = "美式风2x"
LOCAL_VOICE_REFERENCES = {
    BRITISH_GENTLE_VOICE: APP_DIR / "参考音色" / "英式温柔风参考.wav",
    AMERICAN_REFERENCE_VOICE: APP_DIR / "参考音色" / "美式风参考-15号视频-自然连续段.wav",
}
VOICES = {
    BRITISH_GENTLE_VOICE: None,
    AMERICAN_REFERENCE_VOICE: None,
    "英式男声 · Ryan（沉稳自然）": "en-GB-RyanNeural",
    "英式女声 · Sonia（清晰自然）": "en-GB-SoniaNeural",
    "英式女声 · Libby（亲切明亮）": "en-GB-LibbyNeural",
    "英式男声 · Thomas（成熟正式）": "en-GB-ThomasNeural",
}
VOICE_DEFAULT_GENERATION = {
    BRITISH_GENTLE_VOICE: (-18, 0),
    # The approved warm profile stays unpitched and deliberately paced. Its
    # fluency comes from sentence-level synthesis, not from speeding playback.
    AMERICAN_REFERENCE_VOICE: (-10, 0),
}

BG = "#0B1220"
PANEL = "#121C2E"
PANEL_ALT = "#17243A"
TEXT = "#F4F7FB"
MUTED = "#91A1B9"
ACCENT = "#5EEAD4"
ACCENT_ACTIVE = "#2DD4BF"
BUTTON_TEXT = "#062A28"
ERROR = "#FB7185"


class PodcastApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.generating = False
        self.last_output = self._find_latest_output()
        self.preview_output = self._find_latest_preview()
        self.preview_saved = self.preview_output is None
        self.preview_style = self._style_for_preview(self.preview_output)
        self.audio_player = AudioPlayer()
        self.playback_rate_var = tk.DoubleVar(value=1.0)
        self.playback_rate_text = tk.StringVar(value="1.00×")
        self.playback_file_var = tk.StringVar(value="暂无音频")
        self.playback_status_var = tk.StringVar(value="未播放")
        self.playback_target: Path | None = None
        self._playback_error_shown = False
        self._playback_poll_id: str | None = None
        self._closing = False

        root.title("英语口音播客生成器 · 英式温柔风")
        root.geometry("900x720")
        root.minsize(700, 560)
        root.configure(bg=BG)

        try:
            root.iconbitmap(default="")
        except tk.TclError:
            pass

        self._configure_styles()
        self._build_ui()
        self._load_script()
        for target in (self.preview_output, self.last_output):
            if target and target.is_file():
                self.playback_target = target.resolve()
                self.playback_file_var.set(target.name)
                break
        if self.preview_output:
            self.save_button.configure(state="normal")
            preview_name = self.preview_style or "音频"
            self._set_status(f"已有{preview_name}试听 · 可先试听，满意后再保存", ACCENT)
        self._center_window()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Podcast.Horizontal.TProgressbar",
            background=ACCENT,
            troughcolor=PANEL_ALT,
            bordercolor=PANEL_ALT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )
        style.configure(
            "Podcast.TCombobox",
            fieldbackground=PANEL_ALT,
            background=PANEL_ALT,
            foreground=TEXT,
            arrowcolor=ACCENT,
            bordercolor="#33445F",
            lightcolor="#33445F",
            darkcolor="#33445F",
            padding=8,
        )
        style.map(
            "Podcast.TCombobox",
            fieldbackground=[("readonly", PANEL_ALT)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", PANEL_ALT)],
            selectforeground=[("readonly", TEXT)],
        )

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=22, pady=(14, 10))

        title_row = tk.Frame(header, bg=BG)
        title_row.pack(fill="x")
        tk.Label(
            title_row,
            text="ENGLISH VOICE STUDIO",
            bg=ACCENT,
            fg=BUTTON_TEXT,
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=3,
        ).pack(side="left")
        tk.Label(
            title_row,
            text="把文案变成自然的英美口音",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 19, "bold"),
            anchor="w",
        ).pack(side="left", padx=(12, 0))
        tk.Label(
            header,
            text="输入英文文案，选择美式或英式声音，一键生成 MP3。",
            bg=BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(6, 0))

        body = tk.Frame(self.root, bg=PANEL, highlightthickness=1, highlightbackground="#243550")
        body.pack(fill="both", expand=True, padx=22, pady=(0, 10))

        editor_head = tk.Frame(body, bg=PANEL)
        editor_head.pack(fill="x", padx=16, pady=(8, 6))
        tk.Label(
            editor_head,
            text="播客文案",
            bg=PANEL,
            fg=TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side="left")
        self.counter_var = tk.StringVar(value="0 字符")
        tk.Label(
            editor_head,
            textvariable=self.counter_var,
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right")

        editor_wrap = tk.Frame(body, bg=PANEL_ALT, highlightthickness=1, highlightbackground="#33445F")
        editor_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        scrollbar = tk.Scrollbar(editor_wrap, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.editor = tk.Text(
            editor_wrap,
            wrap="word",
            undo=True,
            bg=PANEL_ALT,
            fg=TEXT,
            insertbackground=ACCENT,
            selectbackground="#285B66",
            relief="flat",
            borderwidth=0,
            height=6,
            padx=15,
            pady=13,
            font=("Segoe UI", 12),
            spacing1=2,
            spacing3=4,
            yscrollcommand=scrollbar.set,
        )
        self.editor.pack(fill="both", expand=True)
        scrollbar.config(command=self.editor.yview)
        self.editor.bind("<KeyRelease>", self._update_counter)

        settings = tk.Frame(body, bg=PANEL)
        settings.pack(fill="x", padx=16, pady=(12, 7), before=editor_head)
        settings.grid_columnconfigure(0, weight=3)
        settings.grid_columnconfigure(1, weight=2)
        settings.grid_columnconfigure(2, weight=2)

        self._field_label(settings, "声音风格", 0)
        self.voice_var = tk.StringVar(value=next(iter(VOICES)))
        self.voice_box = ttk.Combobox(
            settings,
            textvariable=self.voice_var,
            values=list(VOICES),
            state="readonly",
            style="Podcast.TCombobox",
            font=("Microsoft YaHei UI", 10),
        )
        self.voice_box.grid(row=1, column=0, sticky="ew", padx=(0, 16), pady=(6, 0))
        self.voice_box.bind("<<ComboboxSelected>>", self._apply_voice_defaults)

        # The cloned source has a connected, lightly brisk teaching cadence;
        # neutral synthesis sounded noticeably slow after chunk assembly.
        self.rate_var = tk.IntVar(value=-18)
        self.rate_text = tk.StringVar(value="-18%")
        self._slider_field(settings, "生成语速", self.rate_var, self.rate_text, 1, -40, 25, "%")

        self.pitch_var = tk.IntVar(value=0)
        self.pitch_text = tk.StringVar(value="+0 Hz")
        self._slider_field(settings, "音调", self.pitch_var, self.pitch_text, 2, -20, 20, " Hz")

        footer = tk.Frame(body, bg=PANEL)
        footer.pack(fill="x", padx=16, pady=(0, 8), before=editor_head)

        self.generate_button = tk.Button(
            footer,
            text="生成试听",
            command=self.generate,
            bg=ACCENT,
            fg=BUTTON_TEXT,
            activebackground=ACCENT_ACTIVE,
            activeforeground=BUTTON_TEXT,
            disabledforeground="#53716F",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 11, "bold"),
            padx=22,
            pady=11,
        )
        self.generate_button.pack(side="left")

        self.play_button = self._secondary_button(footer, "播放音频", self.play_preview)
        self.play_button.pack(side="left", padx=(10, 0))
        self.save_button = self._secondary_button(footer, "保存生成音频", self.save_preview)
        self.save_button.configure(state="disabled", disabledforeground="#5B687C")
        self.save_button.pack(side="left", padx=(10, 0))
        self._secondary_button(footer, "打开文件夹", self.open_folder).pack(side="left", padx=(10, 0))
        self._secondary_button(footer, "清空", self.clear_editor).pack(side="right")

        player_panel = tk.Frame(body, bg=PANEL_ALT, highlightthickness=1, highlightbackground="#33445F")
        player_panel.pack(fill="x", padx=16, pady=(0, 10), before=editor_head)
        player_top = tk.Frame(player_panel, bg=PANEL_ALT)
        player_top.pack(fill="x", padx=12, pady=(8, 2))
        tk.Label(
            player_top,
            text="播放语速（实时）",
            bg=PANEL_ALT,
            fg=TEXT,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        tk.Label(
            player_top,
            textvariable=self.playback_rate_text,
            bg=PANEL_ALT,
            fg=ACCENT,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="right")
        player_file = tk.Frame(player_panel, bg=PANEL_ALT)
        player_file.pack(fill="x", padx=12, pady=(0, 3))
        tk.Label(
            player_file,
            textvariable=self.playback_file_var,
            bg=PANEL_ALT,
            fg=MUTED,
            anchor="w",
            width=1,
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left", fill="x", expand=True)
        tk.Label(
            player_file,
            textvariable=self.playback_status_var,
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(side="right")
        self.playback_scale = tk.Scale(
            player_panel,
            from_=0.5,
            to=2.0,
            resolution=0.05,
            orient="horizontal",
            variable=self.playback_rate_var,
            command=self._on_playback_rate_change,
            showvalue=False,
            bg=PANEL_ALT,
            fg=TEXT,
            troughcolor=PANEL,
            activebackground=ACCENT,
            highlightthickness=0,
            borderwidth=0,
            sliderrelief="raised",
            sliderlength=22,
            takefocus=True,
        )
        self.playback_scale.pack(fill="x", padx=8, pady=(0, 5))
        player_ticks = tk.Frame(player_panel, bg=PANEL_ALT)
        player_ticks.pack(fill="x", padx=12, pady=(0, 7))
        tk.Label(player_ticks, text="0.50×", bg=PANEL_ALT, fg=MUTED, font=("Segoe UI", 8)).pack(side="left")
        tk.Label(player_ticks, text="1.00×", bg=PANEL_ALT, fg=MUTED, font=("Segoe UI", 8)).place(relx=1 / 3, anchor="n")
        tk.Label(player_ticks, text="2.00×", bg=PANEL_ALT, fg=MUTED, font=("Segoe UI", 8)).pack(side="right")

        player_actions = tk.Frame(player_panel, bg=PANEL_ALT)
        player_actions.pack(fill="x", padx=12, pady=(0, 8))
        self._secondary_button(player_actions, "选择已有音频", self.choose_audio).pack(side="left")
        self.stop_button = self._secondary_button(player_actions, "停止", self.stop_preview)
        self.stop_button.pack(side="left", padx=(8, 0))
        self.stop_button.configure(state="disabled", disabledforeground=MUTED)
        self._secondary_button(player_actions, "恢复 1×", self.reset_playback_rate).pack(side="left", padx=(8, 0))
        self._secondary_button(player_actions, "播放生成音频", self.play_generated_audio).pack(side="left", padx=(8, 0))
        for button in player_actions.winfo_children():
            button.configure(padx=8, pady=7)
        tk.Label(
            player_panel,
            text="播放中可直接调速，无需重新生成；保存生成音频仍保留原始速度。",
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 8))

        status_bar = tk.Frame(self.root, bg=BG)
        status_bar.pack(fill="x", padx=22, pady=(0, 12))
        self.progress = ttk.Progressbar(
            status_bar,
            mode="indeterminate",
            style="Podcast.Horizontal.TProgressbar",
            length=130,
        )
        self.progress.pack(side="left", padx=(0, 12))
        self.status_var = tk.StringVar(value="就绪 · 文案会自动保存到 播客文稿.txt")
        self.status_label = tk.Label(
            status_bar,
            textvariable=self.status_var,
            bg=BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self._playback_poll_id = self.root.after(100, self._poll_playback)

    def _field_label(self, parent: tk.Widget, text: str, column: int) -> None:
        tk.Label(
            parent,
            text=text,
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).grid(row=0, column=column, sticky="ew", padx=(0, 16) if column < 2 else 0)

    def _slider_field(
        self,
        parent: tk.Widget,
        label: str,
        variable: tk.IntVar,
        display: tk.StringVar,
        column: int,
        minimum: int,
        maximum: int,
        suffix: str,
    ) -> None:
        label_row = tk.Frame(parent, bg=PANEL)
        label_row.grid(row=0, column=column, sticky="ew", padx=(0, 16) if column < 2 else 0)
        tk.Label(label_row, text=label, bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Label(label_row, textvariable=display, bg=PANEL, fg=ACCENT, font=("Segoe UI", 9, "bold")).pack(side="right")

        def update(value: str) -> None:
            number = int(float(value))
            variable.set(number)
            display.set(f"{number:+d}{suffix}")

        scale = tk.Scale(
            parent,
            from_=minimum,
            to=maximum,
            orient="horizontal",
            variable=variable,
            command=update,
            showvalue=False,
            bg=PANEL,
            fg=TEXT,
            troughcolor=PANEL_ALT,
            activebackground=ACCENT,
            highlightthickness=0,
            borderwidth=0,
            sliderrelief="flat",
            resolution=1,
        )
        scale.grid(row=1, column=column, sticky="ew", padx=(0, 16) if column < 2 else 0, pady=(2, 0))

    def _apply_voice_defaults(self, _event=None) -> None:
        defaults = VOICE_DEFAULT_GENERATION.get(self.voice_var.get())
        if defaults is None:
            return
        rate, pitch = defaults
        self.rate_var.set(rate)
        self.rate_text.set(f"{rate:+d}%")
        self.pitch_var.set(pitch)
        self.pitch_text.set(f"{pitch:+d} Hz")

    def _secondary_button(self, parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=PANEL_ALT,
            fg=TEXT,
            activebackground="#22334E",
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9),
            padx=14,
            pady=11,
        )

    def _load_script(self) -> None:
        if SCRIPT_FILE.exists():
            try:
                text = SCRIPT_FILE.read_text(encoding="utf-8")
                self.editor.insert("1.0", text.strip())
            except OSError:
                pass
        self._update_counter()
        self.editor.focus_set()

    def _update_counter(self, _event=None) -> None:
        text = self.editor.get("1.0", "end-1c")
        self.counter_var.set(f"{len(text):,} 字符")

    def _center_window(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _find_latest_output(self) -> Path | None:
        if not OUTPUT_DIR.exists():
            return None
        # Existing projects may contain several naming schemes (including
        # manually generated “抖音…” files). Any non-empty local audio file is
        # a valid candidate; the picker still lets the user choose another one.
        files = [
            path
            for path in OUTPUT_DIR.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".mp3", ".wav", ".m4a", ".wma"}
            and path.stat().st_size > 0
        ]
        return max(files, key=lambda path: path.stat().st_mtime, default=None)

    def _find_latest_preview(self) -> Path | None:
        if not PREVIEW_DIR.exists():
            return None
        # Prefer the last preview created by this app. This avoids selecting
        # an unrelated MP3 left in the shared temp folder after a restart.
        try:
            import json

            metadata = json.loads(PREVIEW_METADATA.read_text(encoding="utf-8"))
            saved_path = Path(metadata.get("path", ""))
            if saved_path.is_file() and saved_path.stat().st_size > 0:
                return saved_path
        except (OSError, TypeError, ValueError):
            pass
        files = list(PREVIEW_DIR.glob("preview-*.mp3"))
        if not files:
            files = list(PREVIEW_DIR.glob("*.mp3"))
        files = [path for path in files if path.is_file() and path.stat().st_size > 0]
        return max(files, key=lambda path: path.stat().st_mtime, default=None)

    def _write_preview_metadata(self, output: Path) -> None:
        try:
            import json

            PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            PREVIEW_METADATA.write_text(
                json.dumps(
                    {
                        "path": str(output),
                        "style": self.preview_style,
                        "rate_percent": self.rate_var.get(),
                        "pitch_hz": self.pitch_var.get(),
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            # A valid MP3 remains usable even if the temp metadata file cannot
            # be written.
            pass

    @staticmethod
    def _style_for_preview(preview: Path | None) -> str:
        if preview and "british-gentle" in preview.name.lower():
            return BRITISH_GENTLE_VOICE
        if preview and "reference" in preview.name.lower():
            return "美式风"
        if preview and "official" in preview.name.lower():
            return "英式风"
        return ""

    def clear_editor(self) -> None:
        if self.generating:
            return
        self.editor.delete("1.0", "end")
        self._update_counter()
        self.editor.focus_set()

    def generate(self) -> None:
        if self.generating:
            return

        if self.preview_output and self.preview_output.exists() and not self.preview_saved:
            replace = messagebox.askyesno(
                "替换当前试听",
                "当前试听音频还没有保存。继续生成会替换它，是否继续？",
                parent=self.root,
            )
            if not replace:
                return

        text = self.editor.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("请填写文案", "请先在文案框中输入需要朗读的英文内容。", parent=self.root)
            self.editor.focus_set()
            return
        selected_voice = self.voice_var.get()
        reference_wav = LOCAL_VOICE_REFERENCES.get(selected_voice)
        is_reference_voice = reference_wav is not None
        # Route the 15号 natural-continuous reference through sentence-stable
        # XTTS. The GPU path can paraphrase clause boundaries on this script.
        prefer_xtts = is_reference_voice and "自然连续段" in reference_wav.name
        high_fidelity_ready = (
            HIGH_FIDELITY_PYTHON.exists()
            and HIGH_FIDELITY_SCRIPT.exists()
            and not prefer_xtts
        )
        required_runtime = (
            HIGH_FIDELITY_PYTHON if is_reference_voice and high_fidelity_ready
            else PYTHON if is_reference_voice
            else EDGE_TTS
        )
        if not required_runtime.exists():
            messagebox.showerror(
                "缺少运行环境",
                f"没有找到所需程序：\n{required_runtime}\n\n请保留项目中的 .venv 和 .venv-chatterbox 文件夹。",
                parent=self.root,
            )
            return
        if reference_wav is not None and not reference_wav.exists():
            messagebox.showerror(
                "参考音色尚未配置",
                f"找不到“{selected_voice}”的参考音色：\n{reference_wav}",
                parent=self.root,
            )
            return

        try:
            SCRIPT_FILE.write_text(text + "\n", encoding="utf-8")
            PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法准备生成文件", str(exc), parent=self.root)
            return

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if selected_voice == BRITISH_GENTLE_VOICE:
            preview_kind = "british-gentle"
        else:
            preview_kind = "reference" if is_reference_voice else "official"
        output = PREVIEW_DIR / f"preview-{preview_kind}-{stamp}.mp3"
        voice = VOICES[selected_voice]
        rate = self.rate_var.get()
        pitch = self.pitch_var.get()
        self.preview_style = (
            selected_voice
            if selected_voice in LOCAL_VOICE_REFERENCES
            else "英式风"
        )

        self.generating = True
        self.stop_preview(silent=True)
        self.generate_button.configure(state="disabled", text="正在生成…", bg="#315E5A")
        self.save_button.configure(state="disabled")
        self.progress.start(10)
        if is_reference_voice:
            engine_note = "逐句语气模式" if "自然连续段" in reference_wav.name else "本机高保真模式"
            self._set_status(f"正在用{engine_note}生成“{selected_voice}” · 首次加载约一分钟", ACCENT)
        else:
            self._set_status("正在连接语音服务…通常需要 10–20 秒", ACCENT)

        worker = threading.Thread(
            target=self._generate_worker,
            args=(
                output,
                voice,
                rate,
                pitch,
                reference_wav,
                selected_voice == AMERICAN_REFERENCE_VOICE,
            ),
            daemon=True,
        )
        worker.start()

    def _generate_worker(
        self,
        output: Path,
        voice: str | None,
        rate: int,
        pitch: int,
        reference_wav: Path | None,
        warm_magnetic: bool = False,
    ) -> None:
        if reference_wav is not None:
            self._generate_reference_worker(
                output,
                rate,
                pitch,
                reference_wav,
                warm_magnetic,
            )
            return

        last_error = "未知错误"
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        for attempt in range(1, 4):
            self._after(lambda n=attempt: self._set_status(f"正在生成英式音频 · 第 {n}/3 次尝试", ACCENT))
            try:
                if output.exists():
                    output.unlink()
                result = subprocess.run(
                    [
                        str(EDGE_TTS),
                        "--voice",
                        str(voice),
                        f"--rate={rate:+d}%",
                        f"--pitch={pitch:+d}Hz",
                        "--file",
                        str(SCRIPT_FILE),
                        "--write-media",
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    creationflags=flags,
                )
                if result.returncode == 0 and output.exists() and output.stat().st_size > 0:
                    self._after(lambda: self._generation_succeeded(output))
                    return
                last_error = (result.stderr or result.stdout or f"退出代码 {result.returncode}").strip()
            except subprocess.TimeoutExpired:
                last_error = "连接语音服务超过 60 秒，已终止本次请求。"
            except (OSError, ValueError) as exc:
                last_error = str(exc)

            if attempt < 3:
                self._after(lambda: self._set_status("本次连接失败，2 秒后自动重试…", ERROR))
                time.sleep(2)

        self._after(lambda: self._generation_failed(last_error))

    def _generate_reference_worker(
        self,
        output: Path,
        rate: int,
        pitch: int,
        reference_wav: Path,
        warm_magnetic: bool = False,
    ) -> None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        speed = min(1.25, max(0.70, 1.0 + rate / 100.0))
        try:
            if output.exists():
                output.unlink()
            prefer_xtts = "自然连续段" in reference_wav.name
            high_fidelity_ready = (
                HIGH_FIDELITY_PYTHON.exists()
                and HIGH_FIDELITY_SCRIPT.exists()
                and not prefer_xtts
            )
            command = [
                str(HIGH_FIDELITY_PYTHON if high_fidelity_ready else PYTHON),
                str(HIGH_FIDELITY_SCRIPT if high_fidelity_ready else XTTS_SCRIPT),
                "--text-file",
                str(SCRIPT_FILE),
                "--reference",
                str(reference_wav),
                "--output",
                str(output),
                "--speed",
                f"{speed:.2f}",
                "--pitch-hz",
                str(pitch),
            ]
            if warm_magnetic and not high_fidelity_ready:
                command.append("--warm-magnetic")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
                creationflags=flags,
            )
            if result.returncode == 0 and output.exists() and output.stat().st_size > 0:
                self._after(lambda: self._generation_succeeded(output))
                return
            detail = (result.stderr or result.stdout or f"退出代码 {result.returncode}").strip()
            if high_fidelity_ready:
                self._after(
                    lambda: self._set_status("高保真引擎未完成，正在自动切换兼容模式…", ERROR)
                )
                fallback = subprocess.run(
                    [
                        str(PYTHON),
                        str(XTTS_SCRIPT),
                        "--text-file",
                        str(SCRIPT_FILE),
                        "--reference",
                        str(reference_wav),
                        "--output",
                        str(output),
                        "--speed",
                        f"{speed:.2f}",
                        "--pitch-hz",
                        str(pitch),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=1800,
                    creationflags=flags,
                )
                if fallback.returncode == 0 and output.exists() and output.stat().st_size > 0:
                    self._after(lambda: self._generation_succeeded(output))
                    return
                fallback_detail = (fallback.stderr or fallback.stdout).strip()
                if fallback_detail:
                    detail = f"高保真模式：{detail}\n\n兼容模式：{fallback_detail}"
        except subprocess.TimeoutExpired:
            detail = "本机生成超过 30 分钟，已停止本次任务。"
        except (OSError, ValueError) as exc:
            detail = str(exc)
        self._after(lambda: self._generation_failed(detail))

    def _generation_succeeded(self, output: Path) -> None:
        previous_preview = self.preview_output
        self.audio_player.close()  # Release the old MP3 before deleting it.
        self.generating = False
        self.preview_output = output
        self.preview_saved = False
        self.progress.stop()
        self.generate_button.configure(state="normal", text="生成试听", bg=ACCENT)
        self.save_button.configure(state="normal")
        self._write_preview_metadata(output)
        self._set_status("试听已生成 · 满意后请点击“保存生成音频”", ACCENT)

        if previous_preview and previous_preview != output:
            try:
                previous_preview.unlink(missing_ok=True)
            except OSError:
                pass

        # Start in the embedded player so the playback-rate control is available
        # immediately. The original MP3 remains unchanged on disk.
        self._start_playback(output, auto=True)

    def _generation_failed(self, detail: str) -> None:
        self.generating = False
        self.progress.stop()
        self.generate_button.configure(state="normal", text="重新生成", bg=ACCENT)
        if self.preview_output and self.preview_output.exists() and not self.preview_saved:
            self.save_button.configure(state="normal")
        self._set_status("生成失败，请查看提示后重试", ERROR)
        concise = detail[-800:] if detail else "语音服务暂时不可用。"
        messagebox.showerror(
            "音频生成失败",
            f"本次生成没有成功。\n\n详细信息：\n{concise}",
            parent=self.root,
        )

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_label.configure(fg=color)

    def _after(self, callback) -> None:
        if self._closing:
            return
        try:
            self.root.after(0, lambda: callback() if not self._closing else None)
        except tk.TclError:
            pass

    def _start_playback(self, target: Path, auto: bool = False) -> None:
        try:
            self.audio_player.play(target)
            self.playback_target = target.resolve()
            self.playback_file_var.set(target.name)
            self.playback_status_var.set("加载中…")
            self.play_button.configure(text="加载中…", state="disabled")
            self.stop_button.configure(state="normal")
            self._playback_error_shown = False
            self._set_status(
                f"正在试听 · {target.name} · 可拖动“播放语速”实时调节",
                ACCENT,
            )
        except PlaybackError as exc:
            self.playback_status_var.set("无法播放")
            self._set_status(str(exc).splitlines()[0], ERROR)
            if auto:
                messagebox.showwarning("内置试听不可用", str(exc), parent=self.root)
            else:
                messagebox.showerror("无法播放", str(exc), parent=self.root)

    def play_preview(self) -> None:
        # Keep the selected file as the toggle target (including files chosen
        # through “选择已有音频”), instead of jumping back to the latest preview.
        target = self.playback_target or self.preview_output or self.last_output or self._find_latest_output()
        if target:
            try:
                snapshot = self.audio_player.snapshot()
                if snapshot.state == "loading":
                    return
                if snapshot.state == "playing" and self.audio_player.path == target.resolve():
                    self.audio_player.pause()
                    self.playback_status_var.set("已暂停")
                    self.play_button.configure(text="继续播放")
                    return
            except PlaybackError as exc:
                self._handle_playback_error(exc)
                return
            self._start_playback(target)
        else:
            messagebox.showinfo("暂无试听", "请点击“选择已有音频”，或先生成试听。", parent=self.root)

    def play_generated_audio(self) -> None:
        for target in (self.preview_output, self.last_output, self._find_latest_output()):
            if target and target.is_file():
                self._start_playback(target)
                return
        messagebox.showinfo("暂无试听", "请先生成试听或选择已有音频。", parent=self.root)

    def choose_audio(self) -> None:
        target = filedialog.askopenfilename(
            parent=self.root,
            title="选择要试听的音频",
            initialdir=str(OUTPUT_DIR if OUTPUT_DIR.exists() else APP_DIR),
            filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.wma"), ("所有文件", "*.*")],
        )
        if target:
            self._start_playback(Path(target))

    def stop_preview(self, silent: bool = False) -> None:
        try:
            self.audio_player.stop()
        except PlaybackError as exc:
            if not silent:
                self._set_status(str(exc), ERROR)
            return
        if not silent:
            self.playback_status_var.set("已停止")
            self._set_status("试听已停止", MUTED)

    def _on_playback_rate_change(self, value: str) -> None:
        rate = round(float(value), 2)
        self.playback_rate_text.set(f"{rate:.2f}×")
        self._playback_error_shown = False
        try:
            self.audio_player.set_rate(rate)
        except PlaybackError as exc:
            # Unsupported media keeps playing at the previous rate; restore the
            # thumb so the UI never claims a rate that WMP did not apply.
            actual = self.audio_player.rate
            self.playback_rate_var.set(actual)
            self.playback_rate_text.set(f"{actual:.2f}×")
            self._set_status(str(exc).splitlines()[0], ERROR)

    def reset_playback_rate(self) -> None:
        self.playback_rate_var.set(1.0)
        self._on_playback_rate_change("1.0")

    def _handle_playback_error(self, error: PlaybackError) -> None:
        if self._closing:
            return
        actual = self.audio_player.rate
        self.playback_rate_var.set(actual)
        self.playback_rate_text.set(f"{actual:.2f}×")
        if self.audio_player.path is None:
            self.playback_status_var.set("播放失败 · 可重试")
            self.play_button.configure(text="播放音频", state="normal")
            self.stop_button.configure(state="disabled")
        if not self._playback_error_shown:
            self._playback_error_shown = True
            self._set_status(str(error).splitlines()[0], ERROR)

    @staticmethod
    def _format_time(seconds: float) -> str:
        total = max(0, int(seconds))
        return f"{total // 60:02d}:{total % 60:02d}"

    def _poll_playback(self) -> None:
        self._playback_poll_id = None
        if self._closing:
            return
        try:
            snapshot = self.audio_player.snapshot()
            if self._closing:
                return
            rate = self.audio_player.rate
            pending = " · 调节中" if snapshot.rate_pending else ""
            self.playback_rate_text.set(f"{rate:.2f}×{pending}")
            if snapshot.state == "idle":
                if not self._playback_error_shown:
                    self.playback_status_var.set("未播放")
            elif snapshot.state == "loading":
                self.playback_status_var.set("加载中…")
            else:
                label = {"playing": "播放中", "paused": "已暂停", "stopped": "已停止", "ended": "播放结束"}[snapshot.state]
                self.playback_status_var.set(
                    f"{self._format_time(snapshot.position)} / {self._format_time(snapshot.duration)} · "
                    f"{label}"
                )
            button_text = {"playing": "暂停试听", "paused": "继续播放", "loading": "加载中…"}.get(snapshot.state, "播放音频")
            self.play_button.configure(text=button_text, state="disabled" if snapshot.state == "loading" else "normal")
            self.stop_button.configure(state="normal" if snapshot.state in ("loading", "playing", "paused") else "disabled")
        except PlaybackError as exc:
            self._handle_playback_error(exc)
        finally:
            if not self._closing:
                self._playback_poll_id = self.root.after(100, self._poll_playback)

    def save_preview(self) -> None:
        preview = self.preview_output
        if not preview or not preview.exists():
            messagebox.showinfo("暂无试听", "请先生成并试听音频。", parent=self.root)
            return

        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            style = f"-{self.preview_style}" if self.preview_style else ""
            destination = OUTPUT_DIR / f"英语播客{style}-{stamp}.mp3"
            suffix = 2
            while destination.exists():
                destination = OUTPUT_DIR / f"英语播客{style}-{stamp}-{suffix}.mp3"
                suffix += 1
            shutil.copy2(preview, destination)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return

        self.last_output = destination
        self.preview_saved = True
        self.save_button.configure(state="disabled")
        self._set_status(f"已保存 · {destination.name}", ACCENT)
        try:
            subprocess.Popen(["explorer.exe", f"/select,{destination}"], close_fds=True)
        except OSError:
            pass
        messagebox.showinfo("保存成功", f"音频已保存到：\n{destination}", parent=self.root)

    def open_folder(self) -> None:
        latest = self.last_output or self._find_latest_output()
        try:
            if latest and latest.exists():
                subprocess.Popen(["explorer.exe", f"/select,{latest}"], close_fds=True)
            else:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(OUTPUT_DIR)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开文件夹", str(exc), parent=self.root)

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._playback_poll_id is not None:
            self.root.after_cancel(self._playback_poll_id)
            self._playback_poll_id = None
        self.audio_player.close()
        # Keep the latest preview so the user can reopen the launcher and
        # listen/save it without regenerating.
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = PodcastApp(root)
    if "--smoke-test" in sys.argv:
        root.update_idletasks()
        print(f"GUI_OK title={root.title()} size={root.winfo_width()}x{root.winfo_height()}")
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
