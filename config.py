# ============================================================
#  config.py — Fill in your own credentials before running
# ============================================================

# Get your 2Captcha API key at: https://2captcha.com
# (Only needed when a CAPTCHA appears during login)
TWOCAPTCHA_KEY = "YOUR_2CAPTCHA_API_KEY_HERE"

# Cloudflare Turnstile site key for l2reborn.org (don't change this)
TURNSTILE_KEY  = "0x4AAAAAAAPFfPxwacy3GCxf"

# Add one entry per account. Set enabled=False to skip an account.
ACCOUNTS = [
    {
        "label": "Account 1",               # Display name in the GUI
        "email": "your@email.com",           # L2Reborn login email
        "password": "your_password",         # L2Reborn login password
        "gmail_app_pw": "xxxx xxxx xxxx xxxx",  # Gmail App Password (for email verification)
                                             # Guide: https://myaccount.google.com/apppasswords
        "game_account": "your_game_account", # In-game account name
        "character": "YourCharacterName",    # Character to receive the reward
        "enabled": True,
    },
    # Add more accounts below by copying the block above:
    # {
    #     "label": "Account 2",
    #     "email": "another@email.com",
    #     "password": "another_password",
    #     "gmail_app_pw": "yyyy yyyy yyyy yyyy",
    #     "game_account": "another_game_account",
    #     "character": "AnotherCharacter",
    #     "enabled": True,
    # },
]
