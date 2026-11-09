#!/usr/bin/env python3
"""
MatRob Garmin Sender
====================

Runs in GitHub Actions in your own PRIVATE repository. The MatRob Running
Generator app drops a job file into inbox/ and starts this workflow; this
script logs in to Garmin Connect, uploads each workout, puts it on its date in
your Garmin calendar, and writes a result file the app reads back.

Environment:
  GARMIN_EMAIL, GARMIN_PASSWORD   repository secrets (set by the app)
  MODE        "send" (default), "login" (just test the login),
              "clean_sent" (delete every workout this app sent) or
              "clean_all" (delete every workout in your Garmin workouts library)
  JOB         job id; reads inbox/<JOB>.json, writes results/<JOB>.json
  MFA_CODE    Garmin verification code, when a previous run asked for one
"""

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INBOX, RESULTS, STATE = ROOT / "inbox", ROOT / "results", ROOT / "state"
LOGIN_DIR = Path(os.path.expanduser("~/.garminconnect"))        # kept between runs by actions/cache
PENDING = Path(os.path.expanduser("~/.garmin-pending/mfa.json"))  # half-finished 2-step login
os.environ.setdefault("GARMINTOKENS", str(LOGIN_DIR))
PAUSE = float(os.getenv("GARMIN_PAUSE", "0.6"))
APP_TAG = "MatRob"

_redact = [os.getenv("GARMIN_EMAIL") or "", os.getenv("GARMIN_PASSWORD") or ""]


def clean(msg):
    msg = re.sub(r"https?://\S+", "<url>", str(msg))
    for s in _redact:
        if s and len(s) >= 4:
            msg = msg.replace(s, "***")
    return msg[:300]


def log(msg):
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {clean(msg)}", flush=True)


def gh(cmd):
    if os.getenv("GITHUB_ACTIONS"):
        print(cmd, flush=True)


