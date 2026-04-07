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
from urllib.parse import urlparse, parse_qs

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
STATUS_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.json")


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
            a.setdefault("server", "")
            a.setdefault("server_id", "")
            a.setdefault("character_id", "")
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
            f'        "server":       "{a.get("server", "")}",\n',
            f'        "server_id":    "{a.get("server_id", "")}",\n',
            f'        "game_account": "{a["game_account"]}",\n',
            f'        "character":    {char_val},\n',
            f'        "character_id": "{a.get("character_id", "")}",\n',
            f'        "enabled":      {a.get("enabled", True)},\n',
            "    },\n",
        ]
    lines.append("]\n")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def load_status():
    try:
        import json
        with open(STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_status(data):
    try:
        import json
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def is_scheduled():
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", "L2Reborn AutoVote"],
                           capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False

def get_next_scheduled_run():
    """Return datetime of next scheduled run, or None if not scheduled."""
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", "L2Reborn AutoVote", "/fo", "LIST", "/v"],
            capture_output=True, encoding="cp1252",
        )
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            if "Next Run Time" in line:
                val = line.split(":", 1)[1].strip()
                if val.lower() in ("n/a", "disabled", ""):
                    return None
                # Format: "07-Apr-26 11:35:00 PM"
                return datetime.strptime(val, "%d-%b-%y %I:%M:%S %p")
    except Exception:
        pass
    return None


# ─── Static helpers (shared by wizard + main app) ─────────────────────────────

def _solve_turnstile_static(api_key, site_key, page_url):
    import requests
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
            return data["solution"]["token"]
        if data.get("errorId") != 0:
            raise RuntimeError(f"2captcha poll error: {data}")
    raise RuntimeError("Turnstile timed out — check your 2captcha balance")


