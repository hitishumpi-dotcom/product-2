"""
L2Reborn 12h Exp Rune Auto-Claimer — Multi-Account
====================================================
Scheduled version of the claim flow (same AJAX approach as the GUI app).
Run by Windows Task Scheduler every 12 hours.
"""

import asyncio
import imaplib
import email as email_lib
import re
import time
import json
import logging
import os
import sys
from datetime import datetime
from urllib.parse import urlparse, parse_qs

try:
    from config import ACCOUNTS, TWOCAPTCHA_KEY, TURNSTILE_KEY
except ImportError:
    raise SystemExit("ERROR: config.py not found.")

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
    import requests
    log.info("Submitting Turnstile to 2captcha...")
    r = requests.post("https://2captcha.com/in.php", data={
        "key": api_key, "method": "turnstile",
        "sitekey": site_key, "pageurl": page_url, "json": 1,
    }, timeout=30)
    data = r.json()
    if data.get("status") != 1:
        raise RuntimeError(f"2captcha submit error: {data}")
    task_id = data["request"]
    log.info(f"Task {task_id} — polling...")
    for _ in range(60):
        time.sleep(5)
        r = requests.get("https://2captcha.com/res.php", params={
            "key": api_key, "action": "get", "id": task_id, "json": 1,
        }, timeout=15)
        data = r.json()
        if data.get("status") == 1:
            log.info("Turnstile solved")
            return data["request"]
        if data.get("request") not in ("CAPCHA_NOT_READY", "ERROR_CAPTCHA_UNSOLVABLE"):
            raise RuntimeError(f"2captcha error: {data}")
    raise RuntimeError("Turnstile solve timed out")


# ─── GMAIL ────────────────────────────────────────────────────────────────────

def fetch_verification_link(gmail_user, app_pw):
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com")
        m.login(gmail_user, app_pw)
        m.select("inbox")
        _, ids = m.search(None, '(UNSEEN FROM "l2reborn")')
        for mid in (ids[0].split() or [])[-5:]:
            _, raw = m.fetch(mid, "(RFC822)")
            msg = email_lib.message_from_bytes(raw[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() in ("text/html", "text/plain"):
                        body += part.get_payload(decode=True).decode(errors="ignore")
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")
            for link in re.findall(r'https?://[^\s"<>\']+', body):
                if "wfls-email-verification" in link:
                    m.store(mid, "+FLAGS", "\\Seen")
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

    # Email verification
    if not result.get("success") and "verif" in str(result.get("error", "")).lower():
        log.info(f"[{label}] Email verification required — checking Gmail...")
        for attempt in range(12):
            link = await asyncio.to_thread(fetch_verification_link, acct["email"], acct["gmail_app_pw"])
            if link:
                vp = await page.context.new_page()
                await vp.goto(link, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
                await vp.close()
                wfls_token = parse_qs(urlparse(link).query).get("wfls-email-verification", [""])[0]
                result = await ajax_login(page, acct, wfls_token)
                break
            log.info(f"[{label}]   No email yet ({attempt+1}/12) — waiting 15s...")
            await asyncio.sleep(15)

    if not result.get("success"):
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

        for idx, acct in enumerate(active):
            log.info(f"\n── {acct['label']} ({idx+1}/{len(active)}) ──")
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
                await claim_for_account(page, acct)
            except Exception as e:
                log.error(f"[{acct['label']}] Unexpected error: {e}", exc_info=True)
            finally:
                await ctx.close()
            if idx < len(active) - 1:
                await asyncio.sleep(5)

        await browser.close()

    log.info("\nAll accounts processed.")


if __name__ == "__main__":
    asyncio.run(run())
