"""Tests for Phase 2: Frame-aware Commentary Writer.
- Extracts frames every 2 seconds + transcript
- Strict JSON output: hook, timed lines, outro, title, description, hashtags
- Controls: tone, channel voice sample, script style (Devanagari Hindi vs Roman Hinglish)
- Speaking pace enforcement
- 3 variants generated, pick and edit in UI
- Safe-start check for violence in first 7s
- Vision fallback to transcript-only with warning
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

temp_dir = tempfile.mkdtemp(prefix="pc-comm-test-")
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
from studio import ai, auth, commentary, config, ctx, db, jobs, server

db.init()
fails = []

def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)

try:
    # 1. Test prompt generation with tone, voice, and style controls
    p_dev = commentary.build_commentary_prompt("streamer got shot", 20.0, tone="roast",
                                               channel_voice="Bhai kya kar raha hai tu",
                                               script_style="devanagari_hindi", speaking_pace=2.3)
    check("Prompt includes ROAST tone", "ROAST" in p_dev, True)
    check("Prompt includes channel voice sample", "Bhai kya kar raha hai tu" in p_dev, True)
    check("Prompt mentions Devanagari script style", "Devanagari" in p_dev, True)
    check("Prompt specifies [CHECK] rule", "[CHECK]" in p_dev, True)
    check("Prompt specifies SAFE-START CHECK rule", "SAFE-START CHECK" in p_dev, True)

    p_rom = commentary.build_commentary_prompt("insane drift", 15.0, tone="funny",
                                               channel_voice="", script_style="roman_hinglish",
                                               speaking_pace=2.0)
    check("Prompt mentions Roman Hinglish style", "Roman Hinglish" in p_rom, True)

    # 2. Test offline template generator (structure & 3 variants)
    off_data = commentary.make_offline_variants(25.0, tone="hype", script_style="devanagari_hindi", speaking_pace=2.3)
    check("Offline data contains safe_start", "safe_start" in off_data, True)
    check("Offline data contains 3 variants", len(off_data.get("variants", [])), 3)

    v1 = off_data["variants"][0]
    check("Variant has hook", bool(v1.get("hook")), True)
    check("Variant has timed lines", len(v1.get("lines", [])) >= 2, True)
    check("Line has start, end, text", all("start" in l and "end" in l and "text" in l for l in v1["lines"]), True)
    check("Variant has outro", bool(v1.get("outro")), True)
    check("Variant has title", bool(v1.get("title")), True)
    check("Variant has description", bool(v1.get("description")), True)
    check("Variant has hashtags", isinstance(v1.get("hashtags"), list) and len(v1["hashtags"]) > 0, True)

    # 3. Test vision fallback mechanism in studio/ai.py
    # When frames are sent to a mock or unsupported provider, call_with_frames falls back with warning
    dummy_frame = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb"
    out_f, tin, tout, warn = ai.call_with_frames("test-model", "test prompt", {"type": "OBJECT"}, frames=[dummy_frame])
    check("call_with_frames produces output or handles fallback", isinstance(out_f, (dict, list, type(None))), True)

    # 4. Test API endpoints
    u, _ = auth._create("commentator@example.com", "commentator", password_hash="pass123")
    client = TestClient(server.app, base_url="http://127.0.0.1:8799", follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.start_session(u.id))

    with ctx.as_user(u.id):
        job = jobs.create(
            source={"type": "file", "name": "gta6_gameplay.mp4"},
            options={"max_clips": 1, "min_len": 15, "max_len": 30},
            enqueue=False
        )
        jid = job["id"]
        clip = {
            "id": "c_comm_1",
            "start": 5.0,
            "end": 25.0,
            "status": "done",
            "hook": "TEST HOOK",
            "title": "Old Title",
            "description": "Old Desc",
            "hashtags": ["GTA6"],
            "file": "c1.mp4",
            "cover": "c1.jpg",
            "score": 85
        }
        job["clips"] = [clip]
        job_dir = Path(job["dir"])
        (job_dir / "words.json").write_text(json.dumps([
            {"s": 5.5, "e": 7.0, "w": "look"},
            {"s": 7.2, "e": 9.0, "w": "at"},
            {"s": 9.1, "e": 11.0, "w": "this"}
        ]), encoding="utf-8")
        jobs.save(job)

    # 5. POST /api/jobs/{jid}/clips/{cid}/commentary
    r = client.post(f"/api/jobs/{jid}/clips/c_comm_1/commentary", json={
        "tone": "funny",
        "script_style": "roman_hinglish",
        "speaking_pace": 2.5,
        "channel_voice": "Are bhai bhai bhai!"
    })
    check("POST commentary status 200", r.status_code, 200)
    res_data = r.json()
    check("Response has ok True", res_data.get("ok"), True)
    comm = res_data.get("commentary", {})
    check("Commentary tone saved", comm.get("tone"), "funny")
    check("Commentary script_style saved", comm.get("script_style"), "roman_hinglish")
    check("Commentary has 3 variants", len(comm.get("variants", [])), 3)

    # 6. GET /api/jobs/{jid}/clips/{cid}/commentary
    r_get = client.get(f"/api/jobs/{jid}/clips/c_comm_1/commentary")
    check("GET commentary status 200", r_get.status_code, 200)
    check("GET commentary matches saved", r_get.json()["commentary"]["tone"], "funny")

    # 7. PUT /api/jobs/{jid}/clips/{cid}/commentary (edit script line, pick variant, apply to clip)
    variants = comm["variants"]
    variants[1]["lines"][0]["text"] = "MODIFIED FIRST LINE FOR GTA 6"
    variants[1]["hook"] = "NEW APPLIED HOOK"
    variants[1]["title"] = "NEW APPLIED TITLE"

    r_put = client.put(f"/api/jobs/{jid}/clips/c_comm_1/commentary", json={
        "selected_variant": 1,
        "variants": variants,
        "apply_to_clip": True
    })
    check("PUT commentary status 200", r_put.status_code, 200)
    with ctx.as_user(u.id):
        updated_j = jobs.get(jid)
        c_up = next(c for c in updated_j["clips"] if c["id"] == "c_comm_1")
        check("Selected variant updated to 1", c_up["commentary"]["selected_variant"], 1)
        check("Modified script line saved", c_up["commentary"]["variants"][1]["lines"][0]["text"], "MODIFIED FIRST LINE FOR GTA 6")
        check("Hook applied to clip", c_up["hook"], "NEW APPLIED HOOK")
        check("Title applied to clip", c_up["title"], "NEW APPLIED TITLE")

finally:
    db.engine.dispose()
    shutil.rmtree(temp_dir, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} checks failed!")
    sys.exit(1)
else:
    print("\nALL PASSED")
    sys.exit(0)
