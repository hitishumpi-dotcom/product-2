# ============================================================
#  config.py — Fill in your own credentials before running
#  Use the GUI (app.py) to auto-discover server_id and character_id.
# ============================================================

# Get your 2Captcha API key at: https://2captcha.com
TWOCAPTCHA_KEY = "YOUR_2CAPTCHA_API_KEY_HERE"

# Cloudflare Turnstile site key for l2reborn.org (don't change this)
TURNSTILE_KEY  = "0x4AAAAAAAPFfPxwacy3GCxf"

# Add one entry per account.
# Tip: use the "+ Add" button in the GUI — it fills server_id and character_id automatically.
ACCOUNTS = [
    {
        "label":        "Account 1",                   # Display name in the GUI
        "email":        "your@email.com",              # L2Reborn login email
        "password":     "your_password",               # L2Reborn login password
        "gmail_app_pw": "xxxx xxxx xxxx xxxx",         # Gmail App Password (for email verification)
                                                       # Guide: https://myaccount.google.com/apppasswords
        "server":       "",                            # Server name (filled by wizard)
        "server_id":    "",                            # Numeric server ID (filled by wizard)
        "game_account": "your_game_account",           # In-game account name
        "character":    "YourCharacterName",           # Character to receive the reward
        "character_id": "",                            # Numeric character ID (filled by wizard)
        "enabled":      True,
    },
    # Add more accounts by copying the block above, or use the GUI "+ Add" button.
]
