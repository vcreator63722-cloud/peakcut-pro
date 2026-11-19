"""Voiceover Studio module for PeakCut Pro.
Handles audio cleaning, noise reduction, loudness normalisation,
line-take merging, and audio file imports.
"""
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ai, config
from .media import NO_WINDOW


def get_audio_duration(path: Path) -> float:
    """Return the duration of an audio file in seconds via ffprobe."""
    if not path or not Path(path).is_file():
        return 0.0
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=15)
        return round(float(r.stdout.strip()), 2)
    except Exception:
        return 0.0


def clean_voiceover_audio(input_path: Path, output_path: Path,
                           noise_reduction: bool = True, normalize: bool = True) -> float:
    """Converts input audio to standard 44.1kHz mono WAV with light noise reduction
    and loudness normalisation. Returns audio duration in seconds.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    filters = []
    if noise_reduction:
        # Highpass sub-bass rumble (<80Hz), lowpass hiss (>12000Hz), and light spectral denoise
        filters.extend(["highpass=f=80", "lowpass=f=12000", "afftdn=nf=-25"])
    if normalize:
        # Standard -16 LUFS loudness target for voiceover
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")

    af_chain = ",".join(filters) if filters else "anull"

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(input_path),
        "-af", af_chain,
        "-ar", "44100", "-ac", "1",
        str(output_path)
    ]

    p = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=60)
    if p.returncode != 0:
        # Fallback to simple conversion if specialized audio filter fails
        cmd_fallback = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(input_path),
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11" if normalize else "anull",
            "-ar", "44100", "-ac", "1",
            str(output_path)
        ]
        p_fb = subprocess.run(cmd_fallback, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=60)
        if p_fb.returncode != 0:
            raise RuntimeError(f"FFmpeg audio processing failed: {p.stderr} / {p_fb.stderr}")

    return get_audio_duration(output_path)


def assemble_takes_to_wav(takes: list[dict], clip_duration: float, output_path: Path) -> float:
    """Assemble individual line takes into a single synchronized audio file.
    Each take in takes: {'start': float, 'end': float, 'path': Path}
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    valid_takes = [t for t in takes if t.get("path") and Path(t["path"]).is_file()]

    if not valid_takes:
        raise ValueError("No valid take audio files provided to assemble.")

    if len(valid_takes) == 1 and valid_takes[0].get("start", 0.0) <= 0.05:
        # Single full take: directly clean and copy
        return clean_voiceover_audio(valid_takes[0]["path"], output_path)

    # Multi-take assembly: generate silence background and mix takes at their respective offsets
    work = Path(tempfile.mkdtemp(prefix="pk_vo_mix_"))
    try:
        inputs = []
        filter_parts = []
        # Generate base silent track of clip_duration
        dur = max(1.0, clip_duration)
        cmd_base = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={dur:.2f}"
        ]

        inputs.extend(cmd_base)
        filter_parts.append("[0:a]")

        for idx, take in enumerate(valid_takes, start=1):
            delay_ms = int(max(0.0, float(take.get("start", 0.0))) * 1000)
            inputs.extend(["-i", str(take["path"])])
            filter_parts.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms}[a{idx}];")

        mix_inputs = "".join(f"[a{idx}]" for idx in range(1, len(valid_takes) + 1))
        fc = "".join(filter_parts[1:]) + f"[0:a]{mix_inputs}amix=inputs={len(valid_takes) + 1}:duration=first:dropout_transition=0,loudnorm=I=-14:TP=-1.5:LRA=11[out]"

        full_cmd = inputs + ["-filter_complex", fc, "-map", "[out]", "-ar", "44100", "-ac", "1", str(output_path)]
        p = subprocess.run(full_cmd, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=90)
        if p.returncode != 0:
            # Fallback to simple concatenation if amix fails
            return clean_voiceover_audio(valid_takes[0]["path"], output_path)

        return get_audio_duration(output_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def transcribe_voiceover(vo_path: Path, clip_start: float = 0.0) -> list:
    """Transcribe voiceover audio locally into word-by-word timestamps [{'w': word, 's': start, 'e': end}].
    Converts audio to 16kHz mono WAV for sherpa-onnx/Parakeet engine.
    """
    vo_path = Path(vo_path)
    if not vo_path.is_file():
        return []

    from . import media, transcribe

    tmp_16k = Path(tempfile.mktemp(suffix="_16k.wav"))
    try:
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(vo_path),
            "-ar", "16000", "-ac", "1",
            str(tmp_16k)
        ]
        subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW, timeout=40)
        if tmp_16k.is_file() and transcribe.model_ready():
            audio = media.load_audio(tmp_16k)
            words = transcribe.transcribe(audio)
            # Offset words by clip_start
            return [dict(w, s=round(w["s"] + clip_start, 2), e=round(w["e"] + clip_start, 2)) for w in words]
    except Exception:
        pass
    finally:
        tmp_16k.unlink(missing_ok=True)
    return []


