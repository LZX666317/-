from __future__ import annotations

import argparse
import itertools
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


BASE_SEED = 20260908
MAX_CHUNK_CHARS = 260
SAMPLE_RATE = 24_000

# Tuned against the 15号 source: the speaker is calm but conversational, with
# about a ten-semitone useful pitch span.  A nearly disabled exaggeration value
# made earlier renders dull and, unpredictably, much slower.
EXAGGERATION = 0.42
CFG_WEIGHT = 0.33
TEMPERATURE = 0.72
REPETITION_PENALTY = 1.15
MIN_P = 0.03

# The model is installed and cached locally. Avoid a network check on every
# click and make CUDA allocations friendlier on an 8 GB laptop GPU.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _split_long_form_text(text: str) -> list[tuple[str, float]]:
    chunks: list[tuple[str, float]] = []
    paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]

    for paragraph in paragraphs:
        normalized = re.sub(r"\s+", " ", paragraph).strip()
        if normalized[-1] not in ".!?":
            normalized += "."
        sentences = [
            item.strip()
            for item in re.split(r"(?<=[.!?])\s+", normalized)
            if item.strip()
        ]
        paragraph_chunks: list[str] = []

        for sentence in sentences:
            pieces = [sentence]
            if len(sentence) > MAX_CHUNK_CHARS:
                clauses = [
                    item.strip()
                    for item in re.split(r"(?<=[,;:])\s+", sentence)
                    if item.strip()
                ]
                pieces = []
                piece = ""
                for clause in clauses:
                    candidate = f"{piece} {clause}".strip()
                    if piece and len(candidate) > MAX_CHUNK_CHARS:
                        pieces.append(piece)
                        piece = clause
                    else:
                        piece = candidate
                if piece:
                    pieces.append(piece)

            expanded: list[str] = []
            for piece in pieces:
                if len(piece) <= MAX_CHUNK_CHARS:
                    expanded.append(piece)
                    continue
                words = piece.split()
                short_piece = ""
                for word in words:
                    candidate = f"{short_piece} {word}".strip()
                    if short_piece and len(candidate) > MAX_CHUNK_CHARS:
                        expanded.append(short_piece)
                        short_piece = word
                    else:
                        short_piece = candidate
                if short_piece:
                    expanded.append(short_piece)

            # Keep each complete sentence in one inference call. Combining
            # sentences into a near-limit block makes the model invent or
            # swallow a clause; splitting inside a sentence makes the voice
            # restart mid-thought. Long sentences were already divided above.
            paragraph_chunks.extend(expanded)
        for index, chunk in enumerate(paragraph_chunks):
            # A generated chunk already contains a natural sentence release.
            # Add only a short breath; longer synthetic gaps were the main
            # source of the old stop-start impression.
            gap = 0.18 if index == len(paragraph_chunks) - 1 else 0.12
            chunks.append((chunk, gap))

    if chunks:
        chunks[-1] = (chunks[-1][0], 0.0)
    return chunks


def _longest_true_run(values: list[bool]) -> int:
    return max(
        (sum(1 for _ in group) for value, group in itertools.groupby(values) if value),
        default=0,
    )


