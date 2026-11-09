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
              "push_week" (send the next 7 days' workouts to your watch; runs daily),
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
PUSH_DAYS = 7   # default days ahead to keep on the watch (watches only hold a limited number of workouts)
WATCH_RANGES = {"week": 7, "fortnight": 14, "all": None}   # chosen in the app: next week / next 2 weeks / whole plan


def settings():
    p = STATE / "settings.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def save_settings(**kw):
    s = settings()
    s.update({k: v for k, v in kw.items() if v})
    STATE.mkdir(exist_ok=True)
    (STATE / "settings.json").write_text(json.dumps(s, indent=1))


def local_today():
    """Today's date in the user's time zone (GitHub's servers run on UTC, a day behind Australia in the morning)."""
    tz = settings().get("tz")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return dt.datetime.now(ZoneInfo(tz)).date()
        except Exception:
            pass
    return dt.date.today()


def nice(d):
    return dt.date.fromisoformat(d).strftime("%a %-d %b")

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


def _device_id(api):
    try:
        d = api.get_device_last_used() or {}
        return d.get("userDeviceId") or d.get("deviceId") or d.get("lastUsedDeviceUnitId")
    except Exception:
        return None


def push_upcoming(api, sent):
    """Send upcoming scheduled workouts straight to the watch (like tapping Send to Device).
    Returns (pushed, error, info) where info explains what happened in plain words."""
    fn = getattr(api, "push_workout_to_device", None)
    if not callable(fn):
        return 0, "this version of the Garmin library can't send to watches", ""
    rng = settings().get("watch", "week")
    days = WATCH_RANGES.get(rng, PUSH_DAYS)
    today = local_today()
    upcoming = sorted((s["date"], s["workoutId"]) for s in sent if s.get("workoutId") and s.get("date") and s["date"] >= today.isoformat())
    if days is None:
        due = upcoming
    else:
        last = (today + dt.timedelta(days=days - 1)).isoformat()
        due = [u for u in upcoming if u[0] <= last]
    if not due:
        if not upcoming:
            return 0, None, "No upcoming workouts to put on your watch."
        first = upcoming[0][0]
        when = dt.date.fromisoformat(first) - dt.timedelta(days=(days or 1) - 1)
        return 0, None, (f"Nothing is due on your watch yet: your first workout is {nice(first)}. "
                         f"It will be sent to your watch automatically on {when.strftime('%a %-d %b')}.")
    pushed, err, dev, gone = 0, None, None, set()
    is_gone = lambda e: any(k in str(e) for k in ("404", "Not Found", "not found"))
    for _, wid in dict.fromkeys(due):
        try:
            call(fn, wid)
            pushed += 1
        except Exception as e:
            if is_gone(e):
                gone.add(wid)       # deleted in Garmin Connect: forget it instead of failing every day
                continue
            dev = dev or _device_id(api)
            try:
                if not dev:
                    raise e
                call(fn, wid, dev)
                pushed += 1
            except Exception as e2:
                if is_gone(e2):
                    gone.add(wid)
                else:
                    err = clean(e2)[:140]
    if gone:
        sent[:] = [s for s in sent if s.get("workoutId") not in gone]
        (STATE / "sent.json").write_text(json.dumps(sent, indent=1))
    span = "the whole plan" if days is None else f"the next {days} days"
    info = f"{pushed} workout{'s' if pushed != 1 else ''} for {span} sent to your watch." if pushed else ""
    return pushed, err, info


def send(api, job):
    path = INBOX / f"{job}.json"
    if not path.exists():
        raise SystemExit("The workouts didn't reach GitHub. Please tap Send again.")
    data = json.loads(path.read_text())
    save_settings(tz=data.get("tz"), watch=data.get("watch"))
    workouts = data.get("workouts") or []
    today = local_today().isoformat()
    sent_path = STATE / "sent.json"
    sent = json.loads(sent_path.read_text()) if sent_path.exists() else []

    removed = 0
    if data.get("replace"):
        keep = []
        for s in sent:
            if (s.get("date") or "9999") >= today:  # today = user's local date
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
    pushed, push_err, push_info = push_upcoming(api, sent)
    if data.get("single"):   # a single (undated) workout goes straight to the watch
        fn = getattr(api, "push_workout_to_device", None)
        for rec in sent[len(sent) - uploaded:]:
            if rec.get("workoutId") and not rec.get("date") and callable(fn):
                try:
                    call(fn, rec["workoutId"])
                    pushed += 1
                    push_info = "Sent to your watch."
                except Exception as e:
                    push_err = clean(e)[:140]
    parts = [f"{uploaded} workout{'s' if uploaded != 1 else ''} added to Garmin Connect"]
    if scheduled:
        parts.append(f"{scheduled} scheduled on their dates")
    if skipped:
        parts.append(f"{skipped} past-dated skipped")
    if removed:
        parts.append(f"{removed} earlier ones replaced")
    if push_err:
        errors.append(f"Couldn't send {'all ' if pushed else ''}workouts to your watch automatically ({push_err}). "
                      "You can still use Send to Device on a workout in Garmin Connect.")
    return {"status": "ok" if uploaded or not workouts else "error", "uploaded": uploaded, "scheduled": scheduled,
            "skipped": skipped, "removed": removed, "pushed": pushed, "errors": errors[:20],
            "watch": push_info,
            "message": ", ".join(parts) + "." + (f" {push_info}" if push_info else "") + (f" {len(errors)} problem(s)." if errors else "")}


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
        elif mode == "push_week":
            sent_path = STATE / "sent.json"
            sent = json.loads(sent_path.read_text()) if sent_path.exists() else []
            pushed, err, info = push_upcoming(api, sent)
            result = {"status": "ok" if pushed or not err else "error", "pushed": pushed, "watch": info,
                      "message": info or (f"Couldn't send to your watch: {err}" if err else "Nothing to send.")}
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
