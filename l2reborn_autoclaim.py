"""
L2Reborn 12h Exp Rune Auto-Claimer — Multi-Account
====================================================
Claims the free 12h Exp Rune every 12 hours for multiple accounts.
Handles login, logout, Cloudflare Turnstile (via 2captcha), email
verification, and the 60-second wait timer.

Requirements:
    pip install playwright requests
    python -m playwright install chromium

Run once manually to confirm it works, then automate with schedule_task.bat.
"""

import asyncio
import imaplib
import email as email_lib
import re
import time
import logging
import os
import sys
from datetime import datetime

# ─── CONFIG ───────────────────────────────────────────────────────────────────
# All credentials live in config.py — edit that file, not this one.
# This script is safe to share with friends; config.py is not.
try:
    from config import ACCOUNTS, TWOCAPTCHA_KEY, TURNSTILE_KEY
except ImportError:
    raise SystemExit(
        "ERROR: config.py not found.\n"
        "Copy config.py into the same folder as this script and fill in your details."
    )
SHOP_URL        = "https://l2reborn.org/shop/#essence"
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
LOG_FILE        = os.path.join(BASE_DIR, "l2reborn_autoclaim.log")
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ─── 2CAPTCHA TURNSTILE SOLVER ────────────────────────────────────────────────

def solve_turnstile_sync(api_key: str, site_key: str, page_url: str) -> str:
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
    log.info(f"2captcha task {task_id} — polling...")
    for _ in range(60):
        time.sleep(5)
        r = requests.get("https://2captcha.com/res.php", params={
            "key": api_key, "action": "get", "id": task_id, "json": 1,
        }, timeout=15)
        data = r.json()
        if data.get("status") == 1:
            log.info("Turnstile solved ✓")
            return data["request"]
        if data.get("request") not in ("CAPCHA_NOT_READY", "ERROR_CAPTCHA_UNSOLVABLE"):
            raise RuntimeError(f"2captcha error: {data}")
    raise RuntimeError("Turnstile solve timed out")


# ─── GMAIL VERIFICATION ───────────────────────────────────────────────────────

def fetch_verification_link_sync(gmail_user: str, app_pw: str) -> str | None:
    log.info(f"Checking Gmail ({gmail_user}) for verification email...")
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
                if any(kw in link.lower() for kw in ("verify", "confirm", "token", "activate")):
                    m.store(mid, "+FLAGS", "\\Seen")
                    m.logout()
                    log.info(f"Found verification link: {link[:80]}...")
                    return link
        m.logout()
    except Exception as exc:
        log.error(f"Gmail check failed: {exc}")
    return None


# ─── LOGIN / LOGOUT HELPERS ───────────────────────────────────────────────────

async def is_logged_in(page, acct: dict) -> bool:
    try:
        content = await page.content()
        username = acct["email"].split("@")[0]
        return username in content or acct["email"] in content
    except Exception:
        return False


async def do_logout(page):
    log.info("Logging out...")
    try:
        await page.goto("https://l2reborn.org/shop/#logout", wait_until="networkidle")
        await asyncio.sleep(2)
        # Confirm logout by checking we're back on sign-in or homepage
        if "signin" not in page.url and await is_any_logged_in(page):
            # Try clicking logout link directly
            await page.evaluate("""
                () => {
                    const el = document.querySelector('a[href*="logout"]');
                    if (el) el.click();
                }
            """)
            await asyncio.sleep(2)
    except Exception as e:
        log.warning(f"Logout issue (continuing): {e}")


async def is_any_logged_in(page) -> bool:
    try:
        content = await page.content()
        return "Exit of your account" in content or "exit" in content.lower()
    except Exception:
        return False


async def do_login(page, acct: dict):
    log.info(f"Logging in as {acct['email']}...")
    await page.goto("https://l2reborn.org/signin/", wait_until="networkidle")
    await asyncio.sleep(1)

    # Clear + fill fields
    await page.fill('input[type="email"], input[name="email"]', acct["email"])
    await page.fill('input[type="password"]', acct["password"])

    # Solve Turnstile if present
    try:
        site_key = await page.get_attribute('[data-sitekey]', 'data-sitekey', timeout=3000)
        if site_key:
            token = await asyncio.to_thread(
                solve_turnstile_sync, TWOCAPTCHA_KEY, site_key, page.url
            )
            await page.evaluate(
                f"document.querySelector('[name=\"cf-turnstile-response\"]').value = '{token}'"
            )
    except Exception:
        pass  # No Turnstile visible

    await page.locator('input[type="submit"], button[type="submit"]').first.click()
    await page.wait_for_load_state("networkidle")
    log.info(f"Login submitted — now at: {page.url}")


async def handle_email_verification(page, acct: dict):
    await asyncio.sleep(3)
    if not any(kw in page.url for kw in ("verify", "confirm", "check", "email")):
        return
    log.info("Email verification required — checking inbox...")
    for attempt in range(12):
        link = await asyncio.to_thread(
            fetch_verification_link_sync, acct["email"], acct["gmail_app_pw"]
        )
        if link:
            log.info("Clicking verification link...")
            await page.goto(link, wait_until="networkidle")
            await asyncio.sleep(2)
            if "signin" in page.url:
                await do_login(page, acct)
            return
        log.info(f"  No email yet (attempt {attempt+1}/12) — retrying in 15s...")
        await asyncio.sleep(15)
    log.warning("Email verification timed out — continuing anyway")


