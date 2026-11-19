"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import secrets
import threading
import time
from pathlib import Path

import json
import re

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def forget(uid: str):
    """Drop everything queued or recorded for a deleted account, so nothing is posted or retried for it any more."""
    with _lock:
        for p in _posts:
            if p.get("user") == uid:
                p["status"] = "deleted"          # the posting loop may be holding this one already: it then skips it
        _posts[:] = [p for p in _posts if p.get("user") != uid]
        _save()


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error")}
        jobs.save(job)


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def get_source_id(job: dict) -> str:
    """Returns a normalized identifier for the source video across jobs and sessions."""
    if not job:
        return ""
    perm = job.get("permission") or {}
    src = job.get("source") or {}
    url = (perm.get("source_url") or src.get("url") or "").strip()
    if url:
        return url.split("&t=")[0].split("?t=")[0].rstrip("/").lower()
    path = (src.get("path") or job.get("source_path") or "").strip()
    if path:
        return "file:" + Path(path).name.lower()
    name = (src.get("name") or job.get("title") or "").strip()
    if name:
        return "name:" + name.lower()
    return "job:" + str(job.get("id"))


def is_moment_overlap(start1: float, end1: float, start2: float, end2: float, threshold_ratio: float | None = None) -> tuple[bool, float]:
    """Calculates if two clip time segments overlap significantly enough to be the same moment."""
    if threshold_ratio is None:
        threshold_ratio = getattr(config, "DUPLICATE_OVERLAP_THRESHOLD", 0.50)
    s1, e1 = float(start1), float(end1)
    s2, e2 = float(start2), float(end2)
    dur1 = max(0.1, e1 - s1)
    dur2 = max(0.1, e2 - s2)
    overlap = max(0.0, min(e1, e2) - max(s1, s2))
    if overlap <= 0.0:
        return False, 0.0
    ratio1 = overlap / dur1
    ratio2 = overlap / dur2
    # Flag as duplicate if:
    # 1. Overlap is >= threshold_ratio of either clip duration
    # 2. Overlap is 10s or more
    # 3. Start points are within 2.5s of each other and overlap is at least 3.0s
    if ratio1 >= threshold_ratio or ratio2 >= threshold_ratio or overlap >= 10.0 or (abs(s1 - s2) <= 2.5 and overlap >= 3.0):
        return True, round(overlap, 2)
    return False, round(overlap, 2)


def check_duplicate_moment(job: dict, clip: dict, account_id: str | None = None) -> dict:
    """Checks if this clip represents a moment already scheduled or posted.
    If account_id is given, checks posts for that account; otherwise checks all user's posts.
    Returns: {"is_duplicate": bool, "status": str|None, "reason": str, "matched_post": dict|None}
    """
    if not job or not clip:
        return {"is_duplicate": False, "status": None, "reason": "", "matched_post": None}
    cur_source = get_source_id(job)
    cur_start = float(clip.get("start", 0))
    cur_end = float(clip.get("end", 0))
    cur_cid = clip.get("id")
    cur_jid = job.get("id")

    active_posts = [p for p in _mine() if p.get("status") in OPEN + ("posted",)]
    if account_id:
        active_posts = [p for p in active_posts if p.get("account") == account_id]

    for p in active_posts:
        # 1. Exact clip already posted or queued
        if p.get("job") == cur_jid and p.get("clip") == cur_cid:
            return {
                "is_duplicate": True,
                "status": p.get("status"),
                "reason": f"Exact clip was already {p.get('status')} to {p.get('account_name', 'account')}.",
                "matched_post": p
            }

        # 2. Source matching & moment overlap check
        p_src = p.get("source_id")
        p_start = p.get("start")
        p_end = p.get("end")

        if not p_src or p_start is None or p_end is None:
            pj = jobs.get(p.get("job"))
            if pj:
                p_src = get_source_id(pj)
                pc = next((c for c in pj.get("clips", []) if c["id"] == p.get("clip")), None)
                if pc:
                    p_start = float(pc.get("start", 0))
                    p_end = float(pc.get("end", 0))

        if p_src and cur_source and p_src == cur_source and p_start is not None and p_end is not None:
            is_dup, overlap_sec = is_moment_overlap(cur_start, cur_end, p_start, p_end)
            if is_dup:
                return {
                    "is_duplicate": True,
                    "status": p.get("status"),
                    "reason": f"Moment overlaps ({overlap_sec}s) with a clip already {p.get('status')} to {p.get('account_name', 'account')}.",
                    "matched_post": p
                }

    return {"is_duplicate": False, "status": None, "reason": "", "matched_post": None}