def _validate_generated_chunk(audio, word_count: int) -> str | None:
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size < SAMPLE_RATE // 2 or not np.isfinite(samples).all():
        return "音频为空或包含无效采样"

    duration = samples.size / SAMPLE_RATE
    minimum_duration = max(0.8, word_count * 0.13)
    maximum_duration = max(8.0, word_count * 0.78 + 5.0)
    if duration < minimum_duration or duration > maximum_duration:
        return f"时长异常（{duration:.1f} 秒）"

    peak = float(np.max(np.abs(samples)))
    clipped_ratio = float(np.mean(np.abs(samples) >= 0.995))
    if peak < 1e-4 or clipped_ratio > 0.001:
        return "音量异常或存在削波"

    frame_size = SAMPLE_RATE // 25
    hop_size = frame_size // 2
    harsh_frames: list[bool] = []
    frequencies = np.fft.rfftfreq(frame_size, 1.0 / SAMPLE_RATE)
    for start in range(0, samples.size - frame_size + 1, hop_size):
        frame = samples[start : start + frame_size]
        power = np.abs(np.fft.rfft(frame * np.hanning(frame_size))) ** 2 + 1e-12
        centroid = float(np.sum(frequencies * power) / np.sum(power))
        flatness = float(np.exp(np.mean(np.log(power))) / np.mean(power))
        rms_db = 20.0 * math.log10(float(np.sqrt(np.mean(frame * frame))) + 1e-9)
        harsh_frames.append(rms_db > -42.0 and centroid > 2800.0 and flatness > 0.075)
    harsh_seconds = _longest_true_run(harsh_frames) * hop_size / SAMPLE_RATE
    if harsh_seconds >= 0.18:
        return f"检测到高频怪声（{harsh_seconds:.2f} 秒）"
    return None