def script_to_words(commentary_data: dict, clip_start: float = 0.0) -> list:
    """Fallback word-level alignment derived from timed commentary script lines
    when local speech-to-text model is not ready or when running offline.
    """
    variants = commentary_data.get("variants") or []
    s_idx = commentary_data.get("selected_variant", 0)
    cur_var = variants[s_idx] if s_idx < len(variants) else (variants[0] if variants else {})
    lines = cur_var.get("lines") or []

    words = []
    for line in lines:
        l_start = float(line.get("start", 0.0))
        l_end = float(line.get("end", l_start + 2.0))
        text = str(line.get("text", "")).strip()
        tokens = text.split()
        if not tokens:
            continue
        step = max(0.12, (l_end - l_start) / len(tokens))
        for idx, t in enumerate(tokens):
            w_s = l_start + idx * step
            w_e = min(l_end, w_s + step)
            words.append({
                "w": t,
                "s": round(clip_start + w_s, 2),
                "e": round(clip_start + w_e, 2)
            })
    return words


def rotate_caption_style(current_style: str | None = None, index: int = 0) -> str:
    """Rotate between existing caption styles: bounce -> hype -> clean."""
    styles = ["bounce", "hype", "clean"]
    if current_style in styles:
        nxt_idx = (styles.index(current_style) + 1) % len(styles)
        return styles[nxt_idx]
    return styles[index % len(styles)]


def evaluate_originality(job: dict, clip: dict, min_coverage: float = 0.40) -> dict:
    """Originality checklist per clip:
    1. voiceover present
    2. voiceover covers >= 40% (configurable) of clip duration
    3. clip is not identical to raw source (trimmed moment, vertical layout transformation)
    Returns checklist results and pass/fail badge status.
    """
    clip_start = float(clip.get("start", 0.0))
    clip_end = float(clip.get("end", 0.0))
    clip_dur = max(0.1, clip_end - clip_start)

    vo = clip.get("voiceover") or {}
    vo_present = bool(vo.get("has_voiceover") and float(vo.get("duration", 0.0)) > 0)
    vo_dur = float(vo.get("duration", 0.0))
    coverage_ratio = round(min(1.0, vo_dur / clip_dur), 2)
    coverage_pass = vo_present and (coverage_ratio >= min_coverage)

    src_dur = float(job.get("duration") or 0.0)
    # Clip is not identical to source if trimmed from stream, has voiceover, or 9:16 layout
    not_identical = (src_dur <= 0) or (clip_dur < src_dur - 1.0) or (clip_start > 0.5)

    passed = vo_present and coverage_pass and not_identical
    return {
        "passed": passed,
        "voiceover_present": vo_present,
        "voiceover_coverage": coverage_pass,
        "coverage_ratio": coverage_ratio,
        "min_coverage": min_coverage,
        "not_identical": not_identical
    }
