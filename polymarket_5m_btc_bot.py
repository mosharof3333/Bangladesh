import asyncio
import os
import time
import json
from decimal import Decimal
from dotenv import load_dotenv
import requests

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import OrderArgs

load_dotenv()

# ================== YOUR EXACT RULES ==================
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

ENTRY_THRESHOLD = Decimal("0.65")
BUY_LIMIT_PRICE = Decimal("0.60")
TP_PRICE = Decimal("0.98")
SL_PRICE = Decimal("0.45")
BASE_USD = Decimal("3.00")                    # ← Your base amount

# ================== STATE ==================
total_pnl = 0.0
last_trade_pnl = 0.0
wins = 0
losses = 0
current_round = 1
consecutive_losses = 0
cumulative_loss = 0.0                         # Used for recovery martingale

active_entry_order_id = None
tp_order_id = None
sl_order_id = None
current_window_end = None
position_side = None
position_token_id = None
current_shares = 0
client = None


def get_next_bet_info():
    """Recovery martingale: next bet recovers all previous losses + base $3"""
    global consecutive_losses, cumulative_loss

    if consecutive_losses == 0:
        bet_usd = BASE_USD
    else:
        profit_per_dollar = TP_PRICE - BUY_LIMIT_PRICE  # 0.38
        recovery = cumulative_loss / profit_per_dollar
        bet_usd = BASE_USD + recovery

    # Max 5 increases (6 rounds total)
    if consecutive_losses >= 6:
        print("🔄 Max increases reached → Hard reset to base $3")
        bet_usd = BASE_USD
        consecutive_losses = 0
        cumulative_loss = 0.0

    shares = int(round(float(bet_usd) / float(BUY_LIMIT_PRICE)))
    return shares, float(bet_usd)


def print_dashboard(event: str):
    shares, usd = get_next_bet_info()
    win_rate = round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0.0
    print("\n" + "="*90)
    print(f"🚀 POLYMARKET 5M BTC BOT - {event.upper()}")
    print(f"Time: {time.strftime('%H:%M:%S')} | Demo: {DEMO_MODE} | Round: {current_round}")
    print(f"Next Bet: ${usd:.2f} ({shares} shares) | Losses in row: {consecutive_losses}/5")
    print(f"Last: {'+' if last_trade_pnl >= 0 else ''}{last_trade_pnl:.2f} USD | Total P&L: {'+' if total_pnl >= 0 else ''}{total_pnl:.2f} USD")
    print(f"Wins: {wins} | Losses: {losses} | Win Rate: {win_rate}%")
    print("="*90 + "\n")


async def init_client():
    global client
    if DEMO_MODE:
        print("🧪 DEMO MODE ACTIVE")
        return
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=POLYGON,
        signature_type=2,
        funder=WALLET_ADDRESS
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    print("✅ LIVE client connected")


# ================== STRICT LIVE MARKET (unchanged) ==================
def get_current_btc_5m_markets():
    now = int(time.time())
    interval = 300
    current_ts = (now // interval) * interval

    slug = f"btc-updown-5m-{current_ts}"
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
                            try: clob = json.loads(clob)
                            except: clob = None
                        if isinstance(clob, list) and len(clob) >= 2 and now < current_ts + 300:
                            print(f"✅ LIVE MARKET FOUND: {slug}")
                            return {
                                "up_token_id": str(clob[0]),
                                "down_token_id": str(clob[1]),
                                "window_end": current_ts + 300,
                                "slug": slug
                            }
    except:
        pass

    print("⚠️ Waiting for next LIVE 5m window...")
    return None


def get_best_ask_sync(token_id: str) -> Decimal:
    if DEMO_MODE:
        import random
        return Decimal(str(round(0.48 + random.random() * 0.28, 4)))
    try:
        orderbook = client.get_order_book(token_id)
        asks = getattr(orderbook, "asks", None) or (orderbook.get("asks", []) if isinstance(orderbook, dict) else [])
        if asks and len(asks) > 0:
            price = asks[0].price if hasattr(asks[0], "price") else asks[0].get("price")
            return Decimal(str(price))
    except:
        pass
    return Decimal("0.50")


async def get_best_ask(token_id: str) -> Decimal:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_best_ask_sync, token_id)


# ================== TRADE EXECUTION LOGIC (YOUR PROMPT) ==================
def place_order_sync(token_id: str, shares: int, side: str, price: float) -> str:
    order_args = OrderArgs(token_id=token_id, price=price, size=float(shares), side=side)
    response = client.create_and_post_order(order_args)
    return response.get("orderID") or response.get("id") or str(response)


async def place_limit_buy(token_id: str, side: str):
    global active_entry_order_id, position_side, position_token_id, current_shares
    shares, usd = get_next_bet_info()
    current_shares = shares

    print(f"🔥 {side} HIT {ENTRY_THRESHOLD} → LIMIT BUY {shares} shares @ \( {BUY_LIMIT_PRICE} ( \){usd:.2f})")

    if DEMO_MODE:
        active_entry_order_id = f"demo-entry-{int(time.time())}"
        position_side = side
        position_token_id = token_id
        print("🧪 DEMO: Entry placed")
        await place_tp_and_sl()          # Immediately place TP + SL
        return

    try:
        loop = asyncio.get_event_loop()
        order_id = await loop.run_in_executor(None, place_order_sync, token_id, shares, "BUY", float(BUY_LIMIT_PRICE))
        active_entry_order_id = order_id
        position_side = side
        position_token_id = token_id
        print(f"✅ ENTRY LIMIT BUY PLACED: {order_id}")
        await place_tp_and_sl()          # Immediately place TP + SL (as per your prompt)
    except Exception as e:
        print(f"❌ Buy failed: {e}")


