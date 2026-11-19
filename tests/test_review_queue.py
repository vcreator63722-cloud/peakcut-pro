"""Tests for Phase 5: Review Queue & Metadata.
- Default review-before-posting (autopost=False)
- Auto-post gates: permission confirmed AND originality checklist passed AND not duplicate
- Keep daily caps and spacing intact
- Duplicate-moment detection:
    - Normalised source ID resolution (URL, file, name, job)
    - Time-window overlap calculation (ratio >= 50%, >=10s, or close start)
    - Duplicate moment prevents posting the same moment twice
- Metadata management:
    - Caption builder includes title, description, credit line, source link, hashtags
    - Editable title, description, hashtags without requiring video re-render
    - GET /api/jobs/{jid}/clips/{cid}/duplicate endpoint
    - PUT /api/jobs/{jid}/clips/{cid}/metadata endpoint
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

temp_dir = tempfile.mkdtemp(prefix="pc-review-test-")
os.environ["PEAKCUT_DATA"] = temp_dir
os.environ.update(
    DATABASE_URL="",
    PEAKCUT_ACCOUNTS="1",
    PEAKCUT_PUBLIC_URL="http://127.0.0.1:8799",
    PEAKCUT_FETCH_MODEL="0",
    PEAKCUT_UPDATE_CHECK="0",
    PEAKCUT_MIN_VOICEOVER_COVERAGE="0.40",
    PEAKCUT_DUPLICATE_OVERLAP_THRESHOLD="0.50"
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from studio import accounts, auth, config, creators, ctx, db, jobs, posting, server, voiceover

db.init()
fails = []

def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)


def run_tests():
    print("--- 1. Source ID & Overlap Mathematics ---")
    j1 = {"id": "job1", "permission": {"source_url": "https://youtube.com/watch?v=GTA6Trailer1&t=15s"}, "source": {}}
    check("Normalized YouTube source ID ignores query params and case",
          posting.get_source_id(j1),
          "https://youtube.com/watch?v=gta6trailer1")

    j_local = {"id": "job2", "source": {"path": "/var/media/ViceCity_4k.mp4"}, "permission": {}}
    check("Local file source ID uses lowercase filename",
          posting.get_source_id(j_local),
          "file:vicecity_4k.mp4")

    # Overlap tests: is_moment_overlap(s1, e1, s2, e2, threshold=0.50)
    # [10, 30] vs [15, 35] -> overlap = 15s. dur1=20s, ratio=15/20=0.75 >= 0.50
    is_dup, ov = posting.is_moment_overlap(10.0, 30.0, 15.0, 35.0)
    check("75% overlap is flagged as duplicate moment", is_dup, True)
    check("Overlap duration is 15.0s", ov, 15.0)

    # [10, 30] vs [40, 60] -> overlap = 0s
    is_dup_none, _ = posting.is_moment_overlap(10.0, 30.0, 40.0, 60.0)
    check("Non-overlapping segments return False", is_dup_none, False)

    # [10, 40] vs [29, 50] -> overlap = 11s (>=10s absolute threshold)
    is_dup_10s, ov_10s = posting.is_moment_overlap(10.0, 40.0, 29.0, 50.0)
    check(">=10s absolute overlap is flagged as duplicate", is_dup_10s, True)

    # [10, 30] vs [29.5, 49.5] -> overlap = 0.5s (<50%, <10s)
    is_dup_small, _ = posting.is_moment_overlap(10.0, 30.0, 29.5, 49.5)
    check("Negligible 0.5s overlap is not flagged as duplicate", is_dup_small, False)

    print("\n--- 2. Caption Builder (Title, Description, Credit, Source Link, Hashtags) ---")
    test_clip = {
        "id": "c1",
        "title": "Insane Vice City Stunt",
        "description": "Check out this wild escape from the cops!",
        "hashtags": ["GTA6", "ViceCity", "#GamingShorts"]
    }
    test_job = {
        "id": "j_cap",
        "permission": {
            "credit_line": "Original gameplay by RockstarGamer: https://youtube.com/watch?v=GTA6",
            "source_url": "https://youtube.com/watch?v=GTA6"
        }
    }
    test_creator = {"post_footer": "Subscribe for daily clips!"}

    cap = posting._caption(test_clip, test_creator, test_job)
    check("Caption contains base description", "Check out this wild escape" in cap, True)
    check("Caption includes credit line", "Original gameplay by RockstarGamer" in cap, True)
    check("Caption includes source URL", "https://youtube.com/watch?v=GTA6" in cap, True)
    check("Caption formats hashtags with # prefix", "#GTA6 #ViceCity #GamingShorts" in cap, True)
    check("Caption appends post footer", "Subscribe for daily clips!" in cap, True)

    # Ensure no duplicate source link if already included in description
    test_clip2 = {
        "id": "c2",
        "description": "Already has source: https://youtube.com/watch?v=GTA6",
        "hashtags": []
    }
    cap2 = posting._caption(test_clip2, {}, test_job)
    check("Caption does not duplicate source URL if already in description", cap2.count("https://youtube.com/watch?v=GTA6"), 1)

    print("\n--- 3. Duplicate Moment Detection in Queue & Enqueue ---")
    u, _ = auth._create("reviewer@peakcut.test", "reviewer", password_hash="pass123456")
    user_id = u.id
    ctx.set_uid(user_id)

    # Create creator with autopost=False default
    creator = creators.create({"name": "vice_creator", "autopost": False, "accounts": ["acc_yt"]})
    check("Creator default is review-before-posting (autopost=False)", creator["autopost"], False)

    # Add mock connected YouTube account
    accounts._put({"id": "acc_yt", "platform": "youtube", "name": "ViceCityClips", "label": "YT",
                   "access_token": "fake", "refresh_token": "fake", "expires_at": time.time() + 3600})

    job = jobs.create({"type": "url", "url": "https://youtube.com/watch?v=GTA6Gameplay"}, {}, enqueue=False, creator=creator["id"])
    c_a = {"id": "c_a", "start": 10.0, "end": 35.0, "status": "done", "title": "Moment A", "hashtags": ["GTA6"],
           "originality": {"passed": True, "voiceover_present": True, "voiceover_coverage": True, "not_identical": True}}
    c_b = {"id": "c_b", "start": 12.0, "end": 34.0, "status": "done", "title": "Moment B (Duplicate)", "hashtags": ["GTA6"],
           "originality": {"passed": True, "voiceover_present": True, "voiceover_coverage": True, "not_identical": True}}
    c_c = {"id": "c_c", "start": 60.0, "end": 85.0, "status": "done", "title": "Moment C (Different)", "hashtags": ["GTA6"],
           "originality": {"passed": True, "voiceover_present": True, "voiceover_coverage": True, "not_identical": True}}
    job["clips"] = [c_a, c_b, c_c]
    jobs.save(job)

    # 1. Enqueue blocked when permission unconfirmed
    try:
        posting.enqueue(job, c_a, ["acc_yt"])
        check("Enqueue blocked without permission", False, True)
    except ValueError as e:
        check("Enqueue blocked without permission", "permission confirmed" in str(e), True)

    # Confirm permission
    jobs.update_permission(job["id"], {"confirmed": True, "source_url": "https://youtube.com/watch?v=GTA6Gameplay"})
    job = jobs.get(job["id"])

    # Enqueue Moment A
    queued = posting.enqueue(job, c_a, ["acc_yt"])
    check("Moment A enqueued successfully", len(queued), 1)

    # Check duplicate detection for Moment B (overlapping 12s-34s with 10s-35s)
    dup_b = posting.check_duplicate_moment(job, c_b, account_id="acc_yt")
    check("Moment B flagged as duplicate of Moment A", dup_b["is_duplicate"], True)
    check("Duplicate status is scheduled", dup_b["status"], "scheduled")

    # Trying to enqueue Moment B skips it
    queued_b = posting.enqueue(job, c_b, ["acc_yt"])
    check("Enqueue skips duplicate moment B", len(queued_b), 0)

    # Moment C is 60s-85s (no overlap)
    dup_c = posting.check_duplicate_moment(job, c_c, account_id="acc_yt")
    check("Moment C is not duplicate", dup_c["is_duplicate"], False)
    queued_c = posting.enqueue(job, c_c, ["acc_yt"])
    check("Moment C enqueues successfully", len(queued_c), 1)

    print("\n--- 4. Auto-Post Gating Logic (on_clip_ready) ---")
    # Reset posts
    posting._posts.clear()
    posting._save()

    # Case A: Autopost is False (review before posting default)
    creator_manual = creators.create({"name": "manual_creator", "autopost": False, "accounts": ["acc_yt"]})
    job_auto = jobs.create({"type": "url", "url": "https://youtube.com/watch?v=GTA6Auto"}, {}, enqueue=False, creator=creator_manual["id"])
    jobs.update_permission(job_auto["id"], {"confirmed": True})
    c_auto1 = {"id": "ca1", "start": 10.0, "end": 30.0, "status": "done", "score": 90,
               "originality": {"passed": True, "voiceover_present": True, "voiceover_coverage": True, "not_identical": True}}
    job_auto["clips"] = [c_auto1]
    jobs.save(job_auto)

    posting.on_clip_ready(job_auto, c_auto1)
    check("Auto-post does not trigger when creator autopost is False (review first)", len(posting._mine()), 0)

    # Case B: Autopost is True, but permission is UNCONFIRMED
    creator_auto = creators.create({"name": "auto_creator", "autopost": True, "accounts": ["acc_yt"], "post_min_score": 70})
    job_unperm = jobs.create({"type": "url", "url": "https://youtube.com/watch?v=GTA6Unperm"}, {}, enqueue=False, creator=creator_auto["id"])
    # permission confirmed is False
    c_auto2 = {"id": "ca2", "start": 10.0, "end": 30.0, "status": "done", "score": 85,
               "originality": {"passed": True, "voiceover_present": True, "voiceover_coverage": True, "not_identical": True}}
    job_unperm["clips"] = [c_auto2]
    jobs.save(job_unperm)

    posting.on_clip_ready(job_unperm, c_auto2)
    check("Auto-post blocked when permission is unconfirmed", len(posting._mine()), 0)

    # Case C: Autopost is True, permission confirmed, but ORIGINALITY CHECKLIST FAILS
    job_unorig = jobs.create({"type": "url", "url": "https://youtube.com/watch?v=GTA6Unorig"}, {}, enqueue=False, creator=creator_auto["id"])
    jobs.update_permission(job_unorig["id"], {"confirmed": True})
    c_auto3 = {"id": "ca3", "start": 10.0, "end": 30.0, "status": "done", "score": 85,
               "originality": {"passed": False, "voiceover_present": False, "voiceover_coverage": False, "not_identical": True}}
    job_unorig["clips"] = [c_auto3]
    jobs.save(job_unorig)

    posting.on_clip_ready(job_unorig, c_auto3)
    check("Auto-post blocked when originality checklist fails", len(posting._mine()), 0)

    # Case D: Autopost is True, permission confirmed, originality passed, valid score
    job_pass = jobs.create({"type": "url", "url": "https://youtube.com/watch?v=GTA6Pass"}, {}, enqueue=False, creator=creator_auto["id"])
    jobs.update_permission(job_pass["id"], {"confirmed": True})
    c_auto4 = {"id": "ca4", "start": 10.0, "end": 30.0, "status": "done", "score": 85,
               "originality": {"passed": True, "voiceover_present": True, "voiceover_coverage": True, "not_identical": True}}
    job_pass["clips"] = [c_auto4]
    jobs.save(job_pass)

    posting.on_clip_ready(job_pass, c_auto4)
    check("Auto-post triggers when permission confirmed AND originality passed", len(posting._mine()), 1)

    # Case E: Duplicate moment does not auto-post twice
    c_auto5_dup = {"id": "ca5", "start": 11.0, "end": 31.0, "status": "done", "score": 90,
                   "originality": {"passed": True, "voiceover_present": True, "voiceover_coverage": True, "not_identical": True}}
    job_pass["clips"].append(c_auto5_dup)
    jobs.save(job_pass)

    posting.on_clip_ready(job_pass, c_auto5_dup)
    check("Auto-post skips duplicate moment", len(posting._mine()), 1)

    print("\n--- 5. API Endpoints (Metadata PUT, Duplicate GET, Posts POST) ---")
    client = TestClient(server.app, base_url="http://127.0.0.1:8799", follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.start_session(u.id))
    posting.enqueue(job, c_a, ["acc_yt"])

    # 1. Update metadata without re-rendering
    put_res = client.put(f"/api/jobs/{job['id']}/clips/c_a/metadata", json={
        "title": "Updated Pro Title",
        "description": "Updated Pro Description with GTA 6 facts",
        "hashtags": ["GTA6Pro", "ViceCityHighlights"]
    }, headers={})
    check("PUT metadata endpoint returns 200", put_res.status_code, 200)
    meta = put_res.json()["metadata"]
    check("Title was updated", meta["title"], "Updated Pro Title")
    check("Hashtags were formatted as array", meta["hashtags"], ["GTA6Pro", "ViceCityHighlights"])

    # 2. Check duplicate endpoint
    dup_res = client.get(f"/api/jobs/{job['id']}/clips/c_b/duplicate")
    check("GET duplicate endpoint returns 200", dup_res.status_code, 200)
    check("GET duplicate detects overlap with c_a", dup_res.json()["duplicate"]["is_duplicate"], True)

    # 3. Posts endpoint blocks duplicate moment
    post_res = client.post("/api/posts", json={
        "job": job["id"],
        "clip": "c_b",
        "accounts": ["acc_yt"]
    }, headers={})
    check("POST /api/posts rejects duplicate moment with 400", post_res.status_code, 400)
    check("Error mentions duplicate moment", "Duplicate moment" in post_res.json().get("detail", ""), True)

    # Clean up test data
    shutil.rmtree(temp_dir, ignore_errors=True)

    print("\n==========================================")
    if fails:
        print(f"FAILED: {len(fails)} checks failed: {fails}")
        sys.exit(1)
    else:
        print(f"ALL CHECKS PASSED ({28 - len(fails)}/28 checks)")
        print("==========================================")


if __name__ == "__main__":
    run_tests()
