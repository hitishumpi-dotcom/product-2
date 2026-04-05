"""
L2Reborn Account Discovery & Config Setup
==========================================
Run this ONCE to interactively choose your server, game account, and character
for each login account. It logs in, fetches the real options from the site,
lets you pick from numbered lists, and writes the choices into config.py.

Usage:
    python discover.py
"""

import asyncio
import sys
import os
import re
import time
import imaplib
import email as email_lib

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
            print("    Turnstile solved ✓")
            return data["request"]
        if data.get("request") not in ("CAPCHA_NOT_READY", "ERROR_CAPTCHA_UNSOLVABLE"):
            raise RuntimeError(f"2captcha poll error: {data}")
    raise RuntimeError("Turnstile timed out")


def fetch_verification_link_sync(gmail_user: str, app_pw: str) -> str | None:
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
        print(f"    Gmail check failed: {e}")
    return None


# ─── Browser helpers ──────────────────────────────────────────────────────────

async def login(page, acct: dict):
    await page.goto("https://l2reborn.org/signin/", wait_until="networkidle")
    await page.fill('input[type="email"], input[name="email"]', acct["email"])
    await page.fill('input[type="password"]', acct["password"])
    try:
        site_key = await page.get_attribute('[data-sitekey]', 'data-sitekey', timeout=3000)
        if site_key:
            token = await asyncio.to_thread(
                solve_turnstile_sync, cfg.TWOCAPTCHA_KEY, site_key, page.url
            )
            await page.evaluate(
                f"document.querySelector('[name=\"cf-turnstile-response\"]').value = '{token}'"
            )
    except Exception:
        pass
    await page.locator('input[type="submit"], button[type="submit"]').first.click()
    await page.wait_for_load_state("networkidle")


async def handle_verification(page, acct: dict):
    await asyncio.sleep(3)
    if not any(kw in page.url for kw in ("verify", "confirm", "check", "email")):
        return
    print("    Email verification required — checking inbox...")
    for attempt in range(12):
        link = await asyncio.to_thread(fetch_verification_link_sync, acct["email"], acct["gmail_app_pw"])
        if link:
            print(f"    Found link — clicking...")
            await page.goto(link, wait_until="networkidle")
            await asyncio.sleep(2)
            if "signin" in page.url:
                await login(page, acct)
            return
        print(f"    No email yet ({attempt+1}/12), waiting 15s...")
        await asyncio.sleep(15)


async def discover_options(page) -> dict:
    """
    Navigate to the shop and scrape available servers, game accounts, characters.
    Returns: { server_name: { game_account: [characters] } }
    """
    await page.goto("https://l2reborn.org/shop/", wait_until="networkidle")
    await asyncio.sleep(2)

    # Get server tabs
    server_tabs = await page.evaluate("""
        () => Array.from(document.querySelectorAll('.servers_tabs_item, [class*="tab"]'))
              .map(el => el.textContent.trim())
              .filter(t => t.length > 0 && t.length < 30)
    """)

    # Fall back to known servers if scraping fails
    known_servers = ["Origins", "Signature", "Essence", "Eternal IL", "Forever H5"]
    if not server_tabs or len(server_tabs) < 2:
        server_tabs = known_servers

    # Deduplicate while preserving order
    seen = set()
    server_tabs = [s for s in server_tabs if not (s in seen or seen.add(s))]

    result = {}

    for server in server_tabs:
        try:
            await page.locator(f'text="{server}"').first.click()
            await asyncio.sleep(1)
        except Exception:
            continue

        # Get sub-servers (e.g. Aden Main, Goddard New)
        sub_tabs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('.servers_sub_tabs .tab_item, [class*="sub_tab"], [class*="subtab"]'))
                  .map(el => el.textContent.trim()).filter(Boolean)
        """)

        sub_servers = sub_tabs if sub_tabs else [server]

        for sub in sub_servers:
            try:
                if sub != server:
                    await page.locator(f'text="{sub}"').first.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass

            # Click Receive to open the modal and get accounts/characters
            clicked = await page.evaluate("""
                () => {
                    const els = Array.from(document.querySelectorAll('.btn_recive_shop.js-open-shop-service'));
                    const visible = els.find(el => el.getBoundingClientRect().width > 0);
                    if (visible) { visible.click(); return true; }
                    return false;
                }
            """)
            if not clicked:
                continue
            await asyncio.sleep(1.5)

            # Get account options
            try:
                await page.locator('text="Select account"').first.click()
                await asyncio.sleep(0.5)
            except Exception:
                pass

            accounts_in_server = await page.evaluate("""
                () => Array.from(document.querySelectorAll('.select_body_item'))
                      .map(el => el.textContent.trim()).filter(Boolean)
            """)

            server_key = sub if sub != server else server
            result[server_key] = {}

            for acct_name in accounts_in_server:
                # Select account to reveal characters
                try:
                    await page.locator(f'text="{acct_name}"').first.click()
                    await asyncio.sleep(0.5)
                    await page.locator('text="Select character"').first.click()
                    await asyncio.sleep(0.5)
                    chars = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('.select_body_item'))
                              .map(el => el.textContent.trim()).filter(Boolean)
                    """)
                    result[server_key][acct_name] = chars
                    # Reopen account dropdown for next iteration
                    try:
                        await page.locator('text="Select account"').first.click()
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
                except Exception:
                    result[server_key][acct_name] = []

            # Close modal
            try:
                await page.locator('text="Close"').first.click()
                await asyncio.sleep(0.5)
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

            break  # Only need first sub-server per server for discovery

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
        browser = await pw.chromium.launch(headless=False, args=["--no-sandbox"])

        for idx, acct in enumerate(cfg.ACCOUNTS):
            print(f"\n── Account {idx+1}: {acct['email']} ──────────────────────")

            ctx = await browser.new_context()
            page = await ctx.new_page()

            try:
                print("  Logging in...")
                await login(page, acct)
                await handle_verification(page, acct)

                # Verify login
                content = await page.content()
                username = acct["email"].split("@")[0]
                if username not in content and acct["email"] not in content:
                    print("  Login failed — keeping existing config for this account")
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
                updated["game_account"] = chosen_account
                updated["character"] = chosen_char
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
            f'        "game_account":  "{acct["game_account"]}",\n',
            f'        "character":     {char_val},\n',
            "    },\n",
        ]
    lines.append("]\n")

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    with open(config_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"\n  config.py written to: {config_path}")


if __name__ == "__main__":
    asyncio.run(run())