async def place_tp_and_sl():
    """Immediately place both TP and SL limit sells after entry"""
    global tp_order_id, sl_order_id
    if not position_token_id or current_shares == 0:
        return

    print(f"📤 Placing LIMIT TP @ ${TP_PRICE} and LIMIT SL @ ${SL_PRICE}...")

    if DEMO_MODE:
        tp_order_id = f"demo-tp-{int(time.time())}"
        sl_order_id = f"demo-sl-{int(time.time())}"
        print("🧪 DEMO: TP & SL limit sells simulated")
        return

    try:
        loop = asyncio.get_event_loop()
        tp_id = await loop.run_in_executor(None, place_order_sync, position_token_id, current_shares, "SELL", float(TP_PRICE))
        sl_id = await loop.run_in_executor(None, place_order_sync, position_token_id, current_shares, "SELL", float(SL_PRICE))
        tp_order_id = tp_id
        sl_order_id = sl_id
        print(f"✅ TP LIMIT SELL: {tp_id} | SL LIMIT SELL: {sl_id}")
    except Exception as e:
        print(f"❌ TP/SL placement failed: {e}")


async def cancel_entry_if_not_filled():
    """If entry not filled by window end → cancel it"""
    global active_entry_order_id
    if active_entry_order_id and not tp_order_id:   # No TP/SL = entry never filled
        print("🕒 Window ended → Entry not filled → Cancelling...")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: client.cancel(active_entry_order_id))
            print("✅ Entry cancelled")
        except:
            pass
        active_entry_order_id = None


async def close_position(reason: str):
    global active_entry_order_id, tp_order_id, sl_order_id, total_pnl, last_trade_pnl, wins, losses, consecutive_losses, cumulative_loss, current_round, current_shares

    shares = current_shares
    if reason == "TP":
        pnl = round(float(shares) * (float(TP_PRICE) - float(BUY_LIMIT_PRICE)), 2)
        last_trade_pnl = pnl
        total_pnl += pnl
        wins += 1
        consecutive_losses = 0
        cumulative_loss = 0.0
        current_round = 1
        print(f"🎉 TAKE PROFIT +${pnl:.2f}")
    else:  # SL
        pnl = round(float(shares) * (float(SL_PRICE) - float(BUY_LIMIT_PRICE)), 2)
        last_trade_pnl = pnl
        total_pnl += pnl
        losses += 1
        consecutive_losses += 1
        cumulative_loss += abs(pnl)
        if consecutive_losses >= 6:
            print("🔄 Max losses → Reset to base $3")
            consecutive_losses = 0
            cumulative_loss = 0.0
            current_round = 1
        else:
            current_round += 1
            print(f"❌ STOP LOSS -${abs(pnl):.2f} → Round {current_round} (recovery size)")

    print_dashboard(f"{reason} COMPLETE")
    active_entry_order_id = tp_order_id = sl_order_id = None
    current_shares = 0


# ================== MONITORING ==================
async def monitor_prices():
    global active_entry_order_id, current_window_end
    print_dashboard("BOT STARTED")

    last_print = 0
    while True:
        markets = get_current_btc_5m_markets()
        if markets:
            if markets.get("window_end") != current_window_end:
                print(f"🕒 NEW LIVE 5-MIN WINDOW → {markets['slug']}")
                await cancel_entry_if_not_filled()   # Cancel any unfilled entry from previous window
                active_entry_order_id = None
                current_window_end = markets.get("window_end")
                print_dashboard("NEW WINDOW")

            if time.time() - last_print > 2:
                up = await get_best_ask(markets["up_token_id"])
                down = await get_best_ask(markets["down_token_id"])
                print(f"[{time.strftime('%H:%M:%S')}] LIVE BUY UP: {up:.4f}  |  LIVE BUY DOWN: {down:.4f}")
                last_print = time.time()

            # ENTRY LOGIC
            if active_entry_order_id is None and position_token_id is None:
                up_price = await get_best_ask(markets["up_token_id"])
                down_price = await get_best_ask(markets["down_token_id"])
                if up_price >= ENTRY_THRESHOLD:
                    await place_limit_buy(markets["up_token_id"], "UP")
                elif down_price >= ENTRY_THRESHOLD:
                    await place_limit_buy(markets["down_token_id"], "DOWN")

        await asyncio.sleep(0.2)


async def monitor_position():
    while True:
        if active_entry_order_id and DEMO_MODE:
            import random
            if random.random() < 0.18:
                await close_position("TP" if random.random() < 0.65 else "SL")
        await asyncio.sleep(0.25)


async def main():
    await init_client()
    await asyncio.gather(monitor_prices(), monitor_position())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped")
    except Exception as e:
        print(f"💥 Error: {e}")
