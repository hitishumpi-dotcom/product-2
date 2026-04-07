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
from urllib.parse import urlparse, parse_qs

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
    print("    Solving Turnstile via 2captcha (v2 API)...")
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
    for _ in range(100):
        time.sleep(3)
        r = requests.post("https://api.2captcha.com/getTaskResult", json={
            "clientKey": api_key,
            "taskId": task_id,
        }, timeout=15)
        data = r.json()
        if data.get("status") == "ready":
            print("    Turnstile solved ✓")
            return data["solution"]["token"]
        if data.get("errorId") != 0:
            raise RuntimeError(f"2captcha poll error: {data}")
    raise RuntimeError("Turnstile timed out")


def fetch_verification_link_sync(gmail_user, app_pw):
    """Search Gmail for a wfls-email-verification link from l2reborn."""
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com")
        m.login(gmail_user, app_pw)
        m.select("inbox")
        _, ids = m.search(None, 'FROM "l2reborn"')
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
                    m.logout()
                    return link
        m.logout()
    except Exception as e:
        print(f"    Gmail error: {e}")
    return None


# ─── Browser: login + discover ────────────────────────────────────────────────

async def browser_login_and_discover(acct: dict, api_key: str) -> dict | None:
    """
    Login via WordPress AJAX API and discover servers/accounts/characters.
    Returns {server_name: {game_account: [characters]}} or None on failure.
    """
    from playwright.async_api import async_playwright

    cfg_obj = load_config()
    turnstile_key = cfg_obj["TURNSTILE_KEY"]

    async def _login(page, wfls_token=""):
        await page.goto("https://l2reborn.org/signin/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
        print("    Solving Turnstile...")
        token = await asyncio.to_thread(
            solve_turnstile_sync, api_key, turnstile_key, "https://l2reborn.org/signin/"
        )
        print("    Token received — submitting login via AJAX...")
        return await page.evaluate("""
            async ({ email, password, token, wflsToken }) => {
                const nr = await fetch('/wp-admin/admin-ajax.php', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({ action: 'l2mgm_nonce', nonce_name: 'l2mgm_login' }).toString()
                });
                const nd = await nr.json();
                if (!nd.success) return { success: false, error: 'nonce failed' };
                const lr = await fetch('/wp-admin/admin-ajax.php', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: new URLSearchParams({
                        action: 'l2mgm_login',
                        email: email, password: password,
                        remember: '1', 'wfls-remember-device': '1',
                        'cf-turnstile-response': token,
                        'wfls-email-verification': wflsToken,
                        redirect_to: '/account', nonce: nd.data.nonce,
                    }).toString()
                });
                const raw = await lr.text();
                try { return JSON.parse(raw); } catch { return { success: false, raw }; }
            }
        """, {"email": acct["email"], "password": acct["password"], "token": token, "wflsToken": wfls_token})

    result = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await ctx.new_page()

        try:
            login_result = await _login(page)
            print(f"    [DEBUG] Login result: {login_result}")

            # Handle email verification if required
            error = str(login_result.get("error", "")).lower()
            if not login_result.get("success") and "verif" in error:
                print("    Email verification required — checking Gmail inbox...")
                for attempt in range(12):
                    link = await asyncio.to_thread(
                        fetch_verification_link_sync, acct["email"], acct["gmail_app_pw"]
                    )
                    if link:
                        print("    Found verification link — visiting it...")
                        vp = await ctx.new_page()
                        await vp.goto(link, wait_until="domcontentloaded", timeout=60000)
                        await asyncio.sleep(2)
                        await vp.close()
                        params = parse_qs(urlparse(link).query)
                        wfls_token = params.get("wfls-email-verification", [""])[0]
                        login_result = await _login(page, wfls_token)
                        print(f"    [DEBUG] Second login result: {login_result}")
                        break
                    print(f"    No email yet ({attempt+1}/12) — waiting 15s...")
                    await asyncio.sleep(15)

            if not login_result.get("success"):
                print(f"    Login failed: {login_result}")
                return None

            print("    Logged in ✓  Discovering servers & characters via API...")

            await page.goto("https://l2reborn.org/shop/", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)

            # Fetch servers via AJAX
            servers = await page.evaluate("""
                async () => {
                    try {
                        const r = await fetch('/wp-admin/admin-ajax.php?action=l2mgm_get_servers');
                        const d = await r.json();
                        if (d.success && Array.isArray(d.data) && d.data.length)
                            return d.data.map(s => ({ id: String(s.id ?? s.server_id ?? ''), name: s.name || s.server_name || ('Server ' + (s.id ?? '')) }));
                    } catch(e) {}
                    return [];
                }
            """)

            # Fetch characters via AJAX
            characters = await page.evaluate("""
                async () => {
                    for (const action of ['l2mgm_get_characters', 'l2mgm_get_account_characters']) {
                        try {
                            const r = await fetch('/wp-admin/admin-ajax.php?action=' + action);
                            const d = await r.json();
                            if (d.success && Array.isArray(d.data) && d.data.length)
                                return d.data.map(c => ({
                                    id: String(c.id || c.char_id || ''),
                                    name: c.name || c.char_name || c.nickname || '',
                                    account: c.account || c.login || '',
                                    serverId: String(c.server_id || c.serverId || ''),
                                }));
                        } catch(e) {}
                    }
                    return [];
                }
            """)
            print(f"    Servers: {[s['name'] for s in servers]}  Characters: {len(characters)}")

            if not servers and not characters:
                return None

            if not servers:
                servers = [{"id": "", "name": "Default"}]

            result = {}
            for srv in servers:
                chars_for_server = [c for c in characters if not c["serverId"] or c["serverId"] == srv["id"]]
                by_account: dict = {}
                for c in chars_for_server:
                    name = c["account"] or "Default"
                    by_account.setdefault(name, [])
                    if c["name"] and c["name"] not in by_account[name]:
                        by_account[name].append(c["name"])
                result[srv["name"]] = by_account

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
