import asyncio
import os
import time
import json
from decimal import Decimal
from dotenv import load_dotenv
import requests

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.signer import Signer
from py_clob_client.clob_types import OrderArgs

load_dotenv()

# ================== YOUR SETTINGS ==================
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"   # Change to false when ready for real money

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

ENTRY_THRESHOLD = Decimal("0.65")
BUY_LIMIT_PRICE = Decimal("0.60")
MARTINGALE_SIZES = [5, 7, 10, 14, 20, 28]

# P&L tracking
total_pnl = 0.0
last_trade_pnl = 0.0
wins = 0
losses = 0
current_round = 1
consecutive_losses = 0

# Position tracking
active_order_id = None
current_window_end = None
position_side = None
position_token_id = None
current_shares = 0

client = None


def get_next_bet_info():
    idx = min(current_round - 1, len(MARTINGALE_SIZES) - 1)
    shares = MARTINGALE_SIZES[idx]
    usd = round(shares * float(BUY_LIMIT_PRICE), 2)
    return shares, usd


def print_dashboard(event):
    shares, usd = get_next_bet_info()
    win_rate = round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0.0
    print("\n" + "="*80)
    print(f"🚀 POLYMARKET 5M BTC BOT - {event.upper()}")
    print(f"Time: {time.strftime('%H:%M:%S')} | Demo: {DEMO_MODE} | Round: {current_round}")
    print(f"Next Bet: ${usd} ({shares} shares) | Losses in a row: {consecutive_losses}/6")
    print(f"Last trade: {'+' if last_trade_pnl >= 0 else ''}{last_trade_pnl:.2f} USD")
    print(f"Total P&L:   {'+' if total_pnl >= 0 else ''}{total_pnl:.2f} USD")
    print(f"Wins: {wins} | Losses: {losses} | Win Rate: {win_rate}%")
    print("="*80 + "\n")


async def init_client():
    global client
    if DEMO_MODE:
        print("🧪 DEMO MODE — No real money used")
        return
    signer = Signer(PRIVATE_KEY, POLYGON)
    client = ClobClient("https://clob.polymarket.com", chain_id=POLYGON, signer=signer, wallet_address=WALLET_ADDRESS)
    client.set_api_creds(client.create_or_derive_api_creds())
    print("✅ Connected to Polymarket (LIVE)")


