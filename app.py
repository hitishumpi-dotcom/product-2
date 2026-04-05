"""
L2Reborn Auto-Vote — GUI App  v2
Dark gold theme matching L2Reborn style.
First-run wizard: enter credentials → auto-discover → pick account/character.
Run with: python app.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import asyncio
import os
import subprocess
import importlib.util
import time
import re
import imaplib
import email as email_lib
from datetime import datetime

# ─── Theme Colors (L2Reborn dark gold palette) ────────────────────────────────
BG_DARK    = "#0f0f0f"
BG_PANEL   = "#1a1a1a"
BG_CARD    = "#222222"
GOLD       = "#c8972a"
GOLD_LIGHT = "#e8b84b"
GOLD_DIM   = "#6b5015"
TEXT       = "#e8e0d0"
TEXT_DIM   = "#7a7060"
SUCCESS    = "#4caf50"
ERROR      = "#e05050"
WARNING    = "#e09030"
BORDER     = "#333333"
FONT_MAIN  = ("Segoe UI", 10)
FONT_BOLD  = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 8)
FONT_LOG   = ("Consolas", 9)

TURNSTILE_KEY = "0x4AAAAAAAPFfPxwacy3GCxf"
CONFIG_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
CLAIM_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l2reborn_autoclaim.py")


# ─── Config helpers ───────────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {"TWOCAPTCHA_KEY": "", "TURNSTILE_KEY": TURNSTILE_KEY, "ACCOUNTS": []}
    try:
        spec = importlib.util.spec_from_file_location("config", CONFIG_PATH)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        accounts = []
        for a in getattr(mod, "ACCOUNTS", []):
            a.setdefault("enabled", True)
            accounts.append(dict(a))
        return {
            "TWOCAPTCHA_KEY": getattr(mod, "TWOCAPTCHA_KEY", ""),
            "TURNSTILE_KEY":  getattr(mod, "TURNSTILE_KEY",  TURNSTILE_KEY),
            "ACCOUNTS":       accounts,
        }
    except Exception:
        return {"TWOCAPTCHA_KEY": "", "TURNSTILE_KEY": TURNSTILE_KEY, "ACCOUNTS": []}


def save_config(cfg):
    lines = [
        "# ─── L2REBORN AUTO-VOTE CONFIG — DO NOT SHARE ──────────────────\n\n",
        f'TWOCAPTCHA_KEY = "{cfg["TWOCAPTCHA_KEY"]}"\n',
        f'TURNSTILE_KEY  = "{cfg.get("TURNSTILE_KEY", TURNSTILE_KEY)}"\n\n',
        "ACCOUNTS = [\n",
    ]
    for a in cfg["ACCOUNTS"]:
        char_val = f'"{a["character"]}"' if a.get("character") else "None"
        lines += [
            "    {\n",
            f'        "label":        "{a["label"]}",\n',
            f'        "email":        "{a["email"]}",\n',
            f'        "password":     "{a["password"]}",\n',
            f'        "gmail_app_pw": "{a["gmail_app_pw"]}",\n',
            f'        "game_account": "{a["game_account"]}",\n',
            f'        "character":    {char_val},\n',
            f'        "enabled":      {a.get("enabled", True)},\n',
            "    },\n",
        ]
    lines.append("]\n")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def is_scheduled():
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", "L2Reborn AutoVote"],
                           capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False


# ─── Static helpers (shared by wizard + main app) ─────────────────────────────

def _solve_turnstile_static(api_key, site_key, page_url):
    import requests
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


def _fetch_verification_link_static(gmail_user, app_pw):
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
    except Exception:
        pass
    return None


# ─── Settings Dialog ──────────────────────────────────────────────────────────

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self.cfg    = cfg
        self.result = None
        self.grab_set()
        self._build()
        self.geometry("420x200")
        self._center(parent)

    def _center(self, parent):
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _build(self):
        tk.Label(self, text="⚙  Settings", bg=BG_DARK, fg=GOLD,
                 font=FONT_BOLD).pack(pady=(16, 4), padx=20, anchor="w")
        tk.Frame(self, bg=GOLD_DIM, height=1).pack(fill="x", padx=20)

        tk.Label(self, text="2Captcha API Key", bg=BG_DARK, fg=TEXT_DIM,
                 font=FONT_SMALL, anchor="w").pack(fill="x", padx=20, pady=(12, 0))
        self.f_key = tk.Entry(self, bg=BG_CARD, fg=TEXT, insertbackground=GOLD,
                              relief="flat", font=FONT_MAIN)
        self.f_key.insert(0, self.cfg.get("TWOCAPTCHA_KEY", ""))
        self.f_key.pack(fill="x", padx=20, ipady=5)

        tk.Label(self,
                 text="Get a free key at 2captcha.com  (~$0.001/solve, only charged when CAPTCHA appears)",
                 bg=BG_DARK, fg=TEXT_DIM, font=("Segoe UI", 7),
                 wraplength=380, anchor="w").pack(fill="x", padx=20, pady=(4, 0))

        btn_row = tk.Frame(self, bg=BG_DARK)
        btn_row.pack(fill="x", padx=20, pady=16)
        tk.Button(btn_row, text="Cancel", bg=BG_CARD, fg=TEXT_DIM, relief="flat",
                  font=FONT_MAIN, cursor="hand2", command=self.destroy,
                  padx=14, pady=6).pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="Save", bg=GOLD, fg=BG_DARK, relief="flat",
                  font=FONT_BOLD, cursor="hand2", command=self._save,
                  padx=14, pady=6).pack(side="right")

    def _save(self):
        self.result = self.f_key.get().strip()
        self.destroy()


# ─── Add Account Wizard ───────────────────────────────────────────────────────

class AddAccountWizard(tk.Toplevel):
    """
    2-step wizard:
      Page 1 — Enter email, password, Gmail App Password → click Discover
      Page 2 — Live discovery log (browser opens, logs in, scrapes accounts/characters)
      Page 3 — Pick game account + character from discovered dropdowns → Add
    """

    def __init__(self, parent, twocaptcha_key):
        super().__init__(parent)
        self.title("Add Account")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self.result         = None
        self.twocaptcha_key = twocaptcha_key
        self._q             = queue.Queue()
        self._discovered    = {}   # {game_account: [char1, char2, …]}
        self._disc_email    = ""
        self._disc_pw       = ""
        self._disc_app_pw   = ""
        self.grab_set()
        self._container = tk.Frame(self, bg=BG_DARK)
        self._container.pack(fill="both", expand=True)
        self._show_page1()
        self._center(parent)
        self._poll()

    def _center(self, parent):
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _clear(self):
        for w in self._container.winfo_children():
            w.destroy()

    # ── Page 1: credentials ───────────────────────────────────────────────────

    def _show_page1(self):
        self._clear()
        self.geometry("460x390")

        tk.Label(self._container, text="Add Account  —  Step 1 of 2",
                 bg=BG_DARK, fg=GOLD, font=FONT_BOLD
                 ).pack(pady=(18, 4), padx=20, anchor="w")
        tk.Frame(self._container, bg=GOLD_DIM, height=1).pack(fill="x", padx=20)
        tk.Label(self._container,
                 text="Enter your L2Reborn credentials. The app will open a browser,\n"
                      "log in automatically, and find your game accounts and characters.",
                 bg=BG_DARK, fg=TEXT_DIM, font=FONT_SMALL, justify="left"
                 ).pack(pady=(10, 4), padx=20, anchor="w")

        def field(lbl, show=None, prefill=""):
            tk.Label(self._container, text=lbl, bg=BG_DARK, fg=TEXT_DIM,
                     font=FONT_SMALL, anchor="w").pack(fill="x", padx=20, pady=(8, 0))
            e = tk.Entry(self._container, bg=BG_CARD, fg=TEXT, insertbackground=GOLD,
                         relief="flat", font=FONT_MAIN, show=show or "")
            if prefill:
                e.insert(0, prefill)
            e.pack(fill="x", padx=20, ipady=5)
            return e

        self.f_email  = field("L2Reborn Email",      prefill=self._disc_email)
        self.f_pw     = field("L2Reborn Password",    show="•", prefill=self._disc_pw)
        self.f_app_pw = field("Gmail App Password  (for email verification)", show="•",
                              prefill=self._disc_app_pw)

        tk.Label(self._container,
                 text="Gmail App Password guide: myaccount.google.com → Security → App Passwords",
                 bg=BG_DARK, fg=TEXT_DIM, font=("Segoe UI", 7),
                 wraplength=420, anchor="w").pack(fill="x", padx=20, pady=(2, 0))

        btn_row = tk.Frame(self._container, bg=BG_DARK)
        btn_row.pack(fill="x", padx=20, pady=16)
        tk.Button(btn_row, text="Cancel", bg=BG_CARD, fg=TEXT_DIM, relief="flat",
                  font=FONT_MAIN, cursor="hand2", command=self.destroy,
                  padx=14, pady=6).pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="Discover Accounts  →", bg=GOLD, fg=BG_DARK,
                  relief="flat", font=FONT_BOLD, cursor="hand2",
                  command=self._start_discovery, padx=14, pady=6).pack(side="right")

    # ── Page 2: discovering ───────────────────────────────────────────────────

    def _show_page2(self):
        self._clear()
        self.geometry("460x300")

        tk.Label(self._container, text="Discovering Your Accounts…",
                 bg=BG_DARK, fg=GOLD, font=FONT_BOLD
                 ).pack(pady=(18, 4), padx=20, anchor="w")
        tk.Frame(self._container, bg=GOLD_DIM, height=1).pack(fill="x", padx=20)

        self._disc_log = tk.Text(self._container, bg=BG_PANEL, fg=TEXT, font=FONT_LOG,
                                 relief="flat", height=9, state="disabled",
                                 padx=10, pady=8)
        self._disc_log.tag_configure("gold",    foreground=GOLD)
        self._disc_log.tag_configure("success", foreground=SUCCESS)
        self._disc_log.tag_configure("error",   foreground=ERROR)
        self._disc_log.tag_configure("dim",     foreground=TEXT_DIM)
        self._disc_log.pack(fill="both", expand=True, padx=20, pady=12)
        self._disc_append("Opening browser…", "dim")

    def _disc_append(self, msg, tag="white"):
        if not hasattr(self, "_disc_log"):
            return
        self._disc_log.configure(state="normal")
        self._disc_log.insert("end", msg + "\n", tag)
        self._disc_log.configure(state="disabled")
        self._disc_log.see("end")

    # ── Page 3: select ────────────────────────────────────────────────────────

    def _show_page3(self):
        self._clear()
        self.geometry("460x370")

        tk.Label(self._container, text="Add Account  —  Step 2 of 2",
                 bg=BG_DARK, fg=GOLD, font=FONT_BOLD
                 ).pack(pady=(18, 4), padx=20, anchor="w")
        tk.Frame(self._container, bg=GOLD_DIM, height=1).pack(fill="x", padx=20)
        tk.Label(self._container,
                 text=f"Found {len(self._discovered)} game account(s). Select which to use.",
                 bg=BG_DARK, fg=TEXT_DIM, font=FONT_SMALL
                 ).pack(pady=(10, 0), padx=20, anchor="w")

        def lbl(text):
            tk.Label(self._container, text=text, bg=BG_DARK, fg=TEXT_DIM,
                     font=FONT_SMALL, anchor="w").pack(fill="x", padx=20, pady=(10, 0))

        # Game account dropdown
        lbl("Game Account")
        ga_names   = list(self._discovered.keys())
        self.ga_var = tk.StringVar(value=ga_names[0] if ga_names else "")
        ga_menu = tk.OptionMenu(self._container, self.ga_var, *ga_names,
                                command=self._on_ga_change)
        ga_menu.configure(bg=BG_CARD, fg=TEXT, activebackground=GOLD_DIM,
                          activeforeground=TEXT, relief="flat", font=FONT_MAIN,
                          highlightthickness=0, anchor="w")
        ga_menu["menu"].configure(bg=BG_CARD, fg=TEXT, activebackground=GOLD_DIM)
        ga_menu.pack(fill="x", padx=20, ipady=3)

        # Character dropdown
        lbl("Character")
        self.char_var = tk.StringVar()
        self._char_menu = tk.OptionMenu(self._container, self.char_var, "")
        self._char_menu.configure(bg=BG_CARD, fg=TEXT, activebackground=GOLD_DIM,
                                  activeforeground=TEXT, relief="flat", font=FONT_MAIN,
                                  highlightthickness=0, anchor="w")
        self._char_menu["menu"].configure(bg=BG_CARD, fg=TEXT, activebackground=GOLD_DIM)
        self._char_menu.pack(fill="x", padx=20, ipady=3)
        if ga_names:
            self._on_ga_change(ga_names[0])

        # Nickname
        lbl("Nickname / Label  (optional)")
        self.f_label = tk.Entry(self._container, bg=BG_CARD, fg=TEXT,
                                insertbackground=GOLD, relief="flat", font=FONT_MAIN)
        self.f_label.pack(fill="x", padx=20, ipady=5)

        btn_row = tk.Frame(self._container, bg=BG_DARK)
        btn_row.pack(fill="x", padx=20, pady=16)
        tk.Button(btn_row, text="← Back", bg=BG_CARD, fg=TEXT_DIM, relief="flat",
                  font=FONT_MAIN, cursor="hand2", padx=14, pady=6,
                  command=self._show_page1).pack(side="left")
        tk.Button(btn_row, text="Cancel", bg=BG_CARD, fg=TEXT_DIM, relief="flat",
                  font=FONT_MAIN, cursor="hand2", command=self.destroy,
                  padx=14, pady=6).pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="Add Account  ✓", bg=GOLD, fg=BG_DARK,
                  relief="flat", font=FONT_BOLD, cursor="hand2",
                  command=self._confirm, padx=14, pady=6).pack(side="right")

    def _on_ga_change(self, selected):
        chars = self._discovered.get(selected, [])
        menu  = self._char_menu["menu"]
        menu.delete(0, "end")
        for c in chars:
            menu.add_command(label=c, command=lambda v=c: self.char_var.set(v))
        self.char_var.set(chars[0] if chars else "")

    def _confirm(self):
        ga   = self.ga_var.get().strip()
        char = self.char_var.get().strip()
        lbl  = self.f_label.get().strip() or f"Account {self._disc_email.split('@')[0]}"
        if not ga:
            messagebox.showerror("Missing", "Please select a game account.", parent=self)
            return
        self.result = {
            "label":        lbl,
            "email":        self._disc_email,
            "password":     self._disc_pw,
            "gmail_app_pw": self._disc_app_pw,
            "game_account": ga,
            "character":    char or None,
            "enabled":      True,
        }
        self.destroy()

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _start_discovery(self):
        email  = self.f_email.get().strip()
        pw     = self.f_pw.get().strip()
        app_pw = self.f_app_pw.get().strip()
        if not email or not pw:
            messagebox.showerror("Missing", "Email and password are required.", parent=self)
            return
        self._disc_email  = email
        self._disc_pw     = pw
        self._disc_app_pw = app_pw
        self._show_page2()
        threading.Thread(
            target=lambda: asyncio.run(self._run_discovery(email, pw, app_pw)),
            daemon=True
        ).start()

    async def _run_discovery(self, email, password, gmail_app_pw):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self._q.put(("error", "Playwright not installed. Run setup.bat first."))
            return

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                ctx  = await browser.new_context()
                page = await ctx.new_page()

                self._q.put(("log", "Navigating to l2reborn.org…", "dim"))
                await page.goto("https://l2reborn.org/signin/", wait_until="networkidle")
                await page.fill('input[type="email"], input[name="email"]', email)
                await page.fill('input[type="password"]', password)

                # Turnstile
                try:
                    sk = await page.get_attribute('[data-sitekey]', 'data-sitekey', timeout=3000)
                    if sk and self.twocaptcha_key:
                        self._q.put(("log", "Solving CAPTCHA…", "dim"))
                        token = await asyncio.to_thread(
                            _solve_turnstile_static, self.twocaptcha_key, sk, page.url
                        )
                        await page.evaluate(
                            f"document.querySelector('[name=\"cf-turnstile-response\"]').value = '{token}'"
                        )
                except Exception:
                    pass

                await page.locator('input[type="submit"], button[type="submit"]').first.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(3)

                # Email verification
                if any(kw in page.url for kw in ("verify", "confirm", "check", "email")):
                    self._q.put(("log", "Email verification needed — checking Gmail…", "warning"))
                    for attempt in range(12):
                        link = await asyncio.to_thread(
                            _fetch_verification_link_static, email, gmail_app_pw
                        )
                        if link:
                            await page.goto(link, wait_until="networkidle")
                            await asyncio.sleep(2)
                            if "signin" in page.url:
                                await page.fill('input[type="email"], input[name="email"]', email)
                                await page.fill('input[type="password"]', password)
                                await page.locator('input[type="submit"], button[type="submit"]').first.click()
                                await page.wait_for_load_state("networkidle")
                            break
                        self._q.put(("log", f"No email yet ({attempt+1}/12) — retrying…", "dim"))
                        await asyncio.sleep(15)

                self._q.put(("log", "Logged in ✓  —  Opening shop…", "success"))
                await page.goto("https://l2reborn.org/shop/#essence", wait_until="networkidle")
                await asyncio.sleep(2)

                for tab_text in ("Essence", "Aden"):
                    try:
                        await page.locator(f'text="{tab_text}"').first.click()
                        await asyncio.sleep(1)
                    except Exception:
                        pass

                # Open receive modal
                clicked = await page.evaluate("""
                    () => {
                        const els = Array.from(
                            document.querySelectorAll('.btn_recive_shop.js-open-shop-service')
                        );
                        const v = els.find(el => el.getBoundingClientRect().width > 0);
                        if (v) { v.click(); return true; }
                        return false;
                    }
                """)
                if not clicked:
                    self._q.put(("error", "Could not open reward dialog. Is this account eligible?"))
                    await browser.close()
                    return

                await asyncio.sleep(2)
                self._q.put(("log", "Scanning game accounts…", "dim"))

                # Click account dropdown to reveal options
                try:
                    await page.locator('text="Select account"').first.click()
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

                ga_options = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('.custom_select'))
                        .filter(s => s.querySelector('.select_header') &&
                                     s.querySelector('.select_header').textContent.includes('account'))
                        .flatMap(s => Array.from(s.querySelectorAll('.select_body_item'))
                                         .map(el => el.textContent.trim()))
                        .filter(t => t.length > 0)
                """)

                # Fallback: grab all visible select items
                if not ga_options:
                    ga_options = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('.select_body_item'))
                                   .map(el => el.textContent.trim()).filter(t => t.length > 0)
                    """)

                if not ga_options:
                    self._q.put(("error", "No game accounts found. Check your credentials."))
                    await browser.close()
                    return

                discovered = {}
                for ga in ga_options:
                    self._q.put(("log", f"  Account: {ga}", "gold"))
                    try:
                        await page.locator(f'.select_body_item:has-text("{ga}")').first.click()
                        await asyncio.sleep(1)
                        # Click character dropdown to reveal options
                        try:
                            await page.locator('text="Select character"').first.click()
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass
                        chars = await page.evaluate("""
                            () => Array.from(document.querySelectorAll('.custom_select'))
                                .filter(s => s.querySelector('.select_header') &&
                                             s.querySelector('.select_header').textContent.includes('character'))
                                .flatMap(s => Array.from(s.querySelectorAll('.select_body_item'))
                                                 .map(el => el.textContent.trim()))
                                .filter(t => t.length > 0)
                        """)
                        discovered[ga] = chars
                        for c in chars:
                            self._q.put(("log", f"    → {c}", "dim"))
                        # Re-open account dropdown for next iteration
                        try:
                            await page.locator('text="Select account"').first.click()
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass
                    except Exception as e:
                        discovered[ga] = []

                await browser.close()
                self._q.put(("done", discovered))

        except Exception as e:
            self._q.put(("error", str(e)))

    # ── Queue poll ────────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                item = self._q.get_nowait()
                if item[0] == "log":
                    self._disc_append(item[1], item[2])
                elif item[0] == "error":
                    self._disc_append(f"❌ {item[1]}", "error")
                    messagebox.showerror("Discovery Failed", item[1], parent=self)
                    self._show_page1()
                elif item[0] == "done":
                    self._discovered = item[1]
                    self._show_page3()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)


# ─── Main App ─────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("L2Reborn Auto-Vote")
        self.configure(bg=BG_DARK)
        self.geometry("900x620")
        self.minsize(820, 540)

        self.cfg             = load_config()
        self.log_queue       = queue.Queue()
        self.running         = False
        self.account_widgets = []

        self._build_ui()
        self._refresh_accounts()
        self._poll_log()
        self._refresh_schedule_btn()

        # First run: no accounts yet
        if not self.cfg["ACCOUNTS"]:
            self.after(400, self._first_run)

    def _first_run(self):
        if not self.cfg["TWOCAPTCHA_KEY"]:
            messagebox.showinfo(
                "Welcome to L2Reborn Auto-Vote",
                "Welcome! Let's get you set up.\n\n"
                "Step 1: Enter your 2Captcha API key in Settings.\n"
                "Step 2: Click '+ Add' to add your account — the app will log in\n"
                "and automatically find your game accounts and characters.",
                parent=self
            )
            self._open_settings()
        else:
            messagebox.showinfo(
                "Welcome",
                "No accounts set up yet.\nClick '+ Add' to add your first account.",
                parent=self
            )

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        topbar = tk.Frame(self, bg=BG_PANEL, height=56)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)

        tk.Label(topbar, text="⚔  L2REBORN AUTO-VOTE", bg=BG_PANEL, fg=GOLD,
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=20, pady=12)

        tk.Button(topbar, text="⚙", bg=BG_PANEL, fg=TEXT_DIM, relief="flat",
                  font=("Segoe UI", 13), cursor="hand2",
                  command=self._open_settings).pack(side="left", padx=(0, 10), pady=10)

        self.schedule_btn = tk.Button(topbar, text="", bg=GOLD_DIM, fg=GOLD,
                                      relief="flat", font=FONT_BOLD, cursor="hand2",
                                      padx=14, pady=5, command=self._toggle_schedule)
        self.schedule_btn.pack(side="right", padx=12, pady=10)

        self.run_btn = tk.Button(topbar, text="▶  Run Now", bg=GOLD, fg=BG_DARK,
                                 relief="flat", font=FONT_BOLD, cursor="hand2",
                                 padx=14, pady=5, command=self._run_now)
        self.run_btn.pack(side="right", padx=(0, 6), pady=10)

        tk.Frame(self, bg=GOLD_DIM, height=1).pack(fill="x")

        body = tk.Frame(self, bg=BG_DARK)
        body.pack(fill="both", expand=True)

        # Left panel
        left = tk.Frame(body, bg=BG_DARK, width=310)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        acct_header = tk.Frame(left, bg=BG_DARK)
        acct_header.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(acct_header, text="ACCOUNTS", bg=BG_DARK, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Button(acct_header, text="+ Add", bg=GOLD_DIM, fg=GOLD, relief="flat",
                  font=FONT_SMALL, cursor="hand2", padx=8, pady=2,
                  command=self._add_account).pack(side="right")

        self.acct_frame = tk.Frame(left, bg=BG_DARK)
        self.acct_frame.pack(fill="both", expand=True, padx=10)

        # Divider
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        # Right panel: log
        right = tk.Frame(body, bg=BG_DARK)
        right.pack(side="right", fill="both", expand=True)

        log_header = tk.Frame(right, bg=BG_DARK)
        log_header.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(log_header, text="LIVE LOG", bg=BG_DARK, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Button(log_header, text="Clear", bg=BG_CARD, fg=TEXT_DIM, relief="flat",
                  font=FONT_SMALL, cursor="hand2", padx=8, pady=2,
                  command=self._clear_log).pack(side="right")

        log_frame = tk.Frame(right, bg=BG_PANEL)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log_box = tk.Text(log_frame, bg=BG_PANEL, fg=TEXT, font=FONT_LOG,
                               relief="flat", wrap="word", state="disabled",
                               selectbackground=GOLD_DIM, padx=10, pady=8)
        sb = tk.Scrollbar(log_frame, command=self.log_box.yview,
                          bg=BG_PANEL, troughcolor=BG_PANEL, relief="flat")
        self.log_box.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_box.pack(fill="both", expand=True)

        for tag, fg in [("gold", GOLD), ("success", SUCCESS), ("error", ERROR),
                        ("warning", WARNING), ("dim", TEXT_DIM), ("white", TEXT)]:
            self.log_box.tag_configure(tag, foreground=fg)

    # ── Account Cards ─────────────────────────────────────────────────────────

    def _refresh_accounts(self):
        for w in self.acct_frame.winfo_children():
            w.destroy()
        self.account_widgets.clear()
        for idx, acct in enumerate(self.cfg["ACCOUNTS"]):
            self._make_account_card(idx, acct)

    def _make_account_card(self, idx, acct):
        enabled = acct.get("enabled", True)
        card    = tk.Frame(self.acct_frame, bg=BG_CARD)
        card.pack(fill="x", pady=4)

        tk.Frame(card, bg=GOLD if enabled else BORDER, width=3).pack(side="left", fill="y")

        inner = tk.Frame(card, bg=BG_CARD)
        inner.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        hrow = tk.Frame(inner, bg=BG_CARD)
        hrow.pack(fill="x")

        status_dot = tk.Label(hrow, text="●", bg=BG_CARD,
                              fg=GOLD if enabled else TEXT_DIM, font=("Segoe UI", 9))
        status_dot.pack(side="left")

        label_text = acct["label"] + (" [DEFAULT]" if idx == 0 else "")
        tk.Label(hrow, text=label_text, bg=BG_CARD,
                 fg=TEXT if enabled else TEXT_DIM, font=FONT_BOLD,
                 anchor="w").pack(side="left", padx=(4, 0))

        btn_row = tk.Frame(hrow, bg=BG_CARD)
        btn_row.pack(side="right")
        tk.Button(btn_row, text="Disable" if enabled else "Enable",
                  bg=BG_DARK, fg=TEXT_DIM if enabled else GOLD,
                  relief="flat", font=FONT_SMALL, cursor="hand2", padx=6, pady=2,
                  command=lambda i=idx: self._toggle_account(i)).pack(side="left", padx=2)
        tk.Button(btn_row, text="✕", bg=BG_DARK, fg=TEXT_DIM, relief="flat",
                  font=FONT_SMALL, cursor="hand2", padx=6, pady=2,
                  command=lambda i=idx: self._remove_account(i)).pack(side="left")

        tk.Label(inner, text=acct["email"], bg=BG_CARD, fg=TEXT_DIM,
                 font=FONT_SMALL, anchor="w").pack(fill="x", pady=(2, 0))
        tk.Label(inner,
                 text=f"{acct['game_account']}  ›  {acct.get('character') or '(auto)'}",
                 bg=BG_CARD, fg=GOLD if enabled else TEXT_DIM,
                 font=FONT_SMALL, anchor="w").pack(fill="x")

        status_var = tk.StringVar(value="Idle")
        status_lbl = tk.Label(inner, textvariable=status_var, bg=BG_CARD,
                              fg=TEXT_DIM, font=FONT_SMALL, anchor="w")
        status_lbl.pack(fill="x", pady=(4, 0))

        sname = f"Gold{idx}.Horizontal.TProgressbar"
        style = ttk.Style()
        style.theme_use("default")
        style.configure(sname, troughcolor=BG_DARK, background=GOLD,
                        bordercolor=BG_DARK, lightcolor=GOLD, darkcolor=GOLD)
        prog = ttk.Progressbar(inner, style=sname, mode="determinate", maximum=100)
        prog.pack(fill="x", pady=(4, 0))

        self.account_widgets.append({
            "status_var": status_var, "status_lbl": status_lbl,
            "status_dot": status_dot, "prog": prog,
        })

    def set_account_status(self, idx, text, color=TEXT_DIM, progress=None):
        self.log_queue.put(("status", idx, text, color, progress))

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, msg, tag="white"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(("log", f"[{ts}]  {msg}\n", tag))

    def _poll_log(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if item[0] == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", item[1], item[2])
                    self.log_box.configure(state="disabled")
                    self.log_box.see("end")
                elif item[0] == "status":
                    _, idx, text, color, progress = item
                    if idx < len(self.account_widgets):
                        w = self.account_widgets[idx]
                        w["status_var"].set(text)
                        w["status_lbl"].configure(fg=color)
                        w["status_dot"].configure(fg=color)
                        if progress is not None:
                            w["prog"]["value"] = progress
                elif item[0] == "done":
                    self.running = False
                    self.run_btn.configure(state="normal", text="▶  Run Now", bg=GOLD)
                    self.log("─" * 50, "dim")
        except queue.Empty:
            pass
        self.after(80, self._poll_log)

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ── Settings ──────────────────────────────────────────────────────────────

    def _open_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        self.wait_window(dlg)
        if dlg.result is not None:
            self.cfg["TWOCAPTCHA_KEY"] = dlg.result
            save_config(self.cfg)
            self.log("Settings saved ✓", "success")

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run_now(self):
        if self.running:
            return
        self.running = True
        self.run_btn.configure(state="disabled", text="Running…", bg=GOLD_DIM)
        self.log("Starting vote run for all enabled accounts", "gold")
        self.log("─" * 50, "dim")
        threading.Thread(target=self._run_thread, daemon=True).start()

    def _run_thread(self):
        asyncio.run(self._run_all())
        self.log_queue.put(("done",))

    async def _run_all(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.log("ERROR: Playwright not installed. Run setup.bat first.", "error")
            return

        active = [(i, a) for i, a in enumerate(self.cfg["ACCOUNTS"]) if a.get("enabled", True)]
        if not active:
            self.log("No enabled accounts to process.", "warning")
            return

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            for list_idx, (cfg_idx, acct) in enumerate(active):
                self.log(f"Account: {acct['label']}", "gold")
                self.set_account_status(cfg_idx, "Starting…", GOLD, 5)
                ctx  = await browser.new_context()
                page = await ctx.new_page()
                try:
                    await self._process_account(page, acct, cfg_idx)
                except Exception as e:
                    self.log(f"  Unexpected error: {e}", "error")
                    self.set_account_status(cfg_idx, f"Error: {e}", ERROR, 0)
                finally:
                    await ctx.close()
                if list_idx < len(active) - 1:
                    self.log("  Waiting 5s before next account…", "dim")
                    await asyncio.sleep(5)
            await browser.close()

    async def _process_account(self, page, acct, idx):
        self.log(f"  Logging in as {acct['email']}…", "dim")
        self.set_account_status(idx, "Logging in…", WARNING, 15)
        await page.goto("https://l2reborn.org/signin/", wait_until="networkidle")
        await page.fill('input[type="email"], input[name="email"]', acct["email"])
        await page.fill('input[type="password"]', acct["password"])

        try:
            sk = await page.get_attribute('[data-sitekey]', 'data-sitekey', timeout=3000)
            if sk:
                self.log("  Solving Turnstile CAPTCHA…", "dim")
                self.set_account_status(idx, "Solving CAPTCHA…", WARNING, 20)
                token = await asyncio.to_thread(
                    _solve_turnstile_static, self.cfg["TWOCAPTCHA_KEY"], sk, page.url
                )
                await page.evaluate(
                    f"document.querySelector('[name=\"cf-turnstile-response\"]').value = '{token}'"
                )
        except Exception:
            pass

        await page.locator('input[type="submit"], button[type="submit"]').first.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)

        if any(kw in page.url for kw in ("verify", "confirm", "check", "email")):
            self.log("  Email verification needed — checking Gmail…", "warning")
            self.set_account_status(idx, "Awaiting email verification…", WARNING, 25)
            for attempt in range(12):
                link = await asyncio.to_thread(
                    _fetch_verification_link_static, acct["email"], acct["gmail_app_pw"]
                )
                if link:
                    self.log("  Verification link found — clicking…", "dim")
                    await page.goto(link, wait_until="networkidle")
                    await asyncio.sleep(2)
                    if "signin" in page.url:
                        await page.fill('input[type="email"], input[name="email"]', acct["email"])
                        await page.fill('input[type="password"]', acct["password"])
                        await page.locator('input[type="submit"], button[type="submit"]').first.click()
                        await page.wait_for_load_state("networkidle")
                    break
                self.log(f"  No email yet ({attempt+1}/12) — retrying in 15s…", "dim")
                await asyncio.sleep(15)

        content = await page.content()
        if acct["email"].split("@")[0] not in content and acct["email"] not in content:
            self.log(f"  Login failed for {acct['label']}", "error")
            self.set_account_status(idx, "Login failed", ERROR, 0)
            return

        self.log("  Logged in ✓", "success")
        self.set_account_status(idx, "Logged in — opening shop…", GOLD, 35)

        await page.goto("https://l2reborn.org/shop/#essence", wait_until="networkidle")
        await asyncio.sleep(2)
        for tab in ("Essence", "Aden"):
            try:
                await page.locator(f'text="{tab}"').first.click()
                await asyncio.sleep(1)
            except Exception:
                pass

        self.log("  Opening Receive dialog…", "dim")
        self.set_account_status(idx, "Opening reward dialog…", GOLD, 45)
        clicked = await page.evaluate("""
            () => {
                const els = Array.from(
                    document.querySelectorAll('.btn_recive_shop.js-open-shop-service')
                );
                const v = els.find(el => el.getBoundingClientRect().width > 0);
                if (v) { v.click(); return true; }
                return false;
            }
        """)
        if not clicked:
            self.log("  Could not find Receive button", "error")
            self.set_account_status(idx, "Receive button not found", ERROR, 0)
            return

        await asyncio.sleep(2)

        ga = acct["game_account"]
        self.log(f"  Selecting account: {ga}", "dim")
        self.set_account_status(idx, f"Selecting {ga}…", GOLD, 52)
        try:
            await page.locator('text="Select account"').first.click()
            await asyncio.sleep(0.5)
            await page.locator(f'text="{ga}"').first.click()
            await asyncio.sleep(0.5)
        except Exception as e:
            self.log(f"  Could not select account: {e}", "error")
            self.set_account_status(idx, "Account selection failed", ERROR, 0)
            return

        char = acct.get("character")
        self.log(f"  Selecting character: {char or '(auto)'}", "dim")
        self.set_account_status(idx, f"Selecting {char or 'character'}…", GOLD, 58)
        try:
            await page.locator('text="Select character"').first.click()
            await asyncio.sleep(0.5)
            if char:
                opt = page.locator(f'text="{char}"').first
                await opt.scroll_into_view_if_needed()
                await opt.click()
            else:
                await page.locator('.select_body_item').first.click()
            await asyncio.sleep(0.5)
        except Exception as e:
            self.log(f"  Could not select character: {e}", "error")
            self.set_account_status(idx, "Character selection failed", ERROR, 0)
            return

        self.log("  Waiting for 60-second countdown…", "dim")
        for i in range(75):
            elapsed_pct = min(58 + int(i / 75 * 35), 93)
            remaining   = max(60 - i, 0)
            self.set_account_status(idx, f"Waiting… {remaining}s remaining", GOLD, elapsed_pct)
            try:
                btn = page.locator('text="Get reward"').first
                if await btn.is_visible(timeout=500):
                    self.log("  Timer complete — claiming reward!", "gold")
                    self.set_account_status(idx, "Claiming reward…", GOLD, 95)
                    await btn.click()
                    await asyncio.sleep(3)
                    if await page.locator('text="Purchase success"').count() > 0:
                        self.log(f"  ✅  {acct['label']} — 12h Exp Rune claimed!", "success")
                        self.set_account_status(idx, "✅  Reward claimed!", SUCCESS, 100)
                    else:
                        self.log("  ❌  Reward button clicked but no confirmation", "error")
                        self.set_account_status(idx, "Claim failed — no confirmation", ERROR, 0)
                    return
            except Exception:
                pass
            await asyncio.sleep(1)

        self.log("  Timed out waiting for Get reward button", "error")
        self.set_account_status(idx, "Timed out", ERROR, 0)

    # ── Account management ────────────────────────────────────────────────────

    def _add_account(self):
        dlg = AddAccountWizard(self, self.cfg.get("TWOCAPTCHA_KEY", ""))
        self.wait_window(dlg)
        if dlg.result:
            self.cfg["ACCOUNTS"].append(dlg.result)
            save_config(self.cfg)
            self._refresh_accounts()
            self.log(f"Account added: {dlg.result['label']}", "success")

    def _remove_account(self, idx):
        acct = self.cfg["ACCOUNTS"][idx]
        if messagebox.askyesno("Remove Account", f"Remove '{acct['label']}'?", parent=self):
            self.cfg["ACCOUNTS"].pop(idx)
            save_config(self.cfg)
            self._refresh_accounts()
            self.log(f"Account removed: {acct['label']}", "warning")

    def _toggle_account(self, idx):
        acct = self.cfg["ACCOUNTS"][idx]
        acct["enabled"] = not acct.get("enabled", True)
        save_config(self.cfg)
        self._refresh_accounts()
        self.log(f"{acct['label']} {'enabled' if acct['enabled'] else 'disabled'}", "dim")

    # ── Schedule ──────────────────────────────────────────────────────────────

    def _refresh_schedule_btn(self):
        if is_scheduled():
            self.schedule_btn.configure(text="🟢  Auto-vote ON", bg="#1a2a1a", fg=SUCCESS)
        else:
            self.schedule_btn.configure(text="⏱  Schedule 12h", bg=GOLD_DIM, fg=GOLD)
        self.after(5000, self._refresh_schedule_btn)

    def _toggle_schedule(self):
        if is_scheduled():
            try:
                subprocess.run(["schtasks", "/delete", "/tn", "L2Reborn AutoVote", "/f"],
                               check=True)
                self.log("Auto-vote schedule removed.", "warning")
            except Exception as e:
                self.log(f"Could not remove schedule: {e}", "error")
        else:
            try:
                now = datetime.now().strftime("%H:%M")
                subprocess.run([
                    "schtasks", "/create",
                    "/tn", "L2Reborn AutoVote",
                    "/tr", f'python "{CLAIM_PATH}"',
                    "/sc", "HOURLY", "/mo", "12",
                    "/st", now, "/rl", "HIGHEST", "/f",
                ], check=True)
                self.log("Auto-vote scheduled every 12 hours ✓", "success")
            except Exception as e:
                self.log(f"Schedule failed (try running as Administrator): {e}", "error")
        self._refresh_schedule_btn()


if __name__ == "__main__":
    app = App()
    app.mainloop()
