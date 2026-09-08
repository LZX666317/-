from __future__ import annotations

import argparse
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
# The desktop GUI has no interactive stdin. The user accepted the XTTS/Coqui
# model terms on 2026-09-08, so persist that choice for cache refreshes too.
os.environ.setdefault("COQUI_TOS_AGREED", "1")
BASE_SEED = 20260907
# Shorter clauses preserve the measured slow, deliberate cadence of the
# current reference speaker better than near-limit inference blocks.
MAX_CHUNK_CHARS = 260
SAMPLE_RATE = 24_000


def _split_long_form_text(text: str) -> list[tuple[str, float]]:
    """Split prose into stable narration chunks and return each following gap."""
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
                pieces = []
                words = sentence.split()
                piece = ""
                for word in words:
                    candidate = f"{piece} {word}".strip()
                    if piece and len(candidate) > MAX_CHUNK_CHARS:
                        pieces.append(piece)
                        piece = word
                    else:
                        piece = candidate
                if piece:
                    pieces.append(piece)

            # Preserve sentence-level prosody. A shared conditioning state
            # keeps identity consistent; a fresh inference call per sentence
            # avoids mid-clause restarts and near-limit truncation.
            paragraph_chunks.extend(pieces)
        for index, chunk in enumerate(paragraph_chunks):
            # The source speaker connects ideas closely; a long synthetic gap
            # makes the delivery feel stop-start. Keep only a breath-sized gap.
            gap_seconds = 0.14 if index == len(paragraph_chunks) - 1 else 0.10
            chunks.append((chunk, gap_seconds))

    if chunks:
        chunks[-1] = (chunks[-1][0], 0.0)
    return chunks


def _longest_true_run(values) -> int:
    longest = 0
    current = 0
    for value in values:
        if bool(value):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _validate_generated_chunk(audio, word_count: int) -> str | None:
    """Return a retry reason for broken, implausible, or noisy generations."""
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size < SAMPLE_RATE // 2 or not np.isfinite(samples).all():
        return "音频为空或包含无效采样"

    duration = samples.size / SAMPLE_RATE
    minimum_duration = max(0.8, word_count * 0.18)
    maximum_duration = max(8.0, word_count * 0.95 + 4.0)
    if duration < minimum_duration or duration > maximum_duration:
        return f"时长异常（{duration:.1f} 秒）"

    peak = float(np.max(np.abs(samples)))
    clipped_ratio = float(np.mean(np.abs(samples) >= 0.995))
    if peak < 1e-4 or clipped_ratio > 0.001:
        return "音量异常或存在削波"

    frame_size = SAMPLE_RATE // 25
    hop_size = frame_size // 2
    noisy_frames: list[bool] = []
    for start in range(0, samples.size - frame_size + 1, hop_size):
        frame = samples[start : start + frame_size]
        rms_db = 20.0 * math.log10(float(np.sqrt(np.mean(frame * frame))) + 1e-9)
        zero_crossing_rate = float(np.mean(frame[:-1] * frame[1:] < 0))
        noisy_frames.append(rms_db > -36.0 and zero_crossing_rate > 0.30)
    noisy_run_seconds = _longest_true_run(noisy_frames) * hop_size / SAMPLE_RATE
    if noisy_run_seconds >= 0.48:
        return f"检测到持续异常噪声（{noisy_run_seconds:.2f} 秒）"

    # XTTS can occasionally emit a short, bright digital burst. It is not
    # always loud enough for the RMS/ZCR test above, so inspect spectral
    # flatness and centroid as a second, conservative guard.
    harsh_frames: list[bool] = []
    frequencies = np.fft.rfftfreq(frame_size, 1.0 / SAMPLE_RATE)
    for start in range(0, samples.size - frame_size + 1, hop_size):
        frame = samples[start : start + frame_size]
        power = np.abs(np.fft.rfft(frame * np.hanning(frame_size))) ** 2 + 1e-12
        spectral_centroid = float(np.sum(frequencies * power) / np.sum(power))
        spectral_flatness = float(np.exp(np.mean(np.log(power))) / np.mean(power))
        rms_db = 20.0 * math.log10(float(np.sqrt(np.mean(frame * frame))) + 1e-9)
        harsh_frames.append(
            rms_db > -42.0
            and spectral_centroid > 2800.0
            and spectral_flatness > 0.075
        )
    harsh_run_seconds = _longest_true_run(harsh_frames) * hop_size / SAMPLE_RATE
    if harsh_run_seconds >= 0.22:
        return f"检测到短促高频毛刺（{harsh_run_seconds:.2f} 秒）"
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