# === YOUR ORIGINAL MARKET DISCOVERY (unchanged) ===
def get_current_btc_5m_markets():
    now = int(time.time())
    interval = 300
    current_ts = (now // interval) * interval

    print(f"[{time.strftime('%H:%M:%S')}] Searching for live BTC 5m market...")

    for offset in [0]:
        ts = current_ts + offset
        slug = f"btc-updown-5m-{ts}"
        try:
            resp = requests.get("https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                events = data if isinstance(data, list) else [data] if data else []
                for event in events:
                    if event.get("slug") == slug:
                        for m in event.get("markets", []):
                            clob = m.get("clobTokenIds")
                            if isinstance(clob, str):
                                try:
                                    clob = json.loads(clob)
                                except:
                                    clob = None
                            if isinstance(clob, list) and len(clob) >= 2:
                                print(f"✅ FOUND LIVE MARKET: {slug}")
                                return {
                                    "up_token_id": str(clob[0]),
                                    "down_token_id": str(clob[1]),
                                    "window_end": ts + 300,
                                    "slug": slug
                                }
        except:
            pass

    # Fallback - scan active events
    try:
        resp = requests.get("https://gamma-api.polymarket.com/events",
                           params={"active": "true", "closed": "false", "limit": 50}, timeout=8)
        if resp.status_code == 200:
            events = resp.json()
            for event in events if isinstance(events, list) else []:
                if "btc-updown-5m" in event.get("slug", ""):
                    for m in event.get("markets", []):
                        clob = m.get("clobTokenIds")
                        if isinstance(clob, str):
                            try:
                                clob = json.loads(clob)
                            except:
                                continue
                        if isinstance(clob, list) and len(clob) >= 2:
                            print(f"✅ FOUND via active scan: {event.get('slug')}")
                            return {
                                "up_token_id": str(clob[0]),
                                "down_token_id": str(clob[1]),
                                "window_end": int(time.time()) + 300,
                                "slug": event.get("slug")
                            }
    except:
        pass

    print("⚠️ No live 5m market yet...")
    return None


# === YOUR ORIGINAL TRADE PLACEMENT LOGIC (unchanged) ===
def place_order_sync(token_id: str, shares: int) -> str:
    order_args = OrderArgs(
        token_id=token_id,
        price=float(BUY_LIMIT_PRICE),
        size=float(shares),
        side="BUY",
    )
    response = client.create_and_post_order(order_args)
    return response.get("orderID") or response.get("id") or str(response)


async def place_limit_buy(token_id: str, side: str):
    global active_order_id, position_side, position_token_id, current_shares
    shares, usd = get_next_bet_info()
    current_shares = shares

    print(f"🔥 {side} SIGNAL HIT → Buying {shares} shares @ ${BUY_LIMIT_PRICE}")

    if DEMO_MODE:
        active_order_id = f"demo-{int(time.time())}"
        position_side = side
        position_token_id = token_id
        print("🧪 DEMO: Order placed (fake)")
        return

    try:
        loop = asyncio.get_event_loop()
        order_id = await loop.run_in_executor(None, place_order_sync, token_id, shares)
        active_order_id = order_id
        position_side = side
        position_token_id = token_id
        print(f"✅ REAL ORDER PLACED: {order_id}")
    except Exception as e:
        print(f"❌ Order failed: {e}")


async def close_position(reason: str):
    global active_order_id, total_pnl, last_trade_pnl, wins, losses, consecutive_losses, current_round, current_shares
    shares = current_shares

    if reason == "TP":
        pnl = round(shares * 0.38, 2)
        last_trade_pnl = pnl
        total_pnl += pnl
        wins += 1
        consecutive_losses = 0
        current_round = 1
        print(f"🎉 TAKE PROFIT +${pnl:.2f}")
    else:
        pnl = round(shares * -0.15, 2)
        last_trade_pnl = pnl
        total_pnl += pnl
        losses += 1
        consecutive_losses += 1
        if consecutive_losses >= 6:
            consecutive_losses = 0
            current_round = 1
            print("🔄 6 losses → Reset to Round 1")
        else:
            current_round += 1
            print(f"❌ STOP LOSS -${abs(pnl):.2f} → Next Round {current_round}")

    print_dashboard(f"{reason} COMPLETE")
    active_order_id = None


# === FAST MONITORING (0.2s loop) ===
async def monitor_prices():
    global active_order_id, position_side, current_window_end
    print_dashboard("BOT STARTED")

    last_print = 0
    while True:
        markets = get_current_btc_5m_markets()

        if markets:
            if markets.get("window_end") != current_window_end:
                print(f"🕒 NEW 5-MINUTE WINDOW → {markets['slug']}")
                active_order_id = None
                position_side = None
                current_window_end = markets.get("window_end")
                print_dashboard("NEW WINDOW")

            if time.time() - last_print > 2:
                up = await get_best_ask(markets["up_token_id"])
                down = await get_best_ask(markets["down_token_id"])
                print(f"[{time.strftime('%H:%M:%S')}] UP: {up:.4f} | DOWN: {down:.4f}")
                if up >= ENTRY_THRESHOLD or down >= ENTRY_THRESHOLD:
                    print("🔥 ENTRY SIGNAL DETECTED!")
                last_print = time.time()

            # PLACE TRADE (your exact logic)
            if active_order_id is None and position_side is None:
                up_price = await get_best_ask(markets["up_token_id"])
                down_price = await get_best_ask(markets["down_token_id"])
                if up_price >= ENTRY_THRESHOLD:
                    await place_limit_buy(markets["up_token_id"], "UP")
                elif down_price >= ENTRY_THRESHOLD:
                    await place_limit_buy(markets["down_token_id"], "DOWN")

        await asyncio.sleep(0.2)


def get_best_ask_sync(token_id):
    if DEMO_MODE:
        import random
        return Decimal(str(round(0.48 + random.random() * 0.28, 4)))
    try:
        orderbook = client.get_order_book(token_id)
        asks = getattr(orderbook, "asks", [])
        if asks:
            return Decimal(str(asks[0].price))
    except:
        pass
    return Decimal("0.50")


async def get_best_ask(token_id):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_best_ask_sync, token_id)


async def monitor_position():
    while True:
        if active_order_id and DEMO_MODE:
            import random
            if random.random() < 0.20:
                await close_position("TP" if random.random() < 0.65 else "SL")
        await asyncio.sleep(0.25)


async def main():
    await init_client()
    await asyncio.gather(monitor_prices(), monitor_position())


if __name__ == "__main__":
    asyncio.run(main())