def write_result(job, data):
    RESULTS.mkdir(exist_ok=True)
    data["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    (RESULTS / f"{job}.json").write_text(json.dumps(data, indent=1))
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## {data.get('status')}\n\n{data.get('message', '')}\n")


class NeedsCode(Exception):
    pass


def connect():
    from garminconnect import Garmin
    code = re.sub(r"\D", "", os.getenv("MFA_CODE") or "")
    if code:
        gh(f"::add-mask::{code}")
        if not PENDING.exists():
            raise SystemExit("That code arrived but no Garmin login was waiting for it. Tap Send again to get a new code.")
        state = json.loads(PENDING.read_text())
        api = Garmin()
        try:
            api.resume_login(state, code)
        except Exception as e:
            PENDING.unlink(missing_ok=True)
            raise SystemExit(f"Garmin didn't accept that code (it may have expired). Try again to get a new one. ({clean(e)})")
        PENDING.unlink(missing_ok=True)
        save_tokens(api)
        try:
            fresh = Garmin()
            fresh.login(str(LOGIN_DIR))
            return fresh
        except Exception:
            return api

    if LOGIN_DIR.exists() and any(LOGIN_DIR.iterdir()):
        try:
            api = Garmin()
            api.login(str(LOGIN_DIR))
            log("Logged in with saved Garmin login.")
            return api
        except Exception as e:
            log(f"Saved login didn't work ({clean(e)}); using email and password.")

    email, password = os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise SystemExit("Your Garmin email/password aren't saved yet. Open Send to Garmin in the app and connect again.")
    api = Garmin(email, password, return_on_mfa=True)
    try:
        res = api.login(str(LOGIN_DIR))
    except Exception as e:
        t = str(e)
        if any(k in t for k in ("401", "403", "Unauthorized", "credentials", "Authentication", "password")):
            raise SystemExit("Garmin rejected the email or password. Check them and connect again.")
        raise
    if isinstance(res, (tuple, list)) and res and res[0] == "needs_mfa":
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(json.dumps(res[1]))
        raise NeedsCode()
    save_tokens(api)
    log("Logged in with email and password.")
    return api


def save_tokens(api):
    for obj in (api, getattr(api, "client", None), getattr(api, "garth", None)):
        for meth in ("dump", "save_tokens", "save"):
            fn = getattr(obj, meth, None) if obj is not None else None
            if callable(fn):
                try:
                    fn(str(LOGIN_DIR))
                except Exception:
                    pass


def call(fn, *args):
    delay = 20
    for _ in range(6):
        try:
            time.sleep(PAUSE)
            return fn(*args)
        except Exception as e:
            t = f"{type(e).__name__} {e}"
            if "429" in t or "Too Many" in t or "TooMany" in t:
                log(f"Garmin asked us to slow down; waiting {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            raise
    raise RuntimeError("Garmin kept rate-limiting; try again later")


def pick_id(res, *keys):
    if isinstance(res, dict):
        for k in keys:
            if res.get(k) is not None:
                return res[k]
    return None


def full_name(api):
    try:
        n = api.get_full_name()
        return n if isinstance(n, str) else None
    except Exception:
        return None


def send(api, job):
    path = INBOX / f"{job}.json"
    if not path.exists():
        raise SystemExit("The workouts didn't reach GitHub. Please tap Send again.")
    data = json.loads(path.read_text())
    workouts = data.get("workouts") or []
    today = dt.date.today().isoformat()
    sent_path = STATE / "sent.json"
    sent = json.loads(sent_path.read_text()) if sent_path.exists() else []

    removed = 0
    if data.get("replace"):
        keep = []
        for s in sent:
            if (s.get("date") or "9999") >= today:
                try:
                    if s.get("scheduleId"):
                        try:
                            call(api.unschedule_workout, s["scheduleId"])
                        except Exception:
                            pass
                    call(api.delete_workout, s["workoutId"])
                    removed += 1
                except Exception as e:
                    log(f"Couldn't remove an old workout: {clean(e)}")
                    keep.append(s)
            else:
                keep.append(s)
        sent = keep
        log(f"Removed {removed} upcoming workouts sent earlier.")

    uploaded, scheduled, skipped, errors = 0, 0, 0, []
    for i, w in enumerate(workouts, 1):
        date = w.get("date")
        if date and date < today and not data.get("includePast"):
            skipped += 1
            continue
        workout = w.get("workout")
        if isinstance(workout, str):
            workout = json.loads(workout)
        try:
            res = call(api.upload_workout, workout)
            wid = pick_id(res, "workoutId", "id")
            uploaded += 1
            rec = {"workoutId": wid, "name": workout.get("workoutName"), "date": date, "plan": data.get("plan")}
            if date and wid:
                try:
                    sres = call(api.schedule_workout, wid, date)
                    rec["scheduleId"] = pick_id(sres, "workoutScheduleId", "scheduleId", "id")
                    scheduled += 1
                except Exception as e:
                    errors.append(f"{workout.get('workoutName')}: added to library but not scheduled ({clean(e)[:80]})")
            sent.append(rec)
        except Exception as e:
            errors.append(f"{workout.get('workoutName')}: {clean(e)[:120]}")
        if i % 10 == 0:
            log(f"  {i}/{len(workouts)}")

    STATE.mkdir(exist_ok=True)
    sent_path.write_text(json.dumps(sent, indent=1))
    path.unlink()
    parts = [f"{uploaded} workout{'s' if uploaded != 1 else ''} added to Garmin Connect"]
    if scheduled:
        parts.append(f"{scheduled} scheduled on their dates")
    if skipped:
        parts.append(f"{skipped} past-dated skipped")
    if removed:
        parts.append(f"{removed} earlier ones replaced")
    return {"status": "ok" if uploaded or not workouts else "error", "uploaded": uploaded, "scheduled": scheduled,
            "skipped": skipped, "removed": removed, "errors": errors[:20],
            "message": ", ".join(parts) + "." + (f" {len(errors)} problem(s)." if errors else "")}


def cleanup(api, scope):
    """Bulk delete: Garmin Connect has no bulk delete for workouts, so do it one by one here."""
    sent_path = STATE / "sent.json"
    sent = json.loads(sent_path.read_text()) if sent_path.exists() else []
    if scope == "all":
        targets, start = [], 0
        while True:
            batch = call(api.get_workouts, start, 100) or []
            targets += [(w.get("workoutId"), None) for w in batch if isinstance(w, dict) and w.get("workoutId")]
            if len(batch) < 100:
                break
            start += 100
    else:
        targets = [(s.get("workoutId"), s.get("scheduleId")) for s in sent if s.get("workoutId")]
    log(f"Deleting {len(targets)} workouts...")
    removed, failed = 0, []
    for i, (wid, sid) in enumerate(targets, 1):
        try:
            if sid:
                try:
                    call(api.unschedule_workout, sid)
                except Exception:
                    pass
            call(api.delete_workout, wid)
            removed += 1
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                removed += 1          # already gone
            else:
                failed.append(wid)
        if i % 20 == 0:
            log(f"  {i}/{len(targets)}")
    STATE.mkdir(exist_ok=True)
    sent_path.write_text(json.dumps([s for s in sent if s.get("workoutId") in failed], indent=1))
    what = "workouts from your Garmin library" if scope == "all" else "workouts sent from MatRob"
    msg = f"Deleted {removed} {what}." if removed else ("There were no workouts to delete." if not targets else "Nothing could be deleted.")
    if failed:
        msg += f" {len(failed)} couldn't be deleted; try again."
    return {"status": "ok" if not failed or removed else "error", "removed": removed, "failed": len(failed), "message": msg}


TEMPLATE_REPO = "matrobinson1993/matrob-garmin-sender"   # the shared public template: never send from it


def main():
    if (os.getenv("GITHUB_REPOSITORY") or "").lower() == TEMPLATE_REPO:
        msg = ("This is the shared public template, not your private sender. Update the MatRob app and tap "
               "Send to Garmin to connect again. It creates your own private sender.")
        gh(f"::error title=Wrong repository::{msg}")
        job = re.sub(r"[^A-Za-z0-9_.-]", "", os.getenv("JOB") or "") or "template"
        write_result(job, {"status": "error", "message": msg})
        sys.exit(1)
    job = re.sub(r"[^A-Za-z0-9_.-]", "", os.getenv("JOB") or "") or f"run-{os.getenv('GITHUB_RUN_ID', 'local')}"
    mode = (os.getenv("MODE") or "send").strip()
    try:
        api = connect()
    except NeedsCode:
        msg = "Garmin has sent you a verification code. Enter it in the app."
        gh(f"::warning title=Verification code needed::{msg}")
        write_result(job, {"status": "needs_code", "message": msg})
        return
    except SystemExit as e:
        write_result(job, {"status": "error", "message": clean(e.code)})
        gh(f"::error title=Garmin::{clean(e.code)}")
        sys.exit(1)

    if mode == "login":
        name = full_name(api)
        write_result(job, {"status": "ok", "name": name, "message": f"Connected to Garmin{' as ' + name if name else ''}."})
        log("Login OK.")
        return
    try:
        if mode in ("clean_sent", "clean_all"):
            result = cleanup(api, "all" if mode == "clean_all" else "sent")
        else:
            result = send(api, job)
    except SystemExit as e:
        result = {"status": "error", "message": clean(e.code)}
    except Exception as e:
        result = {"status": "error", "message": f"Something went wrong: {clean(e)}"}
    save_tokens(api)
    write_result(job, result)
    log(result["message"])
    if result["status"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
