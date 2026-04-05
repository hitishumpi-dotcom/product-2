"""
L2Reborn Auto-Vote — Account Manager
=====================================
A simple menu to add/remove accounts and manage the 12-hour auto-vote schedule.

Usage:
    python manage.py
"""

import os
import sys
import json
import subprocess
import asyncio
import re
import time
import imaplib
import email as email_lib
from datetime import datetime

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l2reborn_autoclaim.py")

# ─── Config read/write ────────────────────────────────────────────────────────

def load_config() -> dict:
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", CONFIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {
        "TWOCAPTCHA_KEY": mod.TWOCAPTCHA_KEY,
        "TURNSTILE_KEY":  mod.TURNSTILE_KEY,
        "ACCOUNTS":       mod.ACCOUNTS,
    }


def save_config(cfg: dict):
    lines = [
        "# ─── YOUR PRIVATE CONFIG — DO NOT SHARE THIS FILE ────────────────────────────\n",
        "# Keep this file on your PC only. Share l2reborn_autoclaim.py freely.\n\n",
        f'TWOCAPTCHA_KEY = "{cfg["TWOCAPTCHA_KEY"]}"\n',
        f'TURNSTILE_KEY  = "{cfg["TURNSTILE_KEY"]}"\n\n',
        "ACCOUNTS = [\n",
    ]
    for acct in cfg["ACCOUNTS"]:
        char_val = f'"{acct["character"]}"' if acct.get("character") else "None"
        lines += [
            "    {\n",
            f'        "label":        "{acct["label"]}",\n',
            f'        "email":        "{acct["email"]}",\n',
            f'        "password":     "{acct["password"]}",\n',
            f'        "gmail_app_pw": "{acct["gmail_app_pw"]}",\n',
            f'        "game_account": "{acct["game_account"]}",\n',
            f'        "character":    {char_val},\n',
            f'        "enabled":      {str(acct.get("enabled", True))},\n',
            "    },\n",
        ]
    lines.append("]\n")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ─── Display helpers ──────────────────────────────────────────────────────────

def clear():
    os.system("cls" if os.name == "nt" else "clear")


def header():
    print("=" * 58)
    print("   L2Reborn Auto-Vote Manager")
    print("=" * 58)


def list_accounts(accounts: list):
    if not accounts:
        print("  (no accounts configured)")
        return
    for i, a in enumerate(accounts, 1):
        enabled = "✓" if a.get("enabled", True) else "✗"
        default = " [DEFAULT]" if i == 1 else ""
        print(f"  [{i}] {enabled} {a['label']}{default}")
        print(f"       Email:     {a['email']}")
        print(f"       Account:   {a['game_account']}  |  Character: {a.get('character') or '(auto)'}")
        print()


def input_or_default(prompt: str, default: str = "") -> str:
    val = input(f"  {prompt}" + (f" [{default}]: " if default else ": ")).strip()
    return val if val else default