def _install_soundfile_loader() -> None:
    """Make WAV loading independent from TorchCodec/FFmpeg shared DLLs."""
    import soundfile as sf
    import torch
    import torchaudio

    def load_with_soundfile(
        uri,
        frame_offset: int = 0,
        num_frames: int = -1,
        normalize: bool = True,
        channels_first: bool = True,
        format=None,
        buffer_size: int = 4096,
        backend=None,
    ):
        del normalize, format, buffer_size, backend
        frames = -1 if num_frames is None or num_frames < 0 else num_frames
        data, sample_rate = sf.read(
            str(uri),
            start=max(0, frame_offset),
            frames=frames,
            dtype="float32",
            always_2d=True,
        )
        tensor = torch.from_numpy(data)
        if channels_first:
            tensor = tensor.transpose(0, 1)
        return tensor, sample_rate

    torchaudio.load = load_with_soundfile


def _find_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("没有找到 ffmpeg，无法输出 MP3。")
    return executable


def _convert_to_mp3(
    wav_path: Path,
    output_path: Path,
    pitch_hz: int,
    warm_magnetic: bool = False,
) -> None:
    ffmpeg = _find_ffmpeg()
    filters: list[str] = []
    # Keep tempo changes in synthesis, where phonemes remain natural. A final
    # time-stretch made slow narration sound granular and intermittently choppy.
    if pitch_hz:
        pitch_ratio = min(1.25, max(0.75, (119.0 + pitch_hz) / 119.0))
        filters.append(f"rubberband=pitch={pitch_ratio:.6f}:tempo=1")
    # The 15号 source is brighter than the previous reference. Preserve its
    # consonants and upper harmonics instead of applying a dark voice profile.
    filters.append("highpass=f=55")
    filters.append("equalizer=f=170:t=q:w=0.8:g=1.0")
    # Match the 15号 source's softer upper band. The raw clone is otherwise
    # noticeably brighter than the compressed reference video.
    filters.append("treble=g=-1.5:f=3200:w=0.7")
    filters.append("lowpass=f=9500")
    # The accepted voice profile has a faint low-frequency residual in pauses.
    # Remove it conservatively, then restore the upper-band balance lost by
    # spectral denoising. Do not use a hard gate: it can clip weak word tails.
    filters.append("afftdn=nr=14:nf=-50:tn=1:ad=0.7:gs=12")
    filters.append("treble=g=2.0:f=3200:w=0.7")
    filters.append(
        "silenceremove="
        "start_periods=1:start_duration=0.08:start_threshold=-48dB:start_silence=0.08:"
        "stop_periods=-1:stop_duration=0.65:stop_threshold=-45dB:stop_silence=0.30"
    )
    # Restore some of the reference speaker's emphasis contrast without
    # changing pitch or spectral colour.
    filters.append(
        "compand=attacks=0.02:decays=0.30:"
        "points=-80/-80|-45/-48|-30/-32|-20/-18|-10/-6|0/-1:soft-knee=5"
    )
    filters.append("loudnorm=I=-23.5:TP=-3.2:LRA=12")
    if warm_magnetic:
        # Approved 15号暖男 profile: make quiet syllables feel closer and
        # soften level jumps without changing pitch or applying tone-shaping
        # EQ. Keeping this after the established colour pipeline preserves the
        # source voice's brightness while adding a restrained, intimate body.
        filters.append(
            "acompressor=threshold=0.055:ratio=1.30:attack=35:release=260:"
            "makeup=1.12:knee=2.5:detection=rms:link=average"
        )
        filters.append("loudnorm=I=-24:TP=-5:LRA=5")

    command = [
        ffmpeg,
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
    reference_wavs: list[Path],
    output_path: Path,
    speed: float,
    pitch_hz: int,
    style_references: list[Path] | None = None,
    warm_magnetic: bool = False,
) -> None:
    if not text_file.is_file():
        raise FileNotFoundError(f"找不到文案文件：{text_file}")
    if not reference_wavs:
        raise ValueError("没有提供参考音色。")
    missing_references = [path for path in reference_wavs if not path.is_file()]
    if missing_references:
        raise FileNotFoundError(f"找不到参考音色：{missing_references[0]}")
    style_references = style_references or []
    missing_styles = [path for path in style_references if not path.is_file()]
    if missing_styles:
        raise FileNotFoundError(f"找不到语气参考：{missing_styles[0]}")

    text = text_file.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("文案为空。")

    _install_soundfile_loader()
    import numpy as np
    import torch
    from TTS.api import TTS

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="BritishVoiceRender-") as temp_dir:
        wav_path = Path(temp_dir) / "generated.wav"
        engine = TTS(model_name=MODEL_NAME, progress_bar=False, gpu=False)
        speaker_reference: str | list[str]
        if len(reference_wavs) == 1:
            speaker_reference = str(reference_wavs[0])
        else:
            speaker_reference = [str(path) for path in reference_wavs]
        model = engine.synthesizer.tts_model
        identity_conditioning, speaker_embedding = model.get_conditioning_latents(
            audio_path=speaker_reference,
            sound_norm_refs=False,
        )
        chunks = _split_long_form_text(text)
        if not chunks:
            raise ValueError("没有可生成的英文文案。")

        rendered: list[np.ndarray] = []
        # Extremely low inference speeds can stretch individual phonemes and
        # create a stop-start delivery. Keep the model in its fluent range.
        clamped_speed = min(1.25, max(0.58, speed))
        for chunk_index, (chunk_text, gap_seconds) in enumerate(chunks):
            conditioning = identity_conditioning
            if style_references:
                # Keep the speaker embedding fixed for identity while using a
                # different clean, conversational source segment for each
                # sentence group. This transfers natural emphasis without the
                # flat, sleepy contour caused by one global style clip.
                style_path = style_references[min(chunk_index, len(style_references) - 1)]
                style_conditioning, _ = model.get_conditioning_latents(
                    audio_path=str(style_path),
                    gpt_cond_len=12,
                    sound_norm_refs=False,
                )
                conditioning = 0.50 * identity_conditioning + 0.50 * style_conditioning
            word_count = len(re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?", chunk_text))
            last_reason = "未知异常"
            chunk_audio = None
            fallback_audio = None
            fallback_score = float("inf")
            for attempt in range(3):
                # Vary the deterministic seed per sentence group so every line
                # does not inherit the same flat intonation contour.
                seed = BASE_SEED + chunk_index * 10007 + attempt * 1009
                random.seed(seed)
                np.random.seed(seed)
                torch.manual_seed(seed)
                result = model.inference(
                    text=chunk_text,
                    language="en",
                    gpt_cond_latent=conditioning,
                    speaker_embedding=speaker_embedding,
                    speed=clamped_speed,
                    temperature=0.78,
                    repetition_penalty=1.8,
                    top_k=50,
                    top_p=0.90,
                    enable_text_splitting=False,
                )
                candidate = np.asarray(result["wav"], dtype=np.float32).reshape(-1)
                last_reason = _validate_generated_chunk(candidate, word_count) or ""
                if not last_reason:
                    chunk_audio = _fade_edges(candidate)
                    print(
                        f"CHUNK={chunk_index + 1}/{len(chunks)} ATTEMPT={attempt + 1} OK",
                        flush=True,
                    )
                    break
                # Keep the least suspicious candidate as a safe fallback. This
                # prevents a false-positive short consonant from aborting an
                # otherwise usable long-form render.
                match = re.search(r"（([0-9.]+) 秒）", last_reason)
                score = float(match.group(1)) if match else 999.0
                if score < fallback_score:
                    fallback_audio = candidate
                    fallback_score = score
                print(
                    f"CHUNK={chunk_index + 1}/{len(chunks)} ATTEMPT={attempt + 1} RETRY={last_reason}",
                    flush=True,
                )
            if chunk_audio is None:
                if fallback_audio is None:
                    raise RuntimeError(f"第 {chunk_index + 1} 段连续生成异常：{last_reason}")
                chunk_audio = _fade_edges(fallback_audio)
                print(
                    f"CHUNK={chunk_index + 1}/{len(chunks)} FALLBACK score={fallback_score:.2f}s",
                    flush=True,
                )
            rendered.append(chunk_audio)
            if gap_seconds > 0:
                rendered.append(np.zeros(round(SAMPLE_RATE * gap_seconds), dtype=np.float32))

        combined = np.concatenate(rendered)
        import soundfile as sf

        sf.write(str(wav_path), combined, SAMPLE_RATE, subtype="PCM_16")
        if not wav_path.is_file() or wav_path.stat().st_size == 0:
            raise RuntimeError("语音模型没有生成有效的 WAV 文件。")
        _convert_to_mp3(wav_path, output_path, pitch_hz, warm_magnetic)

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("没有生成有效的 MP3 文件。")


def self_test() -> None:
    import wave

    with tempfile.TemporaryDirectory(prefix="BritishVoiceSelfTest-") as temp_dir:
        sample_path = Path(temp_dir) / "sample.wav"
        sample_rate = 24_000
        frame_count = sample_rate // 10
        frames = bytearray()
        for index in range(frame_count):
            value = int(1200 * math.sin(2 * math.pi * 220 * index / sample_rate))
            frames.extend(value.to_bytes(2, "little", signed=True))
        with wave.open(str(sample_path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes(frames)

        _install_soundfile_loader()
        import torchaudio

        audio, loaded_rate = torchaudio.load(sample_path)
        if loaded_rate != sample_rate or tuple(audio.shape) != (1, frame_count):
            raise RuntimeError(f"音频解码自检失败：rate={loaded_rate}, shape={tuple(audio.shape)}")
    print("XTTS_LOADER_OK")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a consented non-commercial British voice preview.")
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--reference", type=Path, nargs="+")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--speed", type=float, default=0.90)
    parser.add_argument("--pitch-hz", type=int, default=0)
    parser.add_argument("--style-reference", type=Path, nargs="+")
    parser.add_argument(
        "--warm-magnetic",
        action="store_true",
        help="Apply the approved warm, intimate dynamics profile without changing pitch or EQ.",
    )
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
        synthesize(
            args.text_file,
            args.reference,
            args.output,
            args.speed,
            args.pitch_hz,
            args.style_reference,
            args.warm_magnetic,
        )
        print(f"GENERATED={args.output}")
        return 0
    except Exception as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
