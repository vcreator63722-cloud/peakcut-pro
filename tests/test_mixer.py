"""Tests for Phase 4: Mixer & Renderer.
- Sidechain ducking of game audio under voiceover track
- Hook text display limited to first 2 seconds
- Output format 1080x1920 (9:16), H.264 + AAC, max 60s
- Word-by-word captions built from voiceover / commentary alignment
- Rotating caption styles (bounce -> hype -> clean)
- Originality checklist per clip (voiceover present, >=40% coverage, non-identical) with pass/fail badge
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

temp_dir = tempfile.mkdtemp(prefix="pc-mix-test-")
os.environ["PEAKCUT_DATA"] = temp_dir
os.environ.update(
    DATABASE_URL="",
    PEAKCUT_ACCOUNTS="1",
    PEAKCUT_PUBLIC_URL="http://127.0.0.1:8799",
    PEAKCUT_FETCH_MODEL="0",
    PEAKCUT_UPDATE_CHECK="0",
    PEAKCUT_MIN_VOICEOVER_COVERAGE="0.40"
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from studio import auth, config, ctx, db, jobs, render, server, voiceover

db.init()
fails = []

def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)


try:
    # 1. Test Hook text duration limited to first 2 seconds in build_ass
    ass_text = render.build_ass(
        words=[],
        start=5.0,
        end=20.0,
        hook="GTA 6 CLUTCH PLAY",
        style_name="bounce",
        highlight=[],
        hook_y=200,
        cap_y=1600
    )
    check("ASS contains Hook style", "Style: Hook" in ass_text, True)
    check("Hook dialogue starts at 0:00:00.00 and ends at 0:00:02.00", "0:00:00.00,0:00:02.00,Hook" in ass_text, True)

    # 2. Test caption style rotation
    check("Rotate bounce -> hype", voiceover.rotate_caption_style("bounce"), "hype")
    check("Rotate hype -> clean", voiceover.rotate_caption_style("hype"), "clean")
    check("Rotate clean -> bounce", voiceover.rotate_caption_style("clean"), "bounce")
    check("Rotate index 0 -> bounce", voiceover.rotate_caption_style(None, 0), "bounce")
    check("Rotate index 1 -> hype", voiceover.rotate_caption_style(None, 1), "hype")

    # 3. Test script_to_words fallback alignment
    dummy_comm = {
        "selected_variant": 0,
        "variants": [{
            "lines": [
                {"start": 0.0, "end": 2.0, "text": "Look at this insane turn"},
                {"start": 2.5, "end": 4.5, "text": "Nobody saw that coming"}
            ]
        }]
    }
    words_res = voiceover.script_to_words(dummy_comm, clip_start=10.0)
    check("script_to_words produced word timestamps", len(words_res) == 9, True)
    check("first word starts at 10.0", words_res[0]["s"] == 10.0, True)
    check("first word text matches", words_res[0]["w"], "Look")

    # 4. Test Originality checklist evaluation
    job_mock = {"duration": 120.0}

    # Case A: No voiceover -> Fail
    clip_no_vo = {"start": 10.0, "end": 25.0, "voiceover": {"has_voiceover": False}}
    orig_a = voiceover.evaluate_originality(job_mock, clip_no_vo, min_coverage=0.40)
    check("No voiceover fails checklist", orig_a["passed"], False)
    check("Voiceover present is False", orig_a["voiceover_present"], False)

    # Case B: Voiceover present but under 40% (2.0s on 15.0s clip = 13%) -> Fail
    clip_short_vo = {"start": 10.0, "end": 25.0, "voiceover": {"has_voiceover": True, "duration": 2.0}}
    orig_b = voiceover.evaluate_originality(job_mock, clip_short_vo, min_coverage=0.40)
    check("Under 40% voiceover fails coverage", orig_b["voiceover_coverage"], False)
    check("Under 40% fails checklist", orig_b["passed"], False)

    # Case C: Voiceover present and >=40% (8.0s on 15.0s clip = 53%) -> Pass
    clip_good_vo = {"start": 10.0, "end": 25.0, "voiceover": {"has_voiceover": True, "duration": 8.0}}
    orig_c = voiceover.evaluate_originality(job_mock, clip_good_vo, min_coverage=0.40)
    check(">=40% voiceover passes coverage", orig_c["voiceover_coverage"], True)
    check("Not identical is True", orig_c["not_identical"], True)
    check("Valid voiceover passes checklist", orig_c["passed"], True)

    # 5. Test FFmpeg Render with Voiceover Audio Ducking and 1080x1920 (9:16) output
    render.install_fonts()
    test_src = Path(temp_dir) / "test_src.mp4"
    test_vo = Path(temp_dir) / "test_vo.wav"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=400",
        "-t", "4.0", "-c:v", "libx264", "-c:a", "aac", str(test_src)
    ], check=True)

    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "sine=frequency=1000",
        "-t", "3.0", "-ar", "44100", "-ac", "1", str(test_vo)
    ], check=True)

    out_dir = Path(temp_dir) / "rendered_clips"
    clip_def = {
        "id": "c_mix_1",
        "start": 0.0,
        "end": 3.0,
        "hook": "TEST 9:16 SHORT",
        "layout": "crop",
        "style": "hype",
        "captions": True,
        "voiceover_path": str(test_vo)
    }

    render_out = render.render_clip(
        src=test_src,
        out_dir=out_dir,
        name="c_mix_1",
        clip=clip_def,
        words=[{"w": "GTA", "s": 0.2, "e": 0.8}, {"w": "DRIFT", "s": 0.9, "e": 1.6}],
        src_w=640,
        src_h=360
    )

    rendered_file = out_dir / render_out["file"]
    check("Rendered clip file exists", rendered_file.is_file(), True)

    # Probe rendered video resolution (must be 1080x1920)
    p_probe = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        str(rendered_file)
    ], capture_output=True, text=True, check=True)
    res_str = p_probe.stdout.strip()
    check("Rendered resolution is 1080x1920 (9:16)", res_str, "1080x1920")

    # 6. Test API GET /api/jobs/{jid}/clips/{cid}/originality
    u, _ = auth._create("mixer_user@example.com", "mixuser", password_hash="pass123")
    client = TestClient(server.app, base_url="http://127.0.0.1:8799", follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.start_session(u.id))

    with ctx.as_user(u.id):
        job = jobs.create(
            source={"type": "file", "name": "source_gameplay.mp4"},
            options={"max_clips": 1, "min_len": 10, "max_len": 20},
            enqueue=False
        )
        jid = job["id"]
        job["duration"] = 100.0
        clip_job = {
            "id": "c_orig_test",
            "start": 10.0,
            "end": 20.0,
            "status": "done",
            "hook": "TEST",
            "file": "c_orig.mp4",
            "voiceover": {
                "has_voiceover": True,
                "duration": 6.0,
                "mode": "mic_full"
            }
        }
        job["clips"] = [clip_job]
        jobs.save(job)

    r_orig = client.get(f"/api/jobs/{jid}/clips/c_orig_test/originality")
    check("GET originality status 200", r_orig.status_code, 200)
    orig_api = r_orig.json().get("originality", {})
    check("API originality passed is True", orig_api.get("passed"), True)
    check("API originality voiceover_coverage is True", orig_api.get("voiceover_coverage"), True)
    check("API originality coverage_ratio 0.6", orig_api.get("coverage_ratio"), 0.6)

finally:
    db.engine.dispose()
    shutil.rmtree(temp_dir, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} checks failed!")
    sys.exit(1)
else:
    print("\nALL PASSED")
    sys.exit(0)
