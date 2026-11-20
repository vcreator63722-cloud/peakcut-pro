"""Tests for Phase 1: Source & Permission Manager.
- Per source video: source URL, creator name, credit text, "permission confirmed" checkbox, proof note/screenshot.
- Auto-build credit line for descriptions and on-screen credit in last 2 seconds.
- Posting blocked while "permission confirmed" is off.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Setup temporary environment
temp_dir = tempfile.mkdtemp(prefix="pc-perm-test-")
os.environ["PEAKCUT_DATA"] = temp_dir
os.environ.update(
    DATABASE_URL="",
    PEAKCUT_ACCOUNTS="1",
    PEAKCUT_PUBLIC_URL="http://127.0.0.1:8799",
    PEAKCUT_FETCH_MODEL="0",
    PEAKCUT_UPDATE_CHECK="0",
    PEAKCUT_REQUIRE_PERMISSION="1"
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from studio import accounts, auth, config, ctx, db, jobs, posting, render, server

db.init()
fails = []

def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)

try:
    # 1. Create a test user
    u, _ = auth._create("creator@example.com", "creator", password_hash="testpass")
    client = TestClient(server.app, base_url="http://127.0.0.1:8799", follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.start_session(u.id))

    # 2. Test build_credit logic
    line, scr = jobs.build_credit("GamerOne", "https://youtube.com/watch?v=12345", "")
    check("build_credit with creator and url", "GamerOne" in line and "12345" in line, True)
    check("build_credit on-screen format", scr, "Credit: @GamerOne")

    line_c, scr_c = jobs.build_credit("GamerOne", "https://youtube.com/watch?v=12345", "Custom Credit: Courtesy of Studio")
    check("build_credit custom override line", line_c, "Custom Credit: Courtesy of Studio")
    check("build_credit custom override screen", scr_c, "Custom Credit: Courtesy of Studio")

    # 3. Test on-screen credit in ASS subtitle generation (last 2 seconds)
    ass_text = render.build_ass([], start=0.0, end=10.0, hook="EPIC MOMENT", style_name="bounce",
                                highlight=[], hook_y=300, cap_y=1380, on_screen_credit="Credit: @GamerOne")
    check("ASS contains Credit style", "Style: Credit" in ass_text, True)
    check("ASS contains on-screen credit event in last 2s (8.00 -> 10.00)",
          "0:00:08.00,0:00:10.00,Credit" in ass_text and "Credit: @GamerOne" in ass_text, True)

    # 4. Create a job and verify default permission fields
    with ctx.as_user(u.id):
        job = jobs.create(
            source={"type": "url", "url": "https://youtube.com/watch?v=gameplay1", "name": "GTA 6 Gameplay"},
            options={"max_clips": 1, "min_len": 15, "max_len": 30},
            enqueue=False
        )
        jid = job["id"]
        perm = job.get("permission")
        check("Job has permission dict", isinstance(perm, dict), True)
        check("Permission confirmed defaults to True", perm.get("confirmed"), True)
        check("Permission source_url initialized", perm.get("source_url"), "https://youtube.com/watch?v=gameplay1")
        check("Permission credit_line generated", bool(perm.get("credit_line")), True)

    # 5. Test GET /api/jobs/{jid}/permission
    r = client.get(f"/api/jobs/{jid}/permission")
    check("GET permission status 200", r.status_code, 200)
    data = r.json()
    check("GET permission has confirmed True", data["confirmed"], True)

    # 6. Test updating permission via POST /api/jobs/{jid}/permission
    r = client.post(f"/api/jobs/{jid}/permission", json={
        "creator_name": "ViceCityKing",
        "source_url": "https://youtube.com/watch?v=gameplay1",
        "credit_text": "",
        "confirmed": False,
        "proof_note": "Obtained via Discord DM with the streamer."
    })
    check("POST permission status 200", r.status_code, 200)
    with ctx.as_user(u.id):
        updated_job = jobs.get(jid)
        u_perm = updated_job["permission"]
        check("Updated creator_name saved", u_perm["creator_name"], "ViceCityKing")
        check("Updated proof_note saved", u_perm["proof_note"], "Obtained via Discord DM with the streamer.")
        check("Auto credit line updated with creator", "ViceCityKing" in u_perm["credit_line"], True)
        check("On-screen credit updated with creator", u_perm["on_screen_credit"], "Credit: @ViceCityKing")

    # 7. Test screenshot proof upload via POST /api/jobs/{jid}/permission/proof
    test_img = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    files = {"file": ("discord_proof.png", io.BytesIO(test_img), "image/png")}
    r = client.post(f"/api/jobs/{jid}/permission/proof", files=files)
    check("Upload proof status 200", r.status_code, 200)
    with ctx.as_user(u.id):
        job_now = jobs.get(jid)
        pf = job_now["permission"].get("proof_file")
        check("proof_file name saved", bool(pf and pf.endswith(".png")), True)

    # Verify proof retrieval via GET /media/{jid}/proof
    r = client.get(f"/media/{jid}/proof")
    check("GET /media/{jid}/proof status 200", r.status_code, 200)
    check("GET /media/{jid}/proof content matches", r.content, test_img)

    # 8. Setup a finished dummy clip and connected account to test posting restrictions
    with ctx.as_user(u.id):
        clip = {
            "id": "c1",
            "start": 0.0,
            "end": 20.0,
            "status": "done",
            "hook": "INSANE DRIFT",
            "title": "GTA 6 Drift Clip",
            "description": "Crazy moment in Vice City",
            "hashtags": ["GTA6", "Gaming"],
            "file": "c1.mp4",
            "cover": "c1.jpg",
            "score": 90
        }
        job_now["clips"] = [clip]
        clips_dir = Path(job_now["dir"]) / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        (clips_dir / "c1.mp4").write_bytes(b"\x00" * 1000)
        jobs.save(job_now)

        # Add a dummy account
        acc = accounts._put({
            "id": "acc-yt-1",
            "user": u.id,
            "platform": "youtube",
            "name": "My Shorts Channel",
            "token": "yt-token"
        })

    # 9. Test: Posting allowed freely without permission gating
    with ctx.as_user(u.id):
        queued_direct = posting.enqueue(job_now, clip, ["acc-yt-1"])
        check("posting.enqueue succeeds without permission gating", len(queued_direct), 1)

    # API call /api/posts should succeed directly
    posting._posts.clear()
    r = client.post("/api/posts", json={"job": jid, "clip": "c1", "accounts": ["acc-yt-1"]})
    check("POST /api/posts succeeds directly (permission gating removed)", r.status_code, 200)
    check("1 post queued", r.json().get("queued"), 1)

finally:
    db.engine.dispose()
    shutil.rmtree(temp_dir, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} checks failed!")
    sys.exit(1)
else:
    print("\nALL PASSED")
    sys.exit(0)
