# MatRob Garmin Sender

This repository sends workouts from the **MatRob Running Generator** to your Garmin watch.

You don't need to do anything here. The app sets it up for you when you tap **Send to Garmin** the first time.

## How it works

1. The app saves your Garmin email and password as encrypted GitHub **secrets** in this repository. It's private to you.
2. When you tap **Send to Garmin**, the app puts your workouts in `inbox/` and starts the **Send workouts to Garmin** workflow.
3. GitHub runs `send_workouts.py`. It logs in to Garmin Connect, uploads each workout, puts it on its date in your Garmin calendar, and writes a result to `results/` that the app shows you.
4. The next 7 days' workouts are sent straight to your watch. Every morning, the sender automatically sends the next 7 days, so your watch stays topped up without filling its storage. Sync with the Garmin Connect app as usual.

The Garmin login is remembered between runs, so you only need to enter a 2-step verification code once in a while.

## Notes

- **Updates are automatic.** The app keeps the files in this repository up to date with the template (`sender_version.txt`).

- **Keep this repository private.** The app creates it private.
- **Past-dated workouts are skipped.** Workouts dated today or later are scheduled on their dates.
- **"Replace earlier workouts"** in the app removes upcoming workouts that it sent before, so a changed plan doesn't leave duplicates. It never touches workouts you made yourself.
- **Bulk delete:** Garmin Connect can't delete workouts in bulk, so the app's **Clean up** option does it for you. It can remove every workout MatRob sent, or every workout in your Garmin workouts library. Deleting a workout also removes it from your Garmin calendar. Completed activities are never affected.
- **Changed your Garmin password?** Open **Send to Garmin** in the app and connect again, or update the `GARMIN_PASSWORD` secret under **Settings → Secrets and variables → Actions**.
- **Unofficial access:** this uses Garmin's unofficial API through the [`garminconnect`](https://pypi.org/project/garminconnect/) library. It isn't endorsed by Garmin and can break when Garmin changes things.
