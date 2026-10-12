"""
Auto-verification helper for the License Key Bot. Deploy on Railway.

Only ONE setting is needed on Railway:  VERIFIER_SECRET
Everything else (Telegram API ID/hash, phone, affiliate bot) is stored in the
app. On first start it asks Telegram for a login code; type that code in the
admin page and the helper logs in and remembers it.
"""

import os
import re
import sys
import time
import asyncio
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

APP_BASE = os.getenv(
    "APP_BASE", "https://project--b593e4b1-0b15-4dc1-9ff2-1d778b91b027-dev.lovable.app"
)
SECRET = os.getenv("VERIFIER_SECRET")
if not SECRET:
    sys.exit("Missing VERIFIER_SECRET variable")
H = {"X-Verifier-Secret": SECRET}

# Exact affiliate-bot replies (confirmed by the bot owner):
#   verified:  "Trader # 60539010\n\nCountry: ...\nAffiliate program: ..."
#   not found: "Trader with ID = '5796504' was not found."
VERIFIED = ["verified", "confirmed", "active", "registered", "valid"]
REJECTED = ["not found", "not registered", "invalid", "unknown", "does not exist", "no such", "not verified"]


def state():
    r = requests.get(f"{APP_BASE}/api/public/telegram/bridge", headers=H, timeout=30)
    r.raise_for_status()
    return r.json()


def report(**body):
    try:
        requests.post(f"{APP_BASE}/api/public/telegram/bridge", headers=H, json=body, timeout=30)
    except Exception as e:
        print("report failed:", e, flush=True)


def classify(text, trader_id):
    t = text.lower()
    # Exact match on the known affiliate-bot formats first.
    if re.search(rf"trader\s*#\s*{re.escape(str(trader_id))}\b", t):
        return True
    if re.search(r"trader with id\s*=\s*'\d+'\s*was not found", t):
        return False
    # Fallback keyword guess for any other wording.
    if any(k in t for k in REJECTED):
        return False
    return any(k in t for k in VERIFIED)


async def login(cfg):
    client = TelegramClient(StringSession(), int(cfg["api_id"]), cfg["api_hash"])
    await client.connect()
    sent = await client.send_code_request(cfg["phone"])
    report(status="waiting_code", clear_code=True)
    print("Login code sent to Telegram. Enter it on the admin page.", flush=True)
    while True:
        await asyncio.sleep(3)
        code = state().get("login_code")
        report(status="waiting_code")
        if not code:
            continue
        try:
            await client.sign_in(cfg["phone"], code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            await client.sign_in(password=cfg.get("password") or "")
        except Exception as e:
            print("Login failed:", e, flush=True)
            report(status="login_failed", clear_code=True)
            await asyncio.sleep(5)
            sent = await client.send_code_request(cfg["phone"])
            report(status="waiting_code")
            continue
        report(status="running", session_string=client.session.save(), clear_code=True)
        return client


async def main():
    cfg = state()
    while not (cfg.get("api_id") and cfg.get("api_hash") and cfg.get("phone") and cfg.get("affiliate_bot")):
        report(status="config_missing")
        print("Telegram details missing in the app; retrying in 30s", flush=True)
        await asyncio.sleep(30)
        cfg = state()

    if cfg.get("session_string"):
        client = TelegramClient(StringSession(cfg["session_string"]), int(cfg["api_id"]), cfg["api_hash"])
        await client.connect()
        if not await client.is_user_authorized():
            report(session_string=None)
            client = await login(cfg)
    else:
        client = await login(cfg)

    bot = cfg["affiliate_bot"]
    report(status="running")
    print("Running. Verifying trader IDs automatically.", flush=True)

    while True:
        try:
            s = state()
            if not s.get("session_string"):
                print("Logged out from admin page; exiting so Railway restarts.", flush=True)
                await client.log_out()
                sys.exit(1)
            report(status="running")
            r = requests.get(f"{APP_BASE}/api/public/telegram/pending-requests", headers=H, timeout=30)
            r.raise_for_status()
            for req in r.json().get("requests", []):
                tid = req["trader_id"]
                print("Checking", tid, flush=True)
                last = await client.get_messages(bot, limit=1)
                last_id = last[0].id if last else 0
                await client.send_message(bot, tid)
                reply = ""
                deadline = time.time() + 60
                while time.time() < deadline:
                    msgs = await client.get_messages(bot, limit=5, min_id=last_id)
                    got = [m for m in msgs if not m.out and m.message]
                    if got:
                        await asyncio.sleep(2)  # let multi-part replies arrive
                        msgs = await client.get_messages(bot, limit=5, min_id=last_id)
                        reply = "\n".join(m.message for m in reversed(msgs) if not m.out and m.message)
                        break
                    await asyncio.sleep(2)
                if not reply:
                    print("  no reply, retry later", flush=True)
                    continue
                ok = classify(reply, tid)
                requests.post(
                    f"{APP_BASE}/api/public/telegram/verify-result",
                    headers=H,
                    json={"request_id": req["id"], "verified": ok, "affiliate_reply": reply[:2000]},
                    timeout=30,
                ).raise_for_status()
                print("  ->", "VERIFIED" if ok else "NOT VERIFIED", flush=True)
        except SystemExit:
            raise
        except Exception as e:
            print("Error:", e, flush=True)
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