# ─── CLAIM FLOW ───────────────────────────────────────────────────────────────

async def claim_rune_for_account(page, acct: dict) -> bool:
    label = acct["label"]
    log.info(f"[{label}] Navigating to shop...")
    await page.goto(SHOP_URL, wait_until="networkidle")
    await asyncio.sleep(2)

    # Ensure Essence tab
    try:
        await page.locator('text="Essence"').first.click()
        await asyncio.sleep(1)
    except Exception:
        pass

    # Ensure Aden (Main) sub-tab
    try:
        await page.locator('text="Aden"').first.click()
        await asyncio.sleep(1)
    except Exception:
        pass

    # Click Receive button via JS (it's a div with a class, not a <button>)
    log.info(f"[{label}] Clicking Receive...")
    clicked = await page.evaluate("""
        () => {
            const els = Array.from(document.querySelectorAll('.btn_recive_shop.js-open-shop-service'));
            const visible = els.find(el => el.getBoundingClientRect().width > 0);
            if (visible) { visible.click(); return true; }
            return false;
        }
    """)
    if not clicked:
        log.error(f"[{label}] Could not find Receive button")
        return False

    await asyncio.sleep(2)

    # Select game account
    game_account = acct["game_account"]
    log.info(f"[{label}] Selecting game account: {game_account}")
    try:
        await page.locator('text="Select account"').first.click()
        await asyncio.sleep(0.5)
        await page.locator(f'text="{game_account}"').first.click()
        await asyncio.sleep(0.5)
    except Exception as e:
        log.error(f"[{label}] Could not select game account: {e}")
        return False

    # Select character
    character = acct.get("character")
    log.info(f"[{label}] Selecting character: {character or '(first available)'}")
    try:
        await page.locator('text="Select character"').first.click()
        await asyncio.sleep(0.5)
        if character:
            char_opt = page.locator(f'text="{character}"').first
            await char_opt.scroll_into_view_if_needed()
            await char_opt.click()
        else:
            # Auto-pick the first option in the dropdown
            first_opt = page.locator('.select_body_item, [class*="select"] li, [class*="dropdown"] li').first
            await first_opt.click()
        await asyncio.sleep(0.5)
    except Exception as e:
        log.error(f"[{label}] Could not select character: {e}")
        return False

    # Wait up to 75 s for the Get reward button
    log.info(f"[{label}] Waiting for 60-second countdown...")
    for i in range(75):
        try:
            btn = page.locator('text="Get reward"').first
            if await btn.is_visible(timeout=1000):
                log.info(f"[{label}] Timer done — clicking Get reward!")
                await btn.click()
                await asyncio.sleep(3)
                if await page.locator('text="Purchase success"').count() > 0:
                    log.info(f"[{label}] ✅  12h Exp Rune claimed successfully!")
                    return True
                else:
                    log.error(f"[{label}] Clicked Get reward but no success message")
                    return False
        except Exception:
            pass
        if i % 10 == 0:
            log.info(f"[{label}]   Still waiting... ({i}s elapsed)")
        await asyncio.sleep(1)

    log.error(f"[{label}] Timed out waiting for Get reward button")
    return False


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def run():
    from playwright.async_api import async_playwright

    log.info("=" * 65)
    log.info(f"L2Reborn Multi-Account Auto-Claim — {datetime.now()}")
    log.info(f"Processing {len(ACCOUNTS)} account(s)")

    async with async_playwright() as pw:
        # Non-persistent context so each account gets a clean session
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        active = [a for a in ACCOUNTS if a.get("enabled", True)]
        log.info(f"Active accounts: {len(active)}/{len(ACCOUNTS)}")

        for idx, acct in enumerate(active):
            log.info("")
            log.info(f"── {acct['label']} ({idx+1}/{len(active)}) ──────────────────────")

            # Fresh context (fresh cookies) for each account
            ctx = await browser.new_context()
            page = await ctx.new_page()

            try:
                # Navigate and log in
                await page.goto("https://l2reborn.org/signin/", wait_until="networkidle")
                await do_login(page, acct)
                await handle_email_verification(page, acct)

                if not await is_logged_in(page, acct):
                    log.error(f"[{acct['label']}] Login failed — skipping")
                    await ctx.close()
                    continue

                log.info(f"[{acct['label']}] Logged in ✓")
                success = await claim_rune_for_account(page, acct)

                if not success:
                    log.warning(f"[{acct['label']}] Claim failed — will retry on next scheduled run")

                # Small pause between accounts
                if idx < len(active) - 1:
                    log.info("Waiting 5s before next account...")
                    await asyncio.sleep(5)

            except Exception as exc:
                log.error(f"[{acct['label']}] Unexpected error: {exc}", exc_info=True)
            finally:
                await ctx.close()

        await browser.close()

    log.info("")
    log.info("All accounts processed. Done.")


if __name__ == "__main__":
    asyncio.run(run())