def pick(prompt: str, options: list) -> str:
    print(f"\n  {prompt}")
    for i, opt in enumerate(options, 1):
        print(f"    [{i}] {opt}")
    while True:
        raw = input("  Choice: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print(f"  Enter a number 1–{len(options)}.")


# ─── Captcha / email helpers (same as other scripts) ─────────────────────────

def solve_turnstile_sync(api_key, site_key, page_url):
    import requests
    print("    Solving Turnstile via 2captcha...")
    r = requests.post("https://2captcha.com/in.php", data={
        "key": api_key, "method": "turnstile",
        "sitekey": site_key, "pageurl": page_url, "json": 1,
    }, timeout=30)
    data = r.json()
    if data.get("status") != 1:
        raise RuntimeError(f"2captcha error: {data}")
    task_id = data["request"]
    for _ in range(60):
        time.sleep(5)
        r = requests.get("https://2captcha.com/res.php", params={
            "key": api_key, "action": "get", "id": task_id, "json": 1,
        }, timeout=15)
        data = r.json()
        if data.get("status") == 1:
            return data["request"]
    raise RuntimeError("Turnstile timed out")


def fetch_verification_link_sync(gmail_user, app_pw):
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
                    return link
        m.logout()
    except Exception as e:
        print(f"    Gmail error: {e}")
    return None


# ─── Browser: login + discover ────────────────────────────────────────────────

async def browser_login_and_discover(acct: dict, api_key: str) -> dict | None:
    """Log in and return {server: {game_account: [characters]}} or None on failure."""
    from playwright.async_api import async_playwright

    result = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--no-sandbox"])
        ctx = await browser.new_context()
        page = await ctx.new_page()

        try:
            # Login
            await page.goto("https://l2reborn.org/signin/", wait_until="networkidle")
            await page.fill('input[type="email"], input[name="email"]', acct["email"])
            await page.fill('input[type="password"]', acct["password"])

            try:
                site_key = await page.get_attribute('[data-sitekey]', 'data-sitekey', timeout=3000)
                if site_key:
                    token = await asyncio.to_thread(solve_turnstile_sync, api_key, site_key, page.url)
                    await page.evaluate(
                        f"document.querySelector('[name=\"cf-turnstile-response\"]').value = '{token}'"
                    )
            except Exception:
                pass

            await page.locator('input[type="submit"], button[type="submit"]').first.click()
            await page.wait_for_load_state("networkidle")

            # Handle email verification
            await asyncio.sleep(3)
            if any(kw in page.url for kw in ("verify", "confirm", "check", "email")):
                print("    Email verification required — checking inbox...")
                for attempt in range(12):
                    link = await asyncio.to_thread(fetch_verification_link_sync, acct["email"], acct["gmail_app_pw"])
                    if link:
                        await page.goto(link, wait_until="networkidle")
                        await asyncio.sleep(2)
                        if "signin" in page.url:
                            await page.fill('input[type="email"], input[name="email"]', acct["email"])
                            await page.fill('input[type="password"]', acct["password"])
                            await page.locator('input[type="submit"], button[type="submit"]').first.click()
                            await page.wait_for_load_state("networkidle")
                        break
                    print(f"    No email yet ({attempt+1}/12) — waiting 15s...")
                    await asyncio.sleep(15)

            # Verify logged in
            content = await page.content()
            if acct["email"].split("@")[0] not in content and acct["email"] not in content:
                print("    Login failed.")
                return None

            print("    Logged in ✓  Discovering servers & accounts...")

            # Navigate to shop and discover options
            await page.goto("https://l2reborn.org/shop/", wait_until="networkidle")
            await asyncio.sleep(2)

            # Get server tabs
            server_names = await page.evaluate("""
                () => {
                    const tabs = document.querySelectorAll('.servers_tabs_item, [class*="tabs"] [class*="item"]');
                    return Array.from(tabs).map(el => el.textContent.trim()).filter(t => t && t.length < 30);
                }
            """)
            known = ["Origins", "Signature", "Essence", "Eternal IL", "Forever H5"]
            servers_found = list(dict.fromkeys([s for s in (server_names or known) if s in known or s]))

            result = {}
            for server in servers_found:
                try:
                    await page.locator(f'text="{server}"').first.click()
                    await asyncio.sleep(1)
                except Exception:
                    continue

                # Click Receive to open modal
                clicked = await page.evaluate("""
                    () => {
                        const els = Array.from(document.querySelectorAll('.btn_recive_shop.js-open-shop-service'));
                        const v = els.find(el => el.getBoundingClientRect().width > 0);
                        if (v) { v.click(); return true; }
                        return false;
                    }
                """)
                if not clicked:
                    continue
                await asyncio.sleep(1.5)

                # Scrape game accounts
                try:
                    await page.locator('text="Select account"').first.click()
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

                game_accounts = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('.select_body_item'))
                          .map(el => el.textContent.trim()).filter(Boolean)
                """)

                result[server] = {}
                for ga in game_accounts:
                    try:
                        await page.locator(f'text="{ga}"').first.click()
                        await asyncio.sleep(0.4)
                        await page.locator('text="Select character"').first.click()
                        await asyncio.sleep(0.4)
                        chars = await page.evaluate("""
                            () => Array.from(document.querySelectorAll('.select_body_item'))
                                  .map(el => el.textContent.trim()).filter(Boolean)
                        """)
                        result[server][ga] = chars
                        try:
                            await page.locator('text="Select account"').first.click()
                            await asyncio.sleep(0.3)
                        except Exception:
                            pass
                    except Exception:
                        result[server][ga] = []

                try:
                    await page.locator('text="Close"').first.click()
                except Exception:
                    await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

        except Exception as e:
            print(f"    Discovery error: {e}")
        finally:
            await ctx.close()
            await browser.close()

    return result


# ─── Add account flow ─────────────────────────────────────────────────────────

async def add_account_flow(cfg: dict) -> dict | None:
    print("\n── Add New Account ─────────────────────────────────────")
    email    = input_or_default("Email address")
    password = input_or_default("Password")
    app_pw   = input_or_default("Gmail App Password (for email verification)")
    label    = input_or_default("Nickname/label", f"Account {len(cfg['ACCOUNTS']) + 1}")

    acct = {
        "label":        label,
        "email":        email,
        "password":     password,
        "gmail_app_pw": app_pw,
        "game_account": "",
        "character":    None,
        "enabled":      True,
    }

    print("\n  Logging in and discovering your servers/accounts...")
    options = await browser_login_and_discover(acct, cfg["TWOCAPTCHA_KEY"])

    if not options:
        print("  Could not discover options. Account NOT added.")
        return None

    # Server selection
    servers = list(options.keys())
    chosen_server = servers[0] if len(servers) == 1 else pick("Choose server:", servers)

    game_accounts = list(options.get(chosen_server, {}).keys())
    if not game_accounts:
        print("  No game accounts found.")
        return None

    chosen_ga = game_accounts[0] if len(game_accounts) == 1 else pick("Choose game account:", game_accounts)

    chars = options[chosen_server].get(chosen_ga, [])
    if not chars:
        chosen_char = None
    elif len(chars) == 1:
        chosen_char = chars[0]
        print(f"  Only one character found: {chosen_char}")
    else:
        chosen_char = pick("Choose character:", chars)

    acct["game_account"] = chosen_ga
    acct["character"]    = chosen_char

    print(f"\n  ✅  Account ready:")
    print(f"      Server:    {chosen_server}")
    print(f"      Account:   {chosen_ga}")
    print(f"      Character: {chosen_char or '(auto)'}")
    return acct


# ─── Scheduler helpers ────────────────────────────────────────────────────────

def is_scheduled() -> bool:
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", "L2Reborn AutoVote"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except Exception:
        return False


def schedule_task():
    script = SCRIPT_PATH
    try:
        now = datetime.now().strftime("%H:%M")
        subprocess.run([
            "schtasks", "/create",
            "/tn", "L2Reborn AutoVote",
            "/tr", f'python "{script}"',
            "/sc", "HOURLY",
            "/mo", "12",
            "/st", now,
            "/ru", os.environ.get("USERNAME", ""),
            "/rl", "HIGHEST",
            "/f"
        ], check=True)
        print("  ✅  Task scheduled! Runs every 12 hours automatically.")
    except Exception as e:
        print(f"  ❌  Failed: {e}")
        print("  Try running manage.py as Administrator.")


def unschedule_task():
    try:
        subprocess.run(["schtasks", "/delete", "/tn", "L2Reborn AutoVote", "/f"], check=True)
        print("  ✅  Auto-vote schedule removed.")
    except Exception as e:
        print(f"  ❌  {e}")


def run_now():
    print("\n  Running auto-vote now (this will open a browser window)...\n")
    subprocess.run([sys.executable, SCRIPT_PATH])


# ─── Main menu ────────────────────────────────────────────────────────────────

async def main():
    while True:
        clear()
        header()

        cfg = load_config()
        accounts = cfg["ACCOUNTS"]
        scheduled = is_scheduled()

        print(f"\n  Accounts: {len(accounts)}   |   Auto-vote: {'🟢 ON (every 12h)' if scheduled else '🔴 OFF'}\n")
        list_accounts(accounts)

        print("  ─────────────────────────────────────────")
        print("  [A] Add account")
        print("  [R] Remove account")
        print("  [T] Toggle account on/off")
        print("  [D] Set default account (runs first)")
        if scheduled:
            print("  [S] Stop auto-vote schedule")
        else:
            print("  [S] Start auto-vote (every 12 hours)")
        print("  [N] Run vote NOW (test run)")
        print("  [Q] Quit")
        print()

        choice = input("  Your choice: ").strip().upper()

        if choice == "A":
            new_acct = await add_account_flow(cfg)
            if new_acct:
                cfg["ACCOUNTS"].append(new_acct)
                save_config(cfg)
                print("\n  Account saved to config.py ✓")
                input("\n  Press Enter to continue...")

        elif choice == "R":
            if not accounts:
                print("  No accounts to remove.")
            else:
                nums = [str(i) for i in range(1, len(accounts)+1)]
                n = input(f"  Remove which account? (1–{len(accounts)}): ").strip()
                if n in nums:
                    removed = cfg["ACCOUNTS"].pop(int(n)-1)
                    save_config(cfg)
                    print(f"  Removed: {removed['label']}")
            input("\n  Press Enter to continue...")

        elif choice == "T":
            if not accounts:
                print("  No accounts.")
            else:
                n = input(f"  Toggle which account? (1–{len(accounts)}): ").strip()
                if n.isdigit() and 1 <= int(n) <= len(accounts):
                    idx = int(n) - 1
                    cfg["ACCOUNTS"][idx]["enabled"] = not cfg["ACCOUNTS"][idx].get("enabled", True)
                    state = "enabled" if cfg["ACCOUNTS"][idx]["enabled"] else "disabled"
                    save_config(cfg)
                    print(f"  Account {n} is now {state}.")
            input("\n  Press Enter to continue...")

        elif choice == "D":
            if len(accounts) < 2:
                print("  Only one account — nothing to reorder.")
            else:
                n = input(f"  Make which account the default? (1–{len(accounts)}): ").strip()
                if n.isdigit() and 2 <= int(n) <= len(accounts):
                    idx = int(n) - 1
                    acct = cfg["ACCOUNTS"].pop(idx)
                    cfg["ACCOUNTS"].insert(0, acct)
                    save_config(cfg)
                    print(f"  '{acct['label']}' is now the default account.")
            input("\n  Press Enter to continue...")

        elif choice == "S":
            if scheduled:
                unschedule_task()
            else:
                schedule_task()
            input("\n  Press Enter to continue...")

        elif choice == "N":
            run_now()
            input("\n  Press Enter to continue...")

        elif choice == "Q":
            print("\n  Bye!\n")
            break


if __name__ == "__main__":
    asyncio.run(main())
