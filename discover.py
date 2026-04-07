"""
L2Reborn Account Discovery & Config Setup
==========================================
Run this ONCE to interactively choose your server, game account, and character
for each login account. It logs in via the site's AJAX API, fetches the real
options, lets you pick from numbered lists, and writes the choices into config.py.

Usage:
    python discover.py
"""

import asyncio
import sys
import os
import re
import time
import json
import imaplib
import email as email_lib
from urllib.parse import urlparse, parse_qs

# ─── Load existing config (for emails/passwords/API keys) ─────────────────────
try:
    import config as cfg
except ImportError:
    raise SystemExit(
        "ERROR: config.py not found.\n"
        "Make sure config.py is in the same folder as this script."
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def pick(prompt: str, options: list[str]) -> str:
    """Print a numbered list and return the chosen item."""
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  [{i}] {opt}")
    while True:
        raw = input("  Your choice (number): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            chosen = options[int(raw) - 1]
            print(f"  → Selected: {chosen}")
            return chosen
        print(f"  Please enter a number between 1 and {len(options)}.")


def solve_turnstile_sync(api_key: str, site_key: str, page_url: str) -> str:
    """Submit Turnstile to 2captcha v2 API and poll for the token."""
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
    print(f"    Task {task_id} — polling...")
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
    raise RuntimeError("Turnstile timed out — check your 2captcha balance")


def fetch_verification_link_sync(gmail_user: str, app_pw: str) -> str | None:
    """Search Gmail inbox for a wfls-email-verification link from l2reborn."""
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
        print(f"    Gmail check failed: {e}")
    return None


# ─── Browser helpers ──────────────────────────────────────────────────────────

async def login(page, acct: dict, wfls_token: str = "") -> dict:
    """
    Login via WordPress AJAX API (nonce → login with Turnstile token).
    Returns the server's JSON response dict.
    """
    await page.goto("https://l2reborn.org/signin/", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    print("    Solving Turnstile via 2captcha...")
    token = await asyncio.to_thread(
        solve_turnstile_sync, cfg.TWOCAPTCHA_KEY, cfg.TURNSTILE_KEY,
        "https://l2reborn.org/signin/"
    )
    print("    Token received — submitting login via AJAX...")

    result = await page.evaluate("""
        async ({ email, password, token, wflsToken }) => {
            // Step 1: get nonce
            const nr = await fetch('/wp-admin/admin-ajax.php', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ action: 'l2mgm_nonce', nonce_name: 'l2mgm_login' }).toString()
            });
            const nd = await nr.json();
            if (!nd.success) return { success: false, error: 'nonce failed', detail: JSON.stringify(nd) };

            // Step 2: login
            const lr = await fetch('/wp-admin/admin-ajax.php', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({
                    action: 'l2mgm_login',
                    email: email,
                    password: password,
                    remember: '1',
                    'wfls-remember-device': '1',
                    'cf-turnstile-response': token,
                    'wfls-email-verification': wflsToken,
                    redirect_to: '/account',
                    nonce: nd.data.nonce,
                }).toString()
            });
            const raw = await lr.text();
            try { return JSON.parse(raw); } catch { return { success: false, parseError: true, raw }; }
        }
    """, {"email": acct["email"], "password": acct["password"], "token": token, "wflsToken": wfls_token})

    print(f"    [DEBUG] Login result: {result}")
    return result


async def handle_verification(page, acct: dict, login_result: dict) -> dict:
    """
    If the login response indicates email verification is needed,
    fetch the wfls link from Gmail and re-login with the token.
    """
    error = str(login_result.get("error", "")).lower()
    if login_result.get("success") or "verif" not in error:
        return login_result

    print("    Email verification required — checking Gmail inbox...")
    for attempt in range(12):
        link = await asyncio.to_thread(fetch_verification_link_sync, acct["email"], acct["gmail_app_pw"])
        if link:
            print(f"    Found verification link — visiting it...")
            vp = await page.context.new_page()
            await vp.goto(link, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            await vp.close()
            # Extract the wfls token from the URL
            params = parse_qs(urlparse(link).query)
            wfls_token = params.get("wfls-email-verification", [""])[0]
            print(f"    wfls token extracted (len={len(wfls_token)}) — re-submitting login...")
            return await login(page, acct, wfls_token)
        print(f"    No email yet ({attempt+1}/12), waiting 15s...")
        await asyncio.sleep(15)

    raise RuntimeError("Email verification timed out — no wfls email found after retries")


async def is_logged_in(page) -> bool:
    """Check login state via the site's AJAX endpoint."""
    try:
        result = await page.evaluate("""
            async () => {
                const r = await fetch('/wp-admin/admin-ajax.php?action=l2mgm_logged');
                return await r.json();
            }
        """)
        return bool(result and result.get("success") is True)
    except Exception:
        return False


async def discover_options(page) -> dict:
    """
    Fetch servers and characters via AJAX API.
    Returns: { server_name: { game_account: [character_names] } }
    """
    # ── Fetch servers (AJAX then DOM fallback) ────────────────────────────────
    await page.goto("https://l2reborn.org/shop/", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)
    servers_raw = await page.evaluate("""
        async () => {
            try {
                const r = await fetch('/wp-admin/admin-ajax.php?action=l2mgm_get_servers');
                const d = await r.json();
                if (d.success && Array.isArray(d.data) && d.data.length) {
                    return d.data.map(s => ({ id: String(s.id ?? s.server_id ?? ''), name: s.name || s.server_name || '' }));
                }
            } catch(e) {}
            return Array.from(document.querySelectorAll('.tab_account[data-server-id]')).map(t => ({
                id: t.dataset.serverId,
                name: t.textContent.trim().replace(/\\s+/g, ' ')
            }));
        }
    """)
    server_map = {s['id']: s['name'] for s in servers_raw} if isinstance(servers_raw, list) else {}
    print(f"    Servers: {list(server_map.values()) or '(none)'}")

    # ── Fetch characters: AJAX first, then account page DOM ───────────────────
    await page.goto("https://l2reborn.org/account/", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)

    result = {}

    # Try AJAX
    characters = await page.evaluate("""
        async () => {
            for (const action of ['l2mgm_get_characters', 'l2mgm_get_account_characters']) {
                try {
                    const r = await fetch('/wp-admin/admin-ajax.php?action=' + action);
                    const d = await r.json();
                    if (d.success && Array.isArray(d.data) && d.data.length) {
                        return d.data.map(c => ({
                            id: String(c.id || c.char_id || c.character_id || ''),
                            name: c.name || c.char_name || c.character_name || c.nickname || '',
                            account: c.account || c.login || '',
                            serverId: String(c.server_id || c.serverId || ''),
                        }));
                    }
                } catch(e) {}
            }
            return [];
        }
    """)

    if characters:
        print(f"    Characters from AJAX: {len(characters)}")
        for c in characters:
            srv_id   = c["serverId"]
            srv_name = server_map.get(srv_id, f"Server {srv_id}" if srv_id else "Default")
            acct     = c["account"] or "Default"
            result.setdefault(srv_name, {})
            result[srv_name].setdefault(acct, [])
            if c["name"] and c["name"] not in result[srv_name][acct]:
                result[srv_name][acct].append(c["name"])
    else:
        # ── DOM fallback: scrape .account_rows on account page ─────────────────
        print("    AJAX returned nothing — scraping account page DOM…")
        dom_accounts = await page.evaluate("""
            () => {
                const out = [];
                document.querySelectorAll('.account_rows[data-div-id]').forEach(row => {
                    const name = row.querySelector('.text_12_account2b, .text_12_account2')?.textContent.trim()
                              || row.dataset.divId;
                    const chars = [];
                    row.querySelectorAll('.btn_unstuck[data-char-id]').forEach(btn => {
                        if (btn.dataset.charId && btn.dataset.charName) {
                            chars.push({ id: btn.dataset.charId, name: btn.dataset.charName, serverId: btn.dataset.serverId || '' });
                        }
                    });
                    if (name && chars.length > 0) out.push({ name, chars });
                });
                return out;
            }
        """)
        print(f"    DOM game accounts: {len(dom_accounts)}")
        for ga in dom_accounts:
            print(f"      {ga['name']}: {[c['name'] for c in ga['chars']]}")
            for c in ga["chars"]:
                srv_id   = c["serverId"]
                srv_name = server_map.get(srv_id, f"Server {srv_id}" if srv_id else "Default")
                result.setdefault(srv_name, {})
                result[srv_name].setdefault(ga["name"], [])
                if c["name"] not in result[srv_name][ga["name"]]:
                    result[srv_name][ga["name"]].append(c["name"])

    return result


# ─── Main discovery loop ───────────────────────────────────────────────────────

async def run():
    from playwright.async_api import async_playwright

    print("=" * 60)
    print("  L2Reborn Account Discovery & Config Setup")
    print("=" * 60)
    print(f"Found {len(cfg.ACCOUNTS)} account(s) in config.py\n")

    updated_accounts = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # visible — avoids headless detection
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )

        for idx, acct in enumerate(cfg.ACCOUNTS):
            print(f"\n── Account {idx+1}: {acct['email']} ──────────────────────")

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
                print("  Logging in via AJAX...")
                login_result = await login(page, acct)
                login_result = await handle_verification(page, acct, login_result)

                if not login_result.get("success"):
                    print(f"  Login failed: {login_result} — keeping existing config")
                    updated_accounts.append(acct)
                    await ctx.close()
                    continue

                print("  Logged in ✓  Discovering options...")
                options = await discover_options(page)

                if not options:
                    print("  Could not discover options — keeping existing config")
                    updated_accounts.append(acct)
                    await ctx.close()
                    continue

                # Let user pick server
                servers = list(options.keys())
                if len(servers) == 1:
                    chosen_server = servers[0]
                    print(f"\n  Only one server available: {chosen_server}")
                else:
                    chosen_server = pick(f"  Choose server for {acct['email']}:", servers)

                game_accounts = list(options.get(chosen_server, {}).keys())
                if not game_accounts:
                    print("  No game accounts found — keeping existing config")
                    updated_accounts.append(acct)
                    await ctx.close()
                    continue

                # Let user pick game account
                if len(game_accounts) == 1:
                    chosen_account = game_accounts[0]
                    print(f"\n  Only one game account: {chosen_account}")
                else:
                    chosen_account = pick(
                        f"  Choose game account for {acct['email']}:", game_accounts
                    )

                characters = options[chosen_server].get(chosen_account, [])
                if not characters:
                    print("  No characters found — will auto-pick at claim time")
                    chosen_char = None
                elif len(characters) == 1:
                    chosen_char = characters[0]
                    print(f"\n  Only one character: {chosen_char}")
                else:
                    chosen_char = pick(
                        f"  Choose character for {chosen_account}:", characters
                    )

                updated = dict(acct)
                updated["server"]       = chosen_server
                updated["game_account"] = chosen_account
                updated["character"]    = chosen_char
                updated_accounts.append(updated)

                print(f"\n  ✅  Saved: server={chosen_server}, account={chosen_account}, character={chosen_char}")

            except Exception as exc:
                print(f"  Error: {exc} — keeping existing config")
                updated_accounts.append(acct)
            finally:
                await ctx.close()

        await browser.close()

    # Write updated config.py
    _write_config(updated_accounts)
    print("\n" + "=" * 60)
    print("  config.py updated! Run l2reborn_autoclaim.py to start.")
    print("=" * 60)


def _write_config(accounts: list[dict]):
    lines = [
        "# ─── YOUR PRIVATE CONFIG — DO NOT SHARE THIS FILE ────────────────────────────\n",
        "# Keep this file on your PC only. Share l2reborn_autoclaim.py freely.\n",
        "\n",
        f'TWOCAPTCHA_KEY = "{cfg.TWOCAPTCHA_KEY}"\n',
        f'TURNSTILE_KEY  = "{cfg.TURNSTILE_KEY}"\n',
        "\n",
        "ACCOUNTS = [\n",
    ]
    for acct in accounts:
        char_val = f'"{acct["character"]}"' if acct.get("character") else "None"
        lines += [
            "    {\n",
            f'        "label":         "{acct["label"]}",\n',
            f'        "email":         "{acct["email"]}",\n',
            f'        "password":      "{acct["password"]}",\n',
            f'        "gmail_app_pw":  "{acct["gmail_app_pw"]}",\n',
            f'        "server":        "{acct.get("server", "")}",\n',
            f'        "game_account":  "{acct.get("game_account", "")}",\n',
            f'        "character":     {char_val},\n',
            f'        "enabled":       {str(acct.get("enabled", True))},\n',
            "    },\n",
        ]
    lines.append("]\n")

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    with open(config_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"\n  config.py written to: {config_path}")


if __name__ == "__main__":
    asyncio.run(run())
