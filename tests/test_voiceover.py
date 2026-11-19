"""Tests for Phase 3: Voiceover Studio.
- Audio cleaning, noise reduction, loudness normalisation with ffmpeg
- Line-take assembly to synchronized audio track
- Audio file import (WAV/MP3/M4A)
- Optional TTS adapter (free Google TTS & OpenAI TTS)
- Teleprompter & Voiceover REST API endpoints
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

temp_dir = tempfile.mkdtemp(prefix="pc-vo-test-")
os.environ["PEAKCUT_DATA"] = temp_dir
os.environ.update(
    DATABASE_URL="",
    PEAKCUT_ACCOUNTS="1",
    PEAKCUT_PUBLIC_URL="http://127.0.0.1:8799",
    PEAKCUT_FETCH_MODEL="0",
    PEAKCUT_UPDATE_CHECK="0"
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from studio import ai, auth, commentary, config, ctx, db, jobs, server, voiceover

db.init()
fails = []

def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)


try:
    # 1. Generate a dummy test audio file with ffmpeg (sine wave 2.0s)
    test_sine = Path(temp_dir) / "sine_raw.wav"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=2.0",
        "-ar", "44100", "-ac", "1",
        str(test_sine)
    ], check=True)

    # 2. Test clean_voiceover_audio
    clean_out = Path(temp_dir) / "sine_clean.wav"
    dur = voiceover.clean_voiceover_audio(test_sine, clean_out, noise_reduction=True, normalize=True)
    check("clean_voiceover_audio created output file", clean_out.is_file(), True)
    check("cleaned audio duration ~2.0s", 1.8 <= dur <= 2.2, True)

    # 3. Test assemble_takes_to_wav
    take1 = Path(temp_dir) / "take1.wav"
    take2 = Path(temp_dir) / "take2.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=800:duration=1.0", str(take1)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=1200:duration=1.0", str(take2)], check=True)

    assembled_out = Path(temp_dir) / "assembled.wav"
    takes = [
        {"start": 0.0, "end": 1.0, "path": take1},
        {"start": 2.0, "end": 3.0, "path": take2}
    ]
    asm_dur = voiceover.assemble_takes_to_wav(takes, clip_duration=4.0, output_path=assembled_out)
    check("assembled output exists", assembled_out.is_file(), True)
    check("assembled audio duration ~4.0s", 3.7 <= asm_dur <= 4.3, True)

    # 4. Test ai.generate_tts
    try:
        ai.generate_tts("")
        check("generate_tts empty text raises ValueError", False)
    except ValueError:
        check("generate_tts empty text raises ValueError", True)

    try:
        ai.generate_tts("Hello", provider="nonexistent")
        check("generate_tts invalid provider raises ValueError", False)
    except ValueError:
        check("generate_tts invalid provider raises ValueError", True)

    # Mock TTS with fake mp3 bytes to verify handler without network
    original_get = ai.httpx.get
    original_post = ai.httpx.post
    dummy_audio_bytes = test_sine.read_bytes()

    class MockTTSResponse:
        def __init__(self, content, status_code=200):
            self.content = content
            self.status_code = status_code
            self.text = ""

    ai.httpx.get = lambda url, **k: MockTTSResponse(dummy_audio_bytes)
    ai.httpx.post = lambda url, **k: MockTTSResponse(dummy_audio_bytes)

    tts_bytes = ai.generate_tts("Ye moment dekho bhai!", provider="free")
    check("free TTS returns audio bytes", len(tts_bytes) > 0, True)

    # Restore httpx
    ai.httpx.get = original_get
    ai.httpx.post = original_post

    # 5. Test API endpoints
    u, _ = auth._create("vo_user@example.com", "vouser", password_hash="pass123")
    client = TestClient(server.app, base_url="http://127.0.0.1:8799", follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.start_session(u.id))

    with ctx.as_user(u.id):
        job = jobs.create(
            source={"type": "file", "name": "gta6_vo.mp4"},
            options={"max_clips": 1, "min_len": 15, "max_len": 30},
            enqueue=False
        )
        jid = job["id"]
        clip = {
            "id": "c_vo_1",
            "start": 0.0,
            "end": 10.0,
            "status": "done",
            "hook": "TEST VO HOOK",
            "title": "Voiceover Test Clip",
            "file": "c_vo_1.mp4",
            "commentary": {
                "selected_variant": 0,
                "variants": [{
                    "hook": "TEST VO HOOK",
                    "lines": [
                        {"start": 0.0, "end": 4.0, "text": "First line of GTA 6 commentary"},
                        {"start": 5.0, "end": 9.0, "text": "Second line of GTA 6 commentary"}
                    ],
                    "outro": "Outro text"
                }]
            }
        }
        job["clips"] = [clip]
        jobs.save(job)

    # GET /api/jobs/{jid}/clips/{cid}/voiceover (initially empty)
    r_get = client.get(f"/api/jobs/{jid}/clips/c_vo_1/voiceover")
    check("GET voiceover status 200", r_get.status_code, 200)
    check("Initially has_voiceover False or not set", r_get.json().get("voiceover", {}).get("has_voiceover", False), False)

    # POST full take audio recording
    r_post_full = client.post(
        f"/api/jobs/{jid}/clips/c_vo_1/voiceover",
        content=test_sine.read_bytes(),
        headers={"Content-Type": "audio/wav"}
    )
    check("POST full take voiceover status 200", r_post_full.status_code, 200)
    vo_res = r_post_full.json().get("voiceover", {})
    check("has_voiceover is True", vo_res.get("has_voiceover"), True)
    check("mode is mic_full", vo_res.get("mode"), "mic_full")
    check("voiceover file path set", bool(vo_res.get("file")), True)

    # GET /media/{jid}/clips/{cid}/voiceover (serves WAV)
    r_media = client.get(f"/media/{jid}/clips/c_vo_1/voiceover")
    check("GET media voiceover status 200", r_media.status_code, 200)
    check("Media content-type audio/wav", "audio/wav" in r_media.headers.get("content-type", ""), True)

    # POST line-by-line take recording (line 0)
    r_line = client.post(
        f"/api/jobs/{jid}/clips/c_vo_1/voiceover?line_index=0",
        content=take1.read_bytes(),
        headers={"Content-Type": "audio/wav"}
    )
    check("POST line take status 200", r_line.status_code, 200)
    line_vo = r_line.json().get("voiceover", {})
    check("Line takes dict contains line 0", "0" in line_vo.get("line_takes", {}), True)

    # POST import audio file
    import_bytes = take2.read_bytes()
    r_imp = client.post(
        f"/api/jobs/{jid}/clips/c_vo_1/voiceover/import",
        files={"file": ("my_audio.wav", io.BytesIO(import_bytes), "audio/wav")}
    )
    check("POST import audio status 200", r_imp.status_code, 200)
    imp_vo = r_imp.json().get("voiceover", {})
    check("Import mode is imported", imp_vo.get("mode"), "imported")
    check("Source filename saved", imp_vo.get("source_filename"), "my_audio.wav")

    # POST optional TTS voiceover (with mock)
    ai.httpx.get = lambda url, **k: MockTTSResponse(dummy_audio_bytes)
    ai.httpx.post = lambda url, **k: MockTTSResponse(dummy_audio_bytes)
    r_tts = client.post(
        f"/api/jobs/{jid}/clips/c_vo_1/voiceover/tts",
        json={"provider": "free"}
    )
    check("POST TTS voiceover status 200", r_tts.status_code, 200)
    tts_vo = r_tts.json().get("voiceover", {})
    check("TTS mode is tts", tts_vo.get("mode"), "tts")

    # DELETE /api/jobs/{jid}/clips/{cid}/voiceover
    r_del = client.delete(f"/api/jobs/{jid}/clips/c_vo_1/voiceover")
    check("DELETE voiceover status 200", r_del.status_code, 200)
    r_get_after = client.get(f"/api/jobs/{jid}/clips/c_vo_1/voiceover")
    check("After delete has_voiceover is False", r_get_after.json().get("voiceover", {}).get("has_voiceover"), False)

finally:
    db.engine.dispose()
    shutil.rmtree(temp_dir, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} checks failed!")
    sys.exit(1)
else:
    print("\nALL PASSED")
    sys.exit(0)
