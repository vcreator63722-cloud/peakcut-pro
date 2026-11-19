"""Frame-aware Commentary Writer for gaming Shorts.
Extracts frames (1 every 2s) + transcript, sends to AI via studio/ai.py,
validates speaking pace, checks safe-start violence, and produces 3 variants.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ai, config, usage

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TONES = {
    "funny": "Playful, humorous, witty reactions and jokes about the gameplay.",
    "roast": "Sarcastic, teasing the players or NPCs, witty banter.",
    "hype": "High-energy, thrilled, energetic esports-caster style hype.",
    "informative": "Analytical, breakdown of tactics, game mechanics, and details.",
    "mystery": "Suspenseful, intriguing, questioning hidden secrets or easter eggs."
}

STYLES = {
    "devanagari_hindi": "Default style: Hindi words written in Devanagari script, and English/gaming terms in Latin script (e.g. 'भाई ये क्या move था! Incredible clutch!').",
    "roman_hinglish": "Full Roman Hinglish: All words in Latin letters (e.g. 'Bhai yeh kya move tha! Incredible clutch!')."
}


def extract_clip_frames(src_video: Path, start: float, end: float, step_seconds: float = 2.0) -> list[bytes]:
    """Extract downscaled JPEG frames (one every step_seconds) from the clip time range."""
    if not src_video or not Path(src_video).is_file():
        return []
    dur = max(0.5, end - start)
    fps = 1.0 / max(0.5, step_seconds)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pk_frames_"))
    frames = []
    try:
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start:.2f}", "-to", f"{end:.2f}",
            "-i", str(src_video),
            "-vf", f"fps={fps:.3f},scale=320:180:force_original_aspect_ratio=decrease",
            "-q:v", "4",
            str(tmp_dir / "frame_%03d.jpg")
        ]
        subprocess.run(cmd, capture_output=True, timeout=60, creationflags=NO_WINDOW)
        for img_path in sorted(tmp_dir.glob("frame_*.jpg")):
            try:
                frames.append(img_path.read_bytes())
            except OSError:
                pass
    except Exception:
        pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return frames


def build_commentary_prompt(transcript: str, clip_duration: float, tone: str = "hype",
                             channel_voice: str = "", script_style: str = "devanagari_hindi",
                             speaking_pace: float = 2.3) -> str:
    tone_desc = TONES.get(tone, TONES["hype"])
    style_desc = STYLES.get(script_style, STYLES["devanagari_hindi"])
    max_words = max(8, int(clip_duration * speaking_pace))

    prompt = f"""You are the lead commentary writer for viral Grand Theft Auto (GTA 6 / Vice City) gaming Shorts.
The creator speaks Hindi/Hinglish and needs commentary to record over this gameplay clip.

CLIP DURATION: {clip_duration:.1f} seconds.
SPEAKING PACE: ~{speaking_pace:.1f} words per second.
STRICT WORD BUDGET: The commentary script MUST NOT exceed {max_words} words across all lines combined.

TONE: {tone.upper()} ({tone_desc})
SCRIPT STYLE: {style_desc}

CHANNEL VOICE SAMPLE (Match this exact persona/rhythm):
{channel_voice.strip() if channel_voice.strip() else "Bhai log look at this drift! Kya crazy scene hai!"}

CLIP TRANSCRIPT / AUDIO CONTEXT:
{transcript.strip() if transcript.strip() else "(No spoken words in clip - focus on game action/visuals)"}

