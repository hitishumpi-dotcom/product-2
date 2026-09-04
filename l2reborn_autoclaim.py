"""
L2Reborn 12h Exp Rune Auto-Claimer — Multi-Account
====================================================
Scheduled version of the claim flow (same AJAX approach as the GUI app).
Run by Windows Task Scheduler every 12 hours.
"""

import asyncio
import html
import imaplib
import email as email_lib
import re
import time
import json
import logging
import os
import sys
import requests
from datetime import datetime
from urllib.parse import urlparse, parse_qs

try:
    from config import ACCOUNTS, TWOCAPTCHA_KEY, TURNSTILE_KEY
except ImportError:
    raise SystemExit("ERROR: config.py not found.")

# ─── TELEGRAM (optional) ────────────────────────────────────────────────────
# Set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in config.py to get run reports
# pushed to a Telegram chat. Leave them blank in config.py to disable.
try:
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
except ImportError:
    TELEGRAM_BOT_TOKEN = ""
    TELEGRAM_CHAT_ID = ""

def telegram_notify(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram notify failed: {e}")

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LOG_FILE    = os.path.join(BASE_DIR, "l2reborn_autoclaim.log")
STATUS_FILE = os.path.join(BASE_DIR, "status.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ─── STATUS ───────────────────────────────────────────────────────────────────

def load_status():
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_status(data):
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ─── 2CAPTCHA ─────────────────────────────────────────────────────────────────

def solve_turnstile(api_key, site_key, page_url):
    log.info("Submitting Turnstile to 2captcha...")
    r = requests.post("https://api.2captcha.com/createTask", json={
        "clientKey": api_key,
        "task": {
            "type": "TurnstileTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        },
    }, timeout=30)
    data = r.json()
    if data.get("errorId") != 0:
        raise RuntimeError(f"2captcha submission error: {data}")
    task_id = data["taskId"]
    log.info(f"Task {task_id} — polling...")
    for _ in range(100):
        time.sleep(3)
        r = requests.post("https://api.2captcha.com/getTaskResult", json={
            "clientKey": api_key,
            "taskId": task_id,
        }, timeout=15)
        data = r.json()
        if data.get("status") == "ready":
            log.info("Turnstile solved")
            return data["solution"]["token"]
        if data.get("errorId") != 0:
            raise RuntimeError(f"2captcha poll error: {data}")
    raise RuntimeError("Turnstile timed out — check your 2captcha balance")


# ─── GMAIL ────────────────────────────────────────────────────────────────────

def _imap_connect(gmail_user, app_pw):
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(gmail_user, app_pw)
    m.select("inbox")
    return m

def _extract_wfls_link(raw_bytes):
    msg = email_lib.message_from_bytes(raw_bytes)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/html", "text/plain"):
                body += part.get_payload(decode=True).decode(errors="ignore")
    else:
        body = msg.get_payload(decode=True).decode(errors="ignore")
    for link in re.findall(r'https?://[^\s"<>\']+', body):
        if "wfls-email-verification" in link:
            return html.unescape(link)
    return None

def fetch_verification_link(gmail_user, app_pw, since: datetime):
    """Find the most recent wfls verification link from emails received after `since`."""
    try:
        m = _imap_connect(gmail_user, app_pw)
        # Search by date only — ignore read/unread so phone reads don't break it
        since_str = since.strftime("%d-%b-%Y")
        _, ids = m.search(None, f'FROM "info@l2reborn.org" SINCE "{since_str}"')
        all_ids = ids[0].split()
        log.info(f"  Gmail: {len(all_ids)} email(s) from info@l2reborn.org since {since_str}")
        # Check newest first, pick the one received after our `since` timestamp
        for mid in reversed(all_ids):
            _, raw = m.fetch(mid, "(RFC822)")
            msg = email_lib.message_from_bytes(raw[0][1])
            date_str = msg.get("Date", "")
            try:
                import email.utils
                received_tuple = email.utils.parsedate_tz(date_str)
                if received_tuple:
                    # Convert both to UTC unix timestamps for accurate comparison
                    received_unix = email.utils.mktime_tz(received_tuple)
                    since_unix = since.timestamp()
                    log.info(f"    Email date: {date_str} | received_unix={received_unix:.0f} since_unix={since_unix:.0f} diff={received_unix - since_unix:.0f}s")
                    if received_unix < (since_unix - 30):  # 30s buffer only — must be fresh
                        continue
                else:
                    log.info(f"    Could not parse date: {date_str} — including email")
            except Exception as ex:
                log.info(f"    Date parse error: {ex} — including email")
            link = _extract_wfls_link(raw[0][1])
            if link:
                m.logout()
                return link
        m.logout()
    except Exception as e:
        log.error(f"Gmail check failed: {e}")
    return None


# ─── AJAX LOGIN ───────────────────────────────────────────────────────────────

async def ajax_login(page, acct, wfls_token=""):
    log.info("Solving captcha...")
    token = await asyncio.to_thread(
        solve_turnstile, TWOCAPTCHA_KEY, TURNSTILE_KEY, "https://l2reborn.org/signin/"
    )
    result = await page.evaluate("""
        async ({ email, password, token, wflsToken }) => {
            const nr = await fetch('/wp-admin/admin-ajax.php', {
                method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ action: 'l2mgm_nonce', nonce_name: 'l2mgm_login' }).toString()
            });
            const nd = await nr.json();
            if (!nd.success) return { success: false, error: 'nonce failed' };
            const lr = await fetch('/wp-admin/admin-ajax.php', {
                method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({
                    action: 'l2mgm_login', email: email, password: password,
                    remember: '1', 'wfls-remember-device': '1',
                    'cf-turnstile-response': token, 'wfls-email-verification': wflsToken,
                    redirect_to: '/account', nonce: nd.data.nonce,
                }).toString()
            });
            const raw = await lr.text();
            try { return JSON.parse(raw); } catch { return { success: false, raw }; }
        }
    """, {"email": acct["email"], "password": acct["password"], "token": token, "wflsToken": wfls_token})
    return result


# ─── CLAIM ────────────────────────────────────────────────────────────────────

async def claim_for_account(page, acct):
    label = acct["label"]

    # Record time before login so we only pick up verification emails sent after this point
    login_time = datetime.now()

    # Login
    await page.goto("https://l2reborn.org/signin/", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    result = await ajax_login(page, acct)
    log.info(f"[{label}] Login: success={result.get('success')} error={result.get('error', 'none')}")

    # Captcha retry
    if not result.get("success") and "captcha" in str(result.get("error", "")).lower():
        log.warning(f"[{label}] Captcha rejected — retrying...")
        await asyncio.sleep(3)
        result = await ajax_login(page, acct)

    # Email verification — the link from the email IS the login token; navigate to it directly
    if not result.get("success") and "verif" in str(result.get("error", "")).lower():
        log.info(f"[{label}] Email verification required — waiting for email from info@l2reborn.org...")
        link = None
        for attempt in range(24):  # up to 6 minutes
            link = await asyncio.to_thread(fetch_verification_link, acct["email"], acct["gmail_app_pw"], login_time)
            if link:
                break
            log.info(f"[{label}]   No email yet ({attempt+1}/24) — waiting 15s...")
            await asyncio.sleep(15)

        if not link:
            log.error(f"[{label}] Verification email never arrived after 6 minutes")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status = load_status()
            entry = status.get(acct["email"], {})
            entry["last_run_at"] = now_str
            entry["last_run_result"] = "needs_verification"
            status[acct["email"]] = entry
            save_status(status)
            return "needs_verification"

        # Navigate to the verification link on the SAME page.
        # The wfls plugin sets a session cookie server-side when the link is visited.
        # Then do AJAX login from that same page — the session already has the trusted device.
        # Do NOT pass the token in the AJAX body — the server reads it from the session.
        log.info(f"[{label}] Navigating to verification link on same page...")
        await page.goto(link, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        log.info(f"[{label}] Landed on: {page.url} — logging in...")
        result = await ajax_login(page, acct, "")
        log.info(f"[{label}] Post-verification login: success={result.get('success')} error={result.get('error','none')}")
        # Retry once if captcha was rejected (token can expire during page navigation)
        if not result.get("success") and "captcha" in str(result.get("error", "")).lower():
            log.warning(f"[{label}] Captcha rejected after verification — retrying once...")
            await asyncio.sleep(3)
            result = await ajax_login(page, acct, "")
            log.info(f"[{label}] Retry login: success={result.get('success')} error={result.get('error','none')}")
        if not result.get("success"):
            log.error(f"[{label}] Login failed after verification: {result}")
            return "needs_verification"
        log.info(f"[{label}] Logged in successfully after verification")

    elif not result.get("success"):
        error_msg = str(result.get("error", ""))
        if "blocked" in error_msg.lower() or "too many" in error_msg.lower():
            log.error(f"[{label}] Account blocked by rate limit — aborting retries: {result}")
            return "blocked"
        log.error(f"[{label}] Login failed: {result}")
        return False

    log.info(f"[{label}] Logged in")

    # Resolve server_id and character_id
    server_id    = acct.get("server_id", "")
    character_id = acct.get("character_id", "")
    ga           = acct["game_account"]
    char_name    = acct.get("character", "")

    if not server_id or not character_id:
        log.info(f"[{label}] Resolving IDs from account page...")
        await page.goto("https://l2reborn.org/account/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        ids = await page.evaluate("""
            ({ga, charName}) => {
                const rows = Array.from(document.querySelectorAll('.account_rows[data-div-id]'));
                for (const row of rows) {
                    const name = row.querySelector('.text_12_account2b, .text_12_account2')?.textContent.trim()
                               || row.dataset.divId || '';
                    if (!name.includes(ga) && !ga.includes(name)) continue;
                    const btns = Array.from(row.querySelectorAll('.btn_unstuck[data-char-id]'));
                    for (const btn of btns) {
                        if (!charName || btn.dataset.charName === charName) {
                            return { serverId: btn.dataset.serverId, charId: btn.dataset.charId };
                        }
                    }
                }
                const btn = document.querySelector('.btn_unstuck[data-char-id]');
                return btn ? { serverId: btn.dataset.serverId, charId: btn.dataset.charId } : null;
            }
        """, {"ga": ga, "charName": char_name})
        if not ids:
            log.error(f"[{label}] Could not resolve server/character IDs")
            return False
        server_id    = ids["serverId"]
        character_id = ids["charId"]

    log.info(f"[{label}] Server ID: {server_id}  Character ID: {character_id}")

    # Get VIP token
    await page.goto("https://l2reborn.org/shop/", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(1)
    token_res = await page.evaluate("""
        async (sid) => {
            const r = await fetch('/wp-admin/admin-ajax.php?action=l2mgm_get_vip_token&server_id=' + sid);
            return await r.json();
        }
    """, server_id)
    if not (isinstance(token_res, dict) and token_res.get("success")):
        log.error(f"[{label}] VIP token failed: {token_res}")
        return False

    vip_token = token_res["data"]["token"]
    log.info(f"[{label}] VIP token received — waiting 65s...")
    await asyncio.sleep(65)

    # Submit claim
    shop_result = await page.evaluate("""
        async ({serverId, account, characterId, vipToken}) => {
            const nr = await fetch('/wp-admin/admin-ajax.php', {
                method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'action=l2mgm_nonce&nonce_name=shop'
            });
            const nd = await nr.json();
            const sr = await fetch('/wp-admin/admin-ajax.php', {
                method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: new URLSearchParams({
                    action: 'l2mgm_donation_service_v2',
                    _wpnonce: nd.data.nonce,
                    service: 'exp_rune',
                    server_id: serverId,
                    account: account,
                    character: characterId,
                    vote_token: vipToken,
                    vote_retries: '0',
                }).toString()
            });
            const raw = await sr.text();
            try { return JSON.parse(raw); } catch { return { raw }; }
        }
    """, {"serverId": server_id, "account": ga, "characterId": character_id, "vipToken": vip_token})

    code = None
    if isinstance(shop_result, dict) and isinstance(shop_result.get("data"), dict):
        code = shop_result["data"].get("error_code")
    ok = bool(isinstance(shop_result, dict) and shop_result.get("success")) or code == 3

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status  = load_status()
    entry   = status.get(acct["email"], {})
    entry["last_run_at"] = now_str
    if ok:
        msg = "Already claimed (cooldown)" if code == 3 else "12h Exp Rune claimed!"
        log.info(f"[{label}] {msg}")
        if code != 3:
            entry["last_claimed"] = now_str
        entry["last_run_result"] = "cooldown" if code == 3 else "claimed"
        status[acct["email"]] = entry
        save_status(status)
        return True
    else:
        log.error(f"[{label}] Claim failed: {shop_result}")
        entry["last_run_result"] = "failed"
        status[acct["email"]] = entry
        save_status(status)
        return False


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def run():
    from playwright.async_api import async_playwright

    log.info("=" * 65)
    log.info(f"L2Reborn Auto-Claim — {datetime.now()}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        active = [a for a in ACCOUNTS if a.get("enabled", True)]
        log.info(f"Active accounts: {len(active)}/{len(ACCOUNTS)}")

        MAX_RETRIES = 3
        results = {}  # label -> (ok, email)
        for idx, acct in enumerate(active):
            log.info(f"\n── {acct['label']} ({idx+1}/{len(active)}) ──")
            attempt = 0
            ok = None
            while ok is not True and attempt < MAX_RETRIES:
                attempt += 1
                delay = min(10 * (2 ** (attempt - 1)), 300)
                log.info(f"[{acct['label']}] Attempt {attempt}/{MAX_RETRIES}...")
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 720},
                )
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page = await ctx.new_page()
                try:
                    ok = await asyncio.wait_for(claim_for_account(page, acct), timeout=600)
                except asyncio.TimeoutError:
                    log.error(f"[{acct['label']}] Attempt {attempt} timed out (5 min)")
                    ok = None
                except Exception as e:
                    log.error(f"[{acct['label']}] Attempt {attempt} error: {e}")
                    ok = None
                finally:
                    await ctx.close()
                if ok in ("blocked", "needs_verification"):
                    reason = "blocked by rate limit" if ok == "blocked" else "verification token rejected — will retry next cycle"
                    log.error(f"[{acct['label']}] {reason} — skipping to next account")
                    ok = False
                    break
                if ok is not True:
                    log.warning(f"[{acct['label']}] Attempt {attempt} failed — retrying in {delay}s...")
                    await asyncio.sleep(delay)
            if ok is not True and attempt >= MAX_RETRIES:
                log.error(f"[{acct['label']}] Exhausted {MAX_RETRIES} retries — giving up")
                ok = False
            results[acct["label"]] = (ok, acct["email"])
            if idx < len(active) - 1:
                await asyncio.sleep(5)

        await browser.close()

    log.info("\nAll accounts processed.")

    # ── Telegram report (optional — no-op if not configured) ─────────────────
    from datetime import timedelta
    status = load_status()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"<b>🎮 L2Reborn VIP Ticket Report</b>", f"<i>{now_str}</i>", ""]
    for label, (ok, email) in results.items():
        entry = status.get(email, {})
        run_result = entry.get("last_run_result", "")
        last_claimed = entry.get("last_claimed")
        if last_claimed:
            next_claim = datetime.strptime(last_claimed, "%Y-%m-%d %H:%M:%S") + timedelta(hours=12)
            next_str = next_claim.strftime("%Y-%m-%d %H:%M")
        else:
            next_str = "unknown"

        if not ok and run_result == "needs_verification":
            lines.append(f"🔐 {label} — verification email not found in time, will retry next cycle")
        elif not ok:
            lines.append(f"❌ {label} — failed\n   ⚠️ Check log for details")
        elif run_result == "cooldown":
            lines.append(f"🔁 {label} — cooldown\n   ⏰ Next claim: {next_str}")
        else:
            lines.append(f"✅ {label} — claimed\n   ⏰ Next claim: {next_str}")
    telegram_notify("\n".join(lines))

    # ── Reschedule task 12h15m from now ─────────────────────────────────────
    import subprocess
    from datetime import timedelta
    next_run = datetime.now() + timedelta(hours=12, minutes=15)
    next_run_str = next_run.strftime("%H:%M")
    next_run_date = next_run.strftime("%d/%m/%Y")
    ps_cmd = (
        f"$t = Get-ScheduledTask -TaskName 'L2Reborn AutoVote';"
        f"$t.Triggers[0].StartBoundary = '{next_run.strftime('%Y-%m-%dT%H:%M:%S')}';"
        f"Set-ScheduledTask -TaskName 'L2Reborn AutoVote' -Trigger $t.Triggers[0]"
    )
    result = subprocess.run(
        ["powershell", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        log.info(f"Next run scheduled for {next_run.strftime('%Y-%m-%d %H:%M')}")
    else:
        log.error(f"Failed to reschedule task: {result.stderr.strip() or result.stdout.strip()}")


if __name__ == "__main__":
    asyncio.run(run())