def enqueue(job, clip, account_ids, now=False) -> list:
    perm = job.get("permission") or {}
    if not perm.get("confirmed"):
        raise ValueError("Posting is blocked while 'permission confirmed' is off.")
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            # Duplicate moment check for this specific account
            dup = check_duplicate_moment(job, clip, account_id=aid)
            if dup["is_duplicate"]:
                continue  # never post the same moment to the same account twice
            due = time.time() if now else _next_slot(aid, c["post_gap_min"], c["daily_cap"])
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None,
                 "source_id": get_source_id(job), "start": float(clip.get("start", 0)), "end": float(clip.get("end", 0))}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    # 1. Review before posting default: creator autopost must be True
    c = creators.get(job.get("creator"))
    if not c or not c.get("autopost") or not c.get("accounts"):
        return

    # 2. Source permission must be confirmed
    perm = job.get("permission") or {}
    if not perm.get("confirmed"):
        return

    # 3. Originality checklist must pass
    orig = clip.get("originality")
    if not orig:
        from . import voiceover
        orig = voiceover.evaluate_originality(job, clip, getattr(config, "MIN_VOICEOVER_COVERAGE", 0.40))
        clip["originality"] = orig
        jobs.save(job)
    if not orig.get("passed"):
        return  # Originality checklist failed; requires review before posting

    # 4. Duplicate moment check
    dup = check_duplicate_moment(job, clip)
    if dup["is_duplicate"]:
        return  # Duplicate moment detected; do not auto-post

    # 5. Score threshold
    if clip.get("score", 0) < c.get("post_min_score", 0):
        return  # includes hand-made clips (score 0): those wait for you to press Post

    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator, job=None):
    raw_tags = clip.get("hashtags", [])
    if isinstance(raw_tags, str):
        tags_list = [t.lstrip("#").strip() for t in re.split(r"[\s,]+", raw_tags) if t.lstrip("#").strip()]
    elif isinstance(raw_tags, list):
        tags_list = [str(t).lstrip("#").strip() for t in raw_tags if str(t).lstrip("#").strip()]
    else:
        tags_list = []
    tags = " ".join("#" + h for h in tags_list)

    desc = (clip.get("description") or "").strip()
    if job:
        perm = job.get("permission") or {}
        c_line = (perm.get("credit_line") or "").strip()
        s_url = (perm.get("source_url") or job.get("source", {}).get("url") or "").strip()
        if c_line and c_line not in desc and (not s_url or s_url not in desc):
            desc = (desc + "\n\n" + c_line).strip()
        if s_url and s_url not in desc and s_url not in c_line:
            desc = (desc + f"\n\nSource: {s_url}").strip()

    footer = creator.get("post_footer", "") if creator else ""
    return "\n\n".join(x for x in (desc, tags, footer) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        perm = job.get("permission") or {}
        if not perm.get("confirmed"):
            raise RuntimeError("Posting is blocked while 'permission confirmed' is off.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator, job)
        if acc["platform"] == "youtube":
            rid, url = accounts.youtube_upload(acc, path, title, cap + "\n\n#Shorts", clip.get("hashtags", []), creator["privacy"])
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    while True:
        time.sleep(15)
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
