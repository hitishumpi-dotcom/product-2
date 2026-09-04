# L2Reborn Auto-Vote

Automatically claims the 12h VIP Exp Rune from [l2reborn.org](https://l2reborn.org) for one or more accounts, every 12 hours, unattended.

## Setup

1. Run **`setup.bat`** — installs Python (if missing) and the required packages (Playwright + Chromium).
2. Get a [2captcha.com](https://2captcha.com) API key (a few cents per solve) and either:
   - Launch the GUI (`python app.py`) and use **"+ Add"** to add your account — it auto-discovers your server and character, or
   - Edit `config.py` by hand and fill in your credentials.
3. In the GUI, click **"Run Now"** to test a claim, or **"Schedule 12h"** to automate it via Windows Task Scheduler.
   - Alternatively, run `schedule_task.bat` directly to create the scheduled task without the GUI.

## Requirements per account

- L2Reborn login email + password
- A Gmail **App Password** for that same inbox (used to fetch the email verification link) — generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
- In-game account name and character name

## Optional: Telegram notifications

Fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `config.py` to get a run report sent to a Telegram chat after each cycle. Leave both blank to disable — nothing is sent anywhere by default.

## Files

| File | Purpose |
|---|---|
| `app.py` | GUI — add/manage accounts, run manually, schedule |
| `l2reborn_autoclaim.py` | Headless claim script, run by the scheduled task |
| `discover.py` | Auto-discovers server_id/character_id for the wizard |
| `manage.py` | Account management helpers used by the GUI |
| `config.py` | Your credentials and settings — **never commit this with real values filled in** |
| `setup.bat` | First-time install (Python + dependencies) |
| `schedule_task.bat` | Creates the "L2Reborn AutoVote" Windows scheduled task (every 12h) |

## Notes

- The scheduled task is named **`L2Reborn AutoVote`** — the script re-nudges its own next run time by 12h15m after each cycle to avoid drift.
- `config.py`, `status.json`, and `*.log` are git-ignored — your credentials never get committed.