HARD RULES:
1. NEVER invent game facts or false storyline lore. Describe only what is visibly happening in the video frames or audible in the transcript.
2. If anything in the clip is uncertain, speculative, or unconfirmed, mark it explicitly with [CHECK].
3. SAFE-START CHECK: Look at the cover frame and the first 7.0 seconds. If there is extreme graphic violence or prohibited content that would hurt platform monetization, set safe_start.flagged = true, explain the reason, and suggest a safer start time (suggested_start). Otherwise safe_start.flagged = false.
4. Generate THREE (3) distinct, punchy commentary variants (variant_id 1, 2, 3) with different angles.
5. In each variant, provide timed lines [{{"start": 0.0, "end": ..., "text": "..."}}] synchronized with the clip moments.
"""
    return prompt


def make_offline_variants(clip_duration: float, tone: str, script_style: str, speaking_pace: float) -> dict:
    """Fallback 3 variants when AI key is absent."""
    max_words = max(8, int(clip_duration * speaking_pace))
    t1 = "0.0s se notice karo ye play! GTA 6 ka next level physics look at this!"
    t2 = "Watch this drift closely! Vice City ke streets par insane control!"
    t3 = "Wait for it! Ye moment miss mat karna [CHECK]!"
    if script_style == "devanagari_hindi":
        t1 = "शुरुआत से notice करो ये play! GTA 6 का next level physics look at this!"
        t2 = "Watch this drift closely! Vice City के streets पर insane control!"
        t3 = "Wait for it! ये moment miss मत करना [CHECK]!"

    half = round(clip_duration / 2.0, 1)
    variants = [
        {
            "variant_id": 1,
            "hook": "GTA 6 INSANE MOMENT",
            "lines": [
                {"start": 0.0, "end": half, "text": t1[:max_words * 4]},
                {"start": half, "end": round(clip_duration, 1), "text": "Kya clutch play tha bhai!"}
            ],
            "outro": "Subscribe for daily GTA 6 moments!",
            "title": "GTA 6 Insane Clutch in Vice City",
            "description": "Watch this gameplay moment in GTA 6 #GTA6 #Shorts",
            "hashtags": ["GTA6", "Gaming", "ViceCity", "RockstarGames"]
        },
        {
            "variant_id": 2,
            "hook": "UNBELIEVABLE MOVE",
            "lines": [
                {"start": 0.0, "end": half, "text": t2[:max_words * 4]},
                {"start": half, "end": round(clip_duration, 1), "text": "Nobody expected that turn!"}
            ],
            "outro": "Comment what you think about this!",
            "title": "GTA 6 Next Level Gameplay Move",
            "description": "Crazy drift action in Vice City streets #GTA6",
            "hashtags": ["GTA6", "GamingShorts", "GTAOnline"]
        },
        {
            "variant_id": 3,
            "hook": "DID YOU SEE THAT?",
            "lines": [
                {"start": 0.0, "end": half, "text": t3[:max_words * 4]},
                {"start": half, "end": round(clip_duration, 1), "text": "Vice City secrets revealed [CHECK]!"}
            ],
            "outro": "Like and follow for more!",
            "title": "GTA 6 Secret Gameplay Moment",
            "description": "Checking out GTA 6 highlights #GTA6",
            "hashtags": ["GTA6", "GamingHighlights", "ViralShorts"]
        }
    ]
    return {
        "safe_start": {"flagged": False, "reason": "", "suggested_start": 0.0},
        "variants": variants
    }


def generate_for_clip(job: dict, clip: dict, tone: str = "hype", channel_voice: str = "",
                      script_style: str = "devanagari_hindi", speaking_pace: float = 2.3) -> dict:
    """Generate 3 frame-aware commentary variants for a given clip."""
    start, end = float(clip.get("start", 0.0)), float(clip.get("end", 0.0))
    dur = max(1.0, end - start)

    # 1. Extract frames every 2 seconds
    src_path = None
    if clip.get("src") and Path(clip["src"]).is_file():
        src_path = Path(clip["src"])
    elif job.get("source_path") and Path(job["source_path"]).is_file():
        src_path = Path(job["source_path"])
    frames = extract_clip_frames(src_path, start, end, step_seconds=2.0) if src_path else []

    # 2. Gather transcript words in this interval
    words_file = Path(job.get("dir", "")) / "words.json"
    clip_transcript = ""
    if words_file.is_file():
        try:
            words = json.loads(words_file.read_text(encoding="utf-8"))
            in_clip = [w.get("w", "") for w in words if start - 0.5 <= w.get("s", 0.0) <= end + 0.5]
            clip_transcript = " ".join(in_clip)
        except Exception:
            pass

    # 3. If AI is ready, invoke AI via studio/ai.py; otherwise use offline generator
    warning = ""
    if ai.ready():
        prompt = build_commentary_prompt(clip_transcript, dur, tone=tone,
                                         channel_voice=channel_voice, script_style=script_style,
                                         speaking_pace=speaking_pace)
        usage.set_context(job.get("creator"), job.get("title") or "Commentary Generation")
        data, tin, tout, warning = ai.generate_commentary(prompt, frames=frames, duration=dur,
                                                          speaking_pace=speaking_pace)
    else:
        data = make_offline_variants(dur, tone=tone, script_style=script_style, speaking_pace=speaking_pace)
        warning = "AI key not configured; generated sample commentary templates."

    return {
        "tone": tone,
        "channel_voice": channel_voice,
        "script_style": script_style,
        "speaking_pace": speaking_pace,
        "warning": warning,
        "safe_start": data.get("safe_start", {"flagged": False, "reason": "", "suggested_start": 0.0}),
        "selected_variant": 0,
        "variants": data.get("variants", [])
    }