def _fade_edges(audio):
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).reshape(-1).copy()
    fade_samples = min(SAMPLE_RATE // 80, samples.size // 2)
    if fade_samples:
        ramp = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
        samples[:fade_samples] *= ramp
        samples[-fade_samples:] *= ramp[::-1]
    return samples


def _trim_chunk_edges(audio):
    """Remove model padding while retaining a tiny natural onset/release."""
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size < SAMPLE_RATE // 2:
        return samples
    frame = SAMPLE_RATE // 100
    usable = samples[: samples.size - samples.size % frame]
    if usable.size == 0:
        return samples
    levels = np.sqrt(np.mean(usable.reshape(-1, frame) ** 2, axis=1) + 1e-12)
    peak_db = 20.0 * np.log10(float(np.max(levels)) + 1e-9)
    threshold_db = max(-48.0, peak_db - 38.0)
    active = np.flatnonzero(20.0 * np.log10(levels + 1e-9) >= threshold_db)
    if not active.size:
        return samples
    padding = int(0.035 * SAMPLE_RATE)
    start = max(0, int(active[0] * frame) - padding)
    end = min(samples.size, int((active[-1] + 1) * frame) + padding)
    return samples[start:end]


def _find_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("没有找到 ffmpeg，无法输出 MP3。")
    return executable


def _convert_to_mp3(
    wav_path: Path,
    output_path: Path,
    speed: float,
    pitch_hz: int,
) -> None:
    filters: list[str] = []
    if pitch_hz:
        pitch_ratio = min(1.25, max(0.75, (119.0 + pitch_hz) / 119.0))
        filters.append(f"rubberband=pitch={pitch_ratio:.6f}:tempo=1")
    clamped_speed = min(1.25, max(0.70, speed))
    if abs(clamped_speed - 1.0) > 0.001:
        # Rubberband's transient/lamination modes preserve consonants and
        # connected speech more smoothly than atempo for this cloned voice.
        filters.append(
            f"rubberband=tempo={clamped_speed:.4f}:pitch=1:"
            "transients=smooth:detector=soft:phase=laminar:window=long:smoothing=on"
        )
    filters.append(
        "silenceremove="
        "start_periods=1:start_duration=0.08:start_threshold=-48dB:start_silence=0.08:"
        "stop_periods=-1:stop_duration=0.80:stop_threshold=-48dB:stop_silence=0.40"
    )
    filters.append("highpass=f=55")
    # The source's presence is clear but not hyped; a small high-end trim
    # keeps sibilants from sounding brighter than the reference clip.
    filters.append("treble=g=-1.5:f=3200:w=0.7")
    filters.append("lowpass=f=9500")
    filters.append("loudnorm=I=-23.5:TP=-3.5:LRA=12")

    command = [
        _find_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(wav_path),
        "-af",
        ",".join(filters),
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffmpeg 转码失败").strip()
        raise RuntimeError(detail)


def synthesize(
    text_file: Path,
    reference_wav: Path,
    output_path: Path,
    speed: float,
    pitch_hz: int,
) -> None:
    if not text_file.is_file():
        raise FileNotFoundError(f"找不到文案文件：{text_file}")
    if not reference_wav.is_file():
        raise FileNotFoundError(f"找不到参考音色：{reference_wav}")
    text = text_file.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("文案为空。")

    import numpy as np
    import soundfile as sf
    import torch
    from chatterbox.tts import ChatterboxTTS

    if not torch.cuda.is_available():
        raise RuntimeError("高保真引擎没有检测到 NVIDIA 显卡。")

    chunks = _split_long_form_text(text)
    if not chunks:
        raise ValueError("没有可生成的英文文案。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    model = ChatterboxTTS.from_pretrained(device="cuda")
    model.prepare_conditionals(str(reference_wav), exaggeration=EXAGGERATION)

    rendered: list[np.ndarray] = []
    for chunk_index, (chunk_text, gap_seconds) in enumerate(chunks):
        word_count = len(re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?", chunk_text))
        last_reason = "未知异常"
        chunk_audio = None
        for attempt in range(3):
            seed = BASE_SEED + chunk_index * 10007 + attempt * 1009
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            candidate_tensor = model.generate(
                chunk_text,
                exaggeration=EXAGGERATION,
                cfg_weight=CFG_WEIGHT,
                temperature=TEMPERATURE,
                repetition_penalty=REPETITION_PENALTY,
                min_p=MIN_P,
                top_p=1.0,
            )
            candidate = candidate_tensor.detach().cpu().numpy().reshape(-1)
            last_reason = _validate_generated_chunk(candidate, word_count) or ""
            if not last_reason:
                chunk_audio = _fade_edges(_trim_chunk_edges(candidate))
                print(
                    f"CHUNK={chunk_index + 1}/{len(chunks)} ATTEMPT={attempt + 1} OK",
                    flush=True,
                )
                break
            print(
                f"CHUNK={chunk_index + 1}/{len(chunks)} ATTEMPT={attempt + 1} RETRY={last_reason}",
                flush=True,
            )
            del candidate_tensor
            torch.cuda.empty_cache()
        if chunk_audio is None:
            raise RuntimeError(f"第 {chunk_index + 1} 段连续生成异常：{last_reason}")
        rendered.append(chunk_audio)
        if gap_seconds > 0:
            rendered.append(np.zeros(round(SAMPLE_RATE * gap_seconds), dtype=np.float32))

    combined = np.concatenate(rendered)
    with tempfile.TemporaryDirectory(prefix="HighFidelityVoiceRender-") as temp_dir:
        wav_path = Path(temp_dir) / "generated.wav"
        sf.write(str(wav_path), combined, SAMPLE_RATE, subtype="PCM_16")
        _convert_to_mp3(wav_path, output_path, speed, pitch_hz)

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("没有生成有效的 MP3 文件。")


def self_test() -> None:
    import torch
    import torchaudio
    from chatterbox.tts import ChatterboxTTS

    del torchaudio, ChatterboxTTS
    if not torch.cuda.is_available():
        raise RuntimeError("没有检测到可用的 NVIDIA CUDA 显卡。")
    _find_ffmpeg()
    print(f"HIGH_FIDELITY_ENGINE_OK={torch.cuda.get_device_name(0)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an authorized high-fidelity voice preview.")
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--pitch-hz", type=int, default=0)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.self_test:
            self_test()
            return 0
        if args.text_file is None or args.reference is None or args.output is None:
            raise ValueError("生成时必须提供 --text-file、--reference 和 --output。")
        synthesize(args.text_file, args.reference, args.output, args.speed, args.pitch_hz)
        print(f"GENERATED={args.output}")
        return 0
    except Exception as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