def _fetch_verification_link_static(gmail_user, app_pw):
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

    def __init__(self, parent, twocaptcha_key,
                 prefill_email="", prefill_password="", prefill_app_pw=""):
        super().__init__(parent)
        self.title("Add Account" if not prefill_email else "Re-discover Account")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self.result         = None
        self.twocaptcha_key = twocaptcha_key
        self._q             = queue.Queue()
        self._discovered    = {}   # {server: {game_account: [char1, char2, …]}}
        self._disc_ids      = {}   # {server: {"_sid": server_id, game_account: {char_name: char_id}}}
        self._disc_email    = prefill_email
        self._disc_pw       = prefill_password
        self._disc_app_pw   = prefill_app_pw
        self.grab_set()
        self._container = tk.Frame(self, bg=BG_DARK)
        self._container.pack(fill="both", expand=True)
        if prefill_email:
            # Skip page 1, go straight to discovery
            self._show_page2()
            threading.Thread(
                target=lambda: asyncio.run(
                    self._run_discovery(prefill_email, prefill_password, prefill_app_pw)
                ),
                daemon=True,
            ).start()
        else:
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
        total_accts = sum(len(v) for v in self._discovered.values())
        self.geometry("460x440")

        tk.Label(self._container, text="Add Account  —  Step 2 of 2",
                 bg=BG_DARK, fg=GOLD, font=FONT_BOLD
                 ).pack(pady=(18, 4), padx=20, anchor="w")
        tk.Frame(self._container, bg=GOLD_DIM, height=1).pack(fill="x", padx=20)
        tk.Label(self._container,
                 text=f"Found {total_accts} account(s) across {len(self._discovered)} server(s).",
                 bg=BG_DARK, fg=TEXT_DIM, font=FONT_SMALL
                 ).pack(pady=(10, 0), padx=20, anchor="w")

        def lbl(text):
            tk.Label(self._container, text=text, bg=BG_DARK, fg=TEXT_DIM,
                     font=FONT_SMALL, anchor="w").pack(fill="x", padx=20, pady=(10, 0))

        def make_menu(var, options, cmd=None):
            opts = options if options else [""]
            m = tk.OptionMenu(self._container, var, *opts,
                              command=cmd if cmd else lambda _: None)
            m.configure(bg=BG_CARD, fg=TEXT, activebackground=GOLD_DIM,
                        activeforeground=TEXT, relief="flat", font=FONT_MAIN,
                        highlightthickness=0, anchor="w")
            m["menu"].configure(bg=BG_CARD, fg=TEXT, activebackground=GOLD_DIM)
            m.pack(fill="x", padx=20, ipady=3)
            return m

        # Server dropdown
        lbl("Server")
        srv_names = list(self._discovered.keys())
        self._srv_var = tk.StringVar(value=srv_names[0] if srv_names else "")
        make_menu(self._srv_var, srv_names, cmd=self._on_srv_change)

        # Game account dropdown
        lbl("Game Account")
        self.ga_var = tk.StringVar()
        self._ga_menu = make_menu(self.ga_var, [""], cmd=self._on_ga_change)

        # Character dropdown
        lbl("Character")
        self.char_var = tk.StringVar()
        self._char_menu = make_menu(self.char_var, [""])

        # Seed cascades from first server
        if srv_names:
            self._on_srv_change(srv_names[0])

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

    def _on_srv_change(self, selected):
        accounts = list(self._discovered.get(selected, {}).keys())
        menu = self._ga_menu["menu"]
        menu.delete(0, "end")
        for a in accounts:
            menu.add_command(label=a, command=lambda v=a: (self.ga_var.set(v), self._on_ga_change(v)))
        first = accounts[0] if accounts else ""
        self.ga_var.set(first)
        self._on_ga_change(first)

    def _on_ga_change(self, selected):
        srv   = self._srv_var.get()
        chars = self._discovered.get(srv, {}).get(selected, [])
        menu  = self._char_menu["menu"]
        menu.delete(0, "end")
        for c in chars:
            menu.add_command(label=c, command=lambda v=c: self.char_var.set(v))
        self.char_var.set(chars[0] if chars else "")

    def _confirm(self):
        ga   = self.ga_var.get().strip()
        char = self.char_var.get().strip()
        srv  = self._srv_var.get()
        lbl  = self.f_label.get().strip() or f"Account {self._disc_email.split('@')[0]}"
        srv_ids    = self._disc_ids.get(srv, {})
        server_id  = srv_ids.get("_sid", "")
        char_id    = srv_ids.get(ga, {}).get(char, "")
        if not ga:
            messagebox.showerror("Missing", "Please select a game account.", parent=self)
            return
        self.result = {
            "label":        lbl,
            "email":        self._disc_email,
            "password":     self._disc_pw,
            "gmail_app_pw": self._disc_app_pw,
            "server":       srv,
            "server_id":    server_id,
            "game_account": ga,
            "character":    char or None,
            "character_id": char_id,
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

        if not self.twocaptcha_key:
            self._q.put(("error", "2Captcha API key is missing. Add it in Settings first."))
            return

        async def _ajax_login(page, wfls_token=""):
            self._q.put(("log", "Requesting CAPTCHA solve from 2captcha…", "dim"))
            token = await asyncio.to_thread(
                _solve_turnstile_static, self.twocaptcha_key,
                TURNSTILE_KEY, "https://l2reborn.org/signin/"
            )
            self._q.put(("log", "Token received — submitting login via AJAX…", "dim"))
            return await page.evaluate("""
                async ({ email, password, token, wflsToken }) => {
                    const nr = await fetch('/wp-admin/admin-ajax.php', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                        body: new URLSearchParams({ action: 'l2mgm_nonce', nonce_name: 'l2mgm_login' }).toString()
                    });
                    const nd = await nr.json();
                    if (!nd.success) return { success: false, error: 'nonce failed', detail: JSON.stringify(nd) };
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
            """, {"email": email, "password": password, "token": token, "wflsToken": wfls_token})

        _dlog_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_run.txt")
        def _dlog(msg):
            self._q.put(("log", msg, "dim"))
            try:
                with open(_dlog_path, "a", encoding="utf-8") as _f:
                    _f.write(f"{datetime.now().isoformat()} {msg}\n")
            except Exception:
                pass
        # Clear previous log
        try:
            open(_dlog_path, "w").close()
        except Exception:
            pass
        _dlog("=== discovery started ===")

        try:
            async with async_playwright() as pw:
                _dlog("launching browser")
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-blink-features=AutomationControlled"],
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
                _dlog("browser ready — navigating to signin")

                self._q.put(("log", "Connecting to l2reborn.org…", "dim"))
                await page.goto("https://l2reborn.org/signin/", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)

                        # ── AJAX login ────────────────────────────────────────────
                _dlog("starting AJAX login")
                login_result = await _ajax_login(page)
                _dlog(f"login result: {login_result}")
                self._q.put(("log", f"Login: success={login_result.get('success')}  error={login_result.get('error', 'none')}", "dim"))

                # Retry once if captcha was rejected
                if not login_result.get("success") and "captcha" in str(login_result.get("error", "")).lower():
                    self._q.put(("log", "Captcha rejected — retrying…", "warning"))
                    await asyncio.sleep(3)
                    login_result = await _ajax_login(page)
                    self._q.put(("log", f"Retry: success={login_result.get('success')}  error={login_result.get('error', 'none')}", "dim"))

                # Handle email verification
                error_msg = str(login_result.get("error", "")).lower()
                if not login_result.get("success") and "verif" in error_msg:
                    self._q.put(("log", "Email verification needed — checking Gmail…", "warning"))
                    for attempt in range(12):
                        link = await asyncio.to_thread(
                            _fetch_verification_link_static, email, gmail_app_pw
                        )
                        if link:
                            self._q.put(("log", "Verification link found — confirming…", "dim"))
                            vp = await ctx.new_page()
                            await vp.goto(link, wait_until="domcontentloaded", timeout=60000)
                            await asyncio.sleep(2)
                            await vp.close()
                            params = parse_qs(urlparse(link).query)
                            wfls_token = params.get("wfls-email-verification", [""])[0]
                            login_result = await _ajax_login(page, wfls_token)
                            self._q.put(("log", f"Re-login: success={login_result.get('success')}", "dim"))
                            break
                        self._q.put(("log", f"No email yet ({attempt+1}/12) — retrying in 15s…", "dim"))
                        await asyncio.sleep(15)

                if not login_result.get("success"):
                    self._q.put(("error", f"Login failed: {login_result.get('error', login_result)}"))
                    await browser.close()
                    return

                self._q.put(("log", "Logged in ✓  Fetching servers…", "success"))
                _dlog("login success — fetching servers from shop AJAX")

                # ── Fetch server list first (shop page, AJAX then DOM fallback) ──
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
                _dlog(f"server_map: {server_map}")
                self._q.put(("log", f"Servers found: {list(server_map.values())}", "dim"))

                # ── Try AJAX character endpoint ────────────────────────────────
                await page.goto("https://l2reborn.org/account/", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(3)

                # Save account page for diagnostics
                try:
                    _acct_html = await page.content()
                    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_account.html"), "w", encoding="utf-8") as _f:
                        _f.write(_acct_html)
                    _dlog("saved debug_account.html")
                except Exception as _ex:
                    _dlog(f"account save err: {_ex}")

                _dlog("on /account/ — running AJAX char check")
                api_raw = await page.evaluate("""
                    async () => {
                        const results = {};
                        for (const action of ['l2mgm_get_characters', 'l2mgm_get_account_characters']) {
                            try {
                                const r = await fetch('/wp-admin/admin-ajax.php?action=' + action);
                                const text = await r.text();
                                try { results[action] = JSON.parse(text); }
                                catch(e) { results[action] = { raw: text.slice(0, 200) }; }
                            } catch(e) {
                                results[action] = { error: String(e) };
                            }
                        }
                        // Also check if user is logged in
                        try {
                            const lr = await fetch('/wp-admin/admin-ajax.php?action=l2mgm_logged');
                            results['logged'] = await lr.json();
                        } catch(e) {}
                        return results;
                    }
                """)
                _dlog(f"AJAX raw: {api_raw}")

                api_chars = []
                for action_key in ['l2mgm_get_characters', 'l2mgm_get_account_characters']:
                    d = api_raw.get(action_key, {})
                    if isinstance(d, dict) and d.get('success') and isinstance(d.get('data'), list) and len(d['data']):
                        api_chars = d['data']
                        _dlog(f"Using {action_key}: {len(api_chars)} chars")
                        break

                discovered = {}
                disc_ids   = {}  # {srv_name: {"_sid": srv_id, acct_name: {char_name: char_id}}}

                if api_chars:
                    self._q.put(("log", f"API returned {len(api_chars)} character(s)", "dim"))
                    for c in api_chars:
                        srv_id    = str(c.get("server_id") or c.get("serverId") or "")
                        srv_name  = server_map.get(srv_id, f"Server {srv_id}" if srv_id else "Default")
                        acct_name = c.get("account") or c.get("login") or "Default"
                        char_name = c.get("name") or c.get("char_name") or c.get("nickname") or ""
                        char_id   = str(c.get("id") or "")
                        if not char_name:
                            continue
                        discovered.setdefault(srv_name, {})
                        discovered[srv_name].setdefault(acct_name, [])
                        disc_ids.setdefault(srv_name, {"_sid": srv_id})
                        disc_ids[srv_name].setdefault(acct_name, {})
                        if char_name not in discovered[srv_name][acct_name]:
                            discovered[srv_name][acct_name].append(char_name)
                            disc_ids[srv_name][acct_name][char_name] = char_id
                            self._q.put(("log", f"  [{srv_name}] {acct_name}  →  {char_name}", "gold"))

                # ── Fallback: account page DOM scraping ───────────────────────
                _dlog(f"api_chars={len(api_chars)} discovered={bool(discovered)}")
                if not discovered:
                    self._q.put(("log", "API empty — scraping account page DOM…", "dim"))
                    _dlog("entering account page DOM fallback")

                    dom_accounts = await page.evaluate("""
                        () => {
                            const result = [];
                            document.querySelectorAll('.account_rows[data-div-id]').forEach(row => {
                                const accountName = row.querySelector('.text_12_account2b, .text_12_account2')?.textContent.trim()
                                                 || row.dataset.divId;
                                const chars = [];
                                row.querySelectorAll('.btn_unstuck[data-char-id]').forEach(btn => {
                                    if (btn.dataset.charId && btn.dataset.charName) {
                                        chars.push({
                                            id: btn.dataset.charId,
                                            name: btn.dataset.charName,
                                            serverId: btn.dataset.serverId || '',
                                        });
                                    }
                                });
                                if (accountName && chars.length > 0) {
                                    result.push({ name: accountName, chars });
                                }
                            });
                            return result;
                        }
                    """)
                    _dlog(f"DOM accounts: {dom_accounts}")

                    for ga in dom_accounts:
                        acct_name = ga["name"]
                        for c in ga["chars"]:
                            srv_id   = c["serverId"]
                            srv_name = server_map.get(srv_id, f"Server {srv_id}" if srv_id else "Default")
                            discovered.setdefault(srv_name, {})
                            discovered[srv_name].setdefault(acct_name, [])
                            disc_ids.setdefault(srv_name, {"_sid": srv_id})
                            disc_ids[srv_name].setdefault(acct_name, {})
                            if c["name"] not in discovered[srv_name][acct_name]:
                                discovered[srv_name][acct_name].append(c["name"])
                                disc_ids[srv_name][acct_name][c["name"]] = c["id"]
                                self._q.put(("log", f"  [{srv_name}] {acct_name}  →  {c['name']}", "gold"))

                if not discovered:
                    self._q.put(("error", "No game accounts found. Log in manually to verify your account has characters."))
                    await browser.close()
                    return

                await browser.close()
                self._q.put(("done", (discovered, disc_ids)))

        except Exception as e:
            import traceback
            _dlog(f"EXCEPTION: {e}\n{traceback.format_exc()}")
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
                    self._discovered, self._disc_ids = item[1]
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
        self._stop_requested = False
        self._browser_ref    = None
        self.account_widgets = []

        self._build_ui()
        self._refresh_accounts()
        self._poll_log()
        self._refresh_schedule_btn()
        self._tick_cooldowns()

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

        self.sync_btn = tk.Button(topbar, text="Sync", bg=BG_PANEL, fg=TEXT_DIM,
                                  relief="flat", font=FONT_BOLD, cursor="hand2",
                                  padx=10, pady=5, command=self._sync_status)
        self.sync_btn.pack(side="right", padx=(0, 4), pady=10)

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
        self._tick_cooldowns()

    def _make_account_card(self, idx, acct):
        enabled = acct.get("enabled", True)
        card    = tk.Frame(self.acct_frame, bg=BG_CARD)
        card.pack(fill="x", pady=4)

        tk.Frame(card, bg=GOLD if enabled else BORDER, width=3).pack(side="left", fill="y")

        inner = tk.Frame(card, bg=BG_CARD)
        inner.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        hrow = tk.Frame(inner, bg=BG_CARD)
        hrow.pack(fill="x")

        # Pack right-side buttons FIRST so they always get space
        btn_row = tk.Frame(hrow, bg=BG_CARD)
        btn_row.pack(side="right")
        tk.Button(btn_row, text="Disable" if enabled else "Enable",
                  bg=BG_DARK, fg=TEXT_DIM if enabled else GOLD,
                  relief="flat", font=FONT_SMALL, cursor="hand2", padx=6, pady=2,
                  command=lambda i=idx: self._toggle_account(i)).pack(side="left", padx=2)
        tk.Button(btn_row, text="Rediscover", bg=BG_DARK, fg=GOLD_DIM, relief="flat",
                  font=FONT_SMALL, cursor="hand2", padx=6, pady=2,
                  command=lambda i=idx: self._rediscover_account(i)).pack(side="left", padx=2)
        tk.Button(btn_row, text="✕", bg=BG_DARK, fg=TEXT_DIM, relief="flat",
                  font=FONT_SMALL, cursor="hand2", padx=6, pady=2,
                  command=lambda i=idx: self._remove_account(i)).pack(side="left")

        status_dot = tk.Label(hrow, text="●", bg=BG_CARD,
                              fg=GOLD if enabled else TEXT_DIM, font=("Segoe UI", 9))
        status_dot.pack(side="left")

        label_text = acct["label"] + (" [DEFAULT]" if idx == 0 else "")
        tk.Label(hrow, text=label_text, bg=BG_CARD,
                 fg=TEXT if enabled else TEXT_DIM, font=FONT_BOLD,
                 anchor="w").pack(side="left", padx=(4, 0))

        tk.Label(inner, text=acct["email"], bg=BG_CARD, fg=TEXT_DIM,
                 font=FONT_SMALL, anchor="w").pack(fill="x", pady=(2, 0))
        tk.Label(inner,
                 text=f"{acct['game_account']}  ›  {acct.get('character') or '(auto)'}",
                 bg=BG_CARD, fg=GOLD if enabled else TEXT_DIM,
                 font=FONT_SMALL, anchor="w").pack(fill="x")

        acct_status = load_status().get(acct["email"], {})
        run_result  = acct_status.get("last_run_result", "")
        run_at      = acct_status.get("last_run_at", "")
        if run_result and run_at:
            try:
                run_dt   = datetime.strptime(run_at, "%Y-%m-%d %H:%M:%S")
                run_time = run_dt.strftime("%H:%M %d/%m")
            except Exception:
                run_time = run_at
            icons   = {"claimed": "✅", "cooldown": "⏳", "failed": "❌"}
            icon    = icons.get(run_result, "•")
            run_txt = f"Last run: {icon} {run_result.capitalize()}  ({run_time})"
            run_col = SUCCESS if run_result == "claimed" else (GOLD if run_result == "cooldown" else ERROR)
        else:
            run_txt = "Last run: —"
            run_col = TEXT_DIM
        tk.Label(inner, text=run_txt, bg=BG_CARD, fg=run_col,
                 font=FONT_SMALL, anchor="w").pack(fill="x")

        cooldown_var = tk.StringVar()
        cooldown_lbl = tk.Label(inner, textvariable=cooldown_var, bg=BG_CARD,
                                font=FONT_SMALL, anchor="w")
        cooldown_lbl.pack(fill="x")

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
            "cooldown_var": cooldown_var, "cooldown_lbl": cooldown_lbl,
            "email": acct["email"],
        })

    def set_account_status(self, idx, text, color=TEXT_DIM, progress=None):
        self.log_queue.put(("status", idx, text, color, progress))

    def _cooldown_text(self, email):
        from datetime import timedelta
        last = load_status().get(email, {}).get("last_claimed")
        last_str = ""
        if last:
            try:
                last_dt  = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                last_str = f"last: {last_dt.strftime('%H:%M %d/%m')}"
            except Exception:
                last_str = f"last: {last}"

        # Prefer scheduler's next run time if active
        next_dt = get_next_scheduled_run()
        if next_dt:
            remaining = next_dt - datetime.now()
            if remaining.total_seconds() <= 0:
                return f"Scheduled run imminent  ({last_str})", SUCCESS
            h, rem = divmod(int(remaining.total_seconds()), 3600)
            m = rem // 60
            suffix = f"  ({last_str})" if last_str else ""
            return f"Next run in {h}h {m:02d}m  [{next_dt.strftime('%H:%M')}]{suffix}", GOLD

        # No scheduler — fall back to last_claimed + 12h
        if not last:
            return "Never claimed", TEXT_DIM
        try:
            last_dt   = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
            next_dt   = last_dt + timedelta(hours=12)
            remaining = next_dt - datetime.now()
            if remaining.total_seconds() <= 0:
                return f"Ready to claim!  ({last_str})", SUCCESS
            h, rem = divmod(int(remaining.total_seconds()), 3600)
            m = rem // 60
            return f"Next claim in {h}h {m:02d}m  ({last_str})", GOLD
        except Exception:
            return f"Last claimed: {last}", TEXT_DIM

    def _tick_cooldowns(self):
        for w in self.account_widgets:
            if "cooldown_var" not in w:
                continue
            text, color = self._cooldown_text(w["email"])
            w["cooldown_var"].set(text)
            w["cooldown_lbl"].configure(fg=color)
        self.after(60000, self._tick_cooldowns)

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
                    self._browser_ref = None
                    self.run_btn.configure(
                        state="normal", text="▶  Run Now",
                        bg=GOLD, fg=BG_DARK, command=self._run_now
                    )
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
        self._stop_requested = False
        self.run_btn.configure(text="⏹  Stop", bg=ERROR, fg="white", command=self._stop_run)
        self.log("Starting vote run for all enabled accounts", "gold")
        self.log("─" * 50, "dim")
        threading.Thread(target=self._run_thread, daemon=True).start()

    def _stop_run(self):
        self._stop_requested = True
        self.run_btn.configure(state="disabled", text="Stopping…", bg=GOLD_DIM, fg=TEXT)
        self.log("Stop requested — finishing current operation…", "warning")
        # Force-close the browser if it's open
        if self._browser_ref is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._browser_ref.close(),
                    asyncio.get_event_loop()
                )
            except Exception:
                pass

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
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            self._browser_ref = browser
            for list_idx, (cfg_idx, acct) in enumerate(active):
                if self._stop_requested:
                    self.log("Stopped.", "warning")
                    break
                self.log(f"Account: {acct['label']}", "gold")
                self.set_account_status(cfg_idx, "Starting…", GOLD, 5)
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 720},
                )
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
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

    async def _ajax_login_account(self, page, acct, idx, wfls_token=""):
        """Login via AJAX (same method as discovery — works with Turnstile)."""
        self.log("  Requesting CAPTCHA solve from 2captcha…", "dim")
        self.set_account_status(idx, "Solving CAPTCHA…", WARNING, 20)
        token = await asyncio.to_thread(
            _solve_turnstile_static, self.cfg["TWOCAPTCHA_KEY"], TURNSTILE_KEY,
            "https://l2reborn.org/signin/"
        )
        self.log("  Token received — submitting login via AJAX…", "dim")
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
                        action: 'l2mgm_login', email: email, password: password,
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

    async def _process_account(self, page, acct, idx):
        self.log(f"  Logging in as {acct['email']}…", "dim")
        self.set_account_status(idx, "Logging in…", WARNING, 15)
        await page.goto("https://l2reborn.org/signin/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)

        result = await self._ajax_login_account(page, acct, idx)
        self.log(f"  Login response: success={result.get('success')} error={result.get('error', 'none')}", "dim")

        # Retry once if captcha was rejected by server
        if not result.get("success") and "captcha" in str(result.get("error", "")).lower():
            self.log("  Captcha rejected — retrying solve once…", "warning")
            await asyncio.sleep(3)
            result = await self._ajax_login_account(page, acct, idx)
            self.log(f"  Retry login: success={result.get('success')} error={result.get('error', 'none')}", "dim")

        if not result.get("success"):
            error = str(result.get("error", "")).lower()
            if "verif" in error:
                self.log("  Email verification needed — checking Gmail…", "warning")
                self.set_account_status(idx, "Awaiting email verification…", WARNING, 25)
                for attempt in range(12):
                    link = await asyncio.to_thread(
                        _fetch_verification_link_static, acct["email"], acct["gmail_app_pw"]
                    )
                    if link:
                        self.log("  Verification link found — confirming…", "dim")
                        vp = await page.context.new_page()
                        await vp.goto(link, wait_until="domcontentloaded", timeout=60000)
                        await asyncio.sleep(2)
                        await vp.close()
                        wfls_token = parse_qs(urlparse(link).query).get("wfls-email-verification", [""])[0]
                        result = await self._ajax_login_account(page, acct, idx, wfls_token)
                        self.log(f"  Re-login: success={result.get('success')}", "dim")
                        break
                    self.log(f"  No email yet ({attempt+1}/12) — retrying in 15s…", "dim")
                    await asyncio.sleep(15)

        if not result.get("success"):
            self.log(f"  Login failed for {acct['label']}: {result.get('error', result)}", "error")
            self.set_account_status(idx, "Login failed", ERROR, 0)
            return

        self.log("  Logged in ✓", "success")

        server_id    = acct.get("server_id", "")
        character_id = acct.get("character_id", "")
        ga           = acct["game_account"]
        char_name    = acct.get("character", "")

        if not server_id or not character_id:
            self.log("  Resolving IDs from account page…", "dim")
            self.set_account_status(idx, "Resolving IDs…", GOLD, 37)
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
                    // fallback: first char anywhere
                    const btn = document.querySelector('.btn_unstuck[data-char-id]');
                    return btn ? { serverId: btn.dataset.serverId, charId: btn.dataset.charId } : null;
                }
            """, {"ga": ga, "charName": char_name})
            if not ids:
                self.log("  Could not resolve server/character IDs from account page", "error")
                self.set_account_status(idx, "IDs not found — check account", ERROR, 0)
                return
            server_id    = ids["serverId"]
            character_id = ids["charId"]
            self.log(f"  Resolved — Server ID: {server_id}  Character ID: {character_id}", "dim")
        else:
            self.log(f"  Server ID: {server_id}  Character ID: {character_id}", "dim")

        # ── Get VIP token ──────────────────────────────────────────────────────
        self.log("  Fetching VIP token…", "dim")
        self.set_account_status(idx, "Fetching VIP token…", GOLD, 50)
        await page.goto("https://l2reborn.org/shop/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1)

        token_res = await page.evaluate("""
            async (sid) => {
                const r = await fetch('/wp-admin/admin-ajax.php?action=l2mgm_get_vip_token&server_id=' + sid);
                return await r.json();
            }
        """, server_id)

        if not (isinstance(token_res, dict) and token_res.get("success")):
            self.log(f"  Failed to get VIP token: {token_res}", "error")
            self.set_account_status(idx, "VIP token failed", ERROR, 0)
            return

        vip_token = token_res["data"]["token"]
        self.log(f"  VIP token received — waiting 65s…", "dim")

        # ── 65-second countdown ────────────────────────────────────────────────
        for i in range(65):
            elapsed_pct = min(50 + int(i / 65 * 43), 93)
            remaining   = 65 - i
            self.set_account_status(idx, f"Waiting… {remaining}s", GOLD, elapsed_pct)
            if remaining in (65, 55, 45, 35, 25, 15, 5):
                self.log(f"  {remaining}s remaining…", "dim")
            await asyncio.sleep(1)

        # ── Submit claim via AJAX ──────────────────────────────────────────────
        self.log("  Submitting claim…", "dim")
        self.set_account_status(idx, "Claiming reward…", GOLD, 95)

        shop_result = await page.evaluate("""
            async ({serverId, account, characterId, vipToken}) => {
                const nr = await fetch('/wp-admin/admin-ajax.php', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'action=l2mgm_nonce&nonce_name=shop'
                });
                const nd = await nr.json();
                const sr = await fetch('/wp-admin/admin-ajax.php', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
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
        """, {
            "serverId":    server_id,
            "account":     ga,
            "characterId": character_id,
            "vipToken":    vip_token,
        })

        code = None
        if isinstance(shop_result, dict) and isinstance(shop_result.get("data"), dict):
            code = shop_result["data"].get("error_code")
        ok = bool(isinstance(shop_result, dict) and shop_result.get("success")) or code == 3

        if ok:
            msg = "Already claimed (on cooldown)" if code == 3 else "12h Exp Rune claimed!"
            self.log(f"  {msg}", "success")
            self.set_account_status(idx, f"✅  {msg}", SUCCESS, 100)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status  = load_status()
            entry   = status.get(acct["email"], {})
            if code != 3:
                entry["last_claimed"] = now_str
            entry["last_run_at"]     = now_str
            entry["last_run_result"] = "cooldown" if code == 3 else "claimed"
            status[acct["email"]]    = entry
            save_status(status)
        else:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status  = load_status()
            entry   = status.get(acct["email"], {})
            entry["last_run_at"]     = now_str
            entry["last_run_result"] = "failed"
            status[acct["email"]]    = entry
            save_status(status)
            self.log(f"  Claim failed: {shop_result}", "error")
            self.set_account_status(idx, "Claim failed", ERROR, 0)

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

    def _rediscover_account(self, idx):
        acct = self.cfg["ACCOUNTS"][idx]
        dlg = AddAccountWizard(
            self,
            self.cfg.get("TWOCAPTCHA_KEY", ""),
            prefill_email=acct["email"],
            prefill_password=acct["password"],
            prefill_app_pw=acct["gmail_app_pw"],
        )
        self.wait_window(dlg)
        if dlg.result:
            # Preserve label and enabled state, update everything else
            dlg.result["label"]   = acct["label"]
            dlg.result["enabled"] = acct.get("enabled", True)
            self.cfg["ACCOUNTS"][idx] = dlg.result
            save_config(self.cfg)
            self._refresh_accounts()
            self.log(f"Re-discovered: {acct['label']}", "success")

    # ── Schedule ──────────────────────────────────────────────────────────────

    def _refresh_schedule_btn(self):
        if is_scheduled():
            self.schedule_btn.configure(text="🟢  Auto-vote ON", bg="#1a2a1a", fg=SUCCESS)
        else:
            self.schedule_btn.configure(text="⏱  Schedule 12h", bg=GOLD_DIM, fg=GOLD)
        self.after(5000, self._refresh_schedule_btn)

    def _sync_status(self):
        if getattr(self, "running", False):
            self.log("A run is already in progress.", "warning")
            return
        self.sync_btn.configure(text="Syncing...", fg=WARNING, state="disabled")
        self.log("Syncing status for all accounts...", "dim")
        threading.Thread(target=self._sync_thread, daemon=True).start()

    def _sync_thread(self):
        asyncio.run(self._sync_all())
        self.after(0, lambda: self.sync_btn.configure(text="Sync", fg=TEXT_DIM, state="normal"))
        self.after(0, self._refresh_accounts)

    async def _sync_all(self):
        from playwright.async_api import async_playwright
        active = [a for a in self.cfg["ACCOUNTS"] if a.get("enabled", True)]
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            for acct in active:
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720},
                )
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page = await ctx.new_page()
                try:
                    await self._sync_account(page, acct)
                except Exception as e:
                    self.log(f"  [{acct['label']}] Sync error: {e}", "error")
                finally:
                    await ctx.close()
            await browser.close()

    async def _sync_account(self, page, acct):
        self.log(f"  Syncing {acct['label']}...", "dim")
        await page.goto("https://l2reborn.org/signin/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)

        # Login
        result = await self._ajax_login_account(page, acct, -1)
        if not result.get("success") and "captcha" in str(result.get("error","")).lower():
            await asyncio.sleep(3)
            result = await self._ajax_login_account(page, acct, -1)
        if not result.get("success"):
            self.log(f"  [{acct['label']}] Login failed: {result.get('error')}", "error")
            return

        # Resolve IDs
        server_id    = acct.get("server_id", "")
        character_id = acct.get("character_id", "")
        ga           = acct["game_account"]
        char_name    = acct.get("character", "")
        if not server_id or not character_id:
            await page.goto("https://l2reborn.org/account/", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            ids = await page.evaluate("""
                ({ga, charName}) => {
                    const rows = Array.from(document.querySelectorAll('.account_rows[data-div-id]'));
                    for (const row of rows) {
                        const name = row.querySelector('.text_12_account2b, .text_12_account2')?.textContent.trim() || row.dataset.divId || '';
                        if (!name.includes(ga) && !ga.includes(name)) continue;
                        for (const btn of row.querySelectorAll('.btn_unstuck[data-char-id]')) {
                            if (!charName || btn.dataset.charName === charName)
                                return { serverId: btn.dataset.serverId, charId: btn.dataset.charId };
                        }
                    }
                    const btn = document.querySelector('.btn_unstuck[data-char-id]');
                    return btn ? { serverId: btn.dataset.serverId, charId: btn.dataset.charId } : null;
                }
            """, {"ga": ga, "charName": char_name})
            if not ids:
                self.log(f"  [{acct['label']}] Could not resolve IDs", "error")
                return
            server_id    = ids["serverId"]
            character_id = ids["charId"]

        # Get VIP token + attempt claim (proves cooldown or records fresh claim)
        await page.goto("https://l2reborn.org/shop/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1)
        token_res = await page.evaluate("""
            async (sid) => {
                const r = await fetch('/wp-admin/admin-ajax.php?action=l2mgm_get_vip_token&server_id=' + sid);
                return await r.json();
            }
        """, server_id)
        if not (isinstance(token_res, dict) and token_res.get("success")):
            self.log(f"  [{acct['label']}] VIP token failed", "error")
            return

        vip_token = token_res["data"]["token"]
        self.log(f"  [{acct['label']}] Waiting 65s for claim window...", "dim")
        await asyncio.sleep(65)

        shop_result = await page.evaluate("""
            async ({serverId, account, characterId, vipToken}) => {
                const nr = await fetch('/wp-admin/admin-ajax.php', {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'action=l2mgm_nonce&nonce_name=shop'});
                const nd = await nr.json();
                const sr = await fetch('/wp-admin/admin-ajax.php', {
                    method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
                    body: new URLSearchParams({action:'l2mgm_donation_service_v2',_wpnonce:nd.data.nonce,service:'exp_rune',server_id:serverId,account:account,character:characterId,vote_token:vipToken,vote_retries:'0'}).toString()
                });
                const raw = await sr.text();
                try { return JSON.parse(raw); } catch { return {raw}; }
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
        if ok and code != 3:
            entry["last_claimed"]    = now_str
            entry["last_run_result"] = "claimed"
            self.log(f"  [{acct['label']}] Claimed! Timestamp updated.", "success")
        elif code == 3:
            entry["last_run_result"] = "cooldown"
            self.log(f"  [{acct['label']}] On cooldown — local timer unchanged.", "dim")
        else:
            entry["last_run_result"] = "failed"
            self.log(f"  [{acct['label']}] Unexpected response: {shop_result}", "warning")
        status[acct["email"]] = entry
        save_status(status)

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
                    "/st", now, "/f",
                ], check=True)
                self.log("Auto-vote scheduled every 12 hours ✓", "success")
            except Exception as e:
                self.log(f"Schedule failed (try running as Administrator): {e}", "error")
        self._refresh_schedule_btn()


if __name__ == "__main__":
    app = App()
    app.mainloop()
