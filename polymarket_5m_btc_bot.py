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

# ================== YOUR RULES ==================
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

ENTRY_THRESHOLD = Decimal("0.65")      # Trigger when ASK hits this
BUY_LIMIT_PRICE = Decimal("0.60")
TP_PRICE = Decimal("0.98")
SL_PRICE = Decimal("0.45")
BASE_USD = Decimal("3.00")

# ================== STATE ==================
total_pnl = 0.0
last_trade_pnl = 0.0
wins = 0
losses = 0
current_round = 1
consecutive_losses = 0
cumulative_loss = 0.0

active_entry_order_id = None
tp_order_id = None
sl_order_id = None
current_window_end = None
position_side = None
position_token_id = None
current_shares = 0

client = None


def get_next_bet_info():
    global consecutive_losses, cumulative_loss
    if consecutive_losses == 0:
        bet_usd = BASE_USD
    else:
        profit_per_dollar = float(TP_PRICE - BUY_LIMIT_PRICE)
        recovery = cumulative_loss / profit_per_dollar
        bet_usd = BASE_USD + Decimal(recovery)

    if consecutive_losses >= 6:
        bet_usd = BASE_USD
        consecutive_losses = 0
        cumulative_loss = 0.0

    shares = int(round(float(bet_usd) / float(BUY_LIMIT_PRICE)))
    return shares, float(bet_usd)


def print_dashboard(event: str):
    shares, usd = get_next_bet_info()
    win_rate = round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0.0
    print("\n" + "="*105)
    print(f"🚀 POLYMARKET 5M BTC BOT - {event.upper()}")
    print(f"Time: {time.strftime('%H:%M:%S')} | Demo: {DEMO_MODE} | Round: {current_round}")
    print(f"Next Bet: ${usd:.2f} ({shares} shares) | Consec Losses: {consecutive_losses}/5")
    print(f"Last: {'+' if last_trade_pnl >= 0 else ''}{last_trade_pnl:.2f} | Total P&L: {'+' if total_pnl >= 0 else ''}{total_pnl:.2f}")
    print(f"Wins: {wins} | Losses: {losses} | Win Rate: {win_rate}%")
    print("="*105 + "\n")


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
                            print(f"✅ LIVE MARKET: {slug}")
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


def get_orderbook_info_sync(token_id: str):
    if DEMO_MODE:
        import random
        ask = round(0.45 + random.random() * 0.50, 4)
        mid = round(0.40 + random.random() * 0.55, 4)
        return Decimal(str(ask)), Decimal(str(mid))

    try:
        orderbook = client.get_order_book(token_id)
        asks = getattr(orderbook, "asks", None) or (orderbook.get("asks", []) if isinstance(orderbook, dict) else [])
        bids = getattr(orderbook, "bids", None) or (orderbook.get("bids", []) if isinstance(orderbook, dict) else [])

        best_ask = Decimal("0.50")
        if asks and len(asks) > 0:
            first = asks[0]
            best_ask = Decimal(str(first.price if hasattr(first, "price") else first.get("price")))

        best_bid = Decimal("0.50")
        if bids and len(bids) > 0:
            first = bids[0]
            best_bid = Decimal(str(first.price if hasattr(first, "price") else first.get("price")))

        mid = (best_bid + best_ask) / 2
        return best_ask, mid
    except Exception as e:
        print(f"⚠️ Orderbook error: {e}")
    return Decimal("0.50"), Decimal("0.50")


async def get_orderbook_info(token_id: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_orderbook_info_sync, token_id)


def place_order_sync(token_id: str, shares: int, side: str, price: float) -> str:
    order_args = OrderArgs(token_id=token_id, price=price, size=float(shares), side=side)
    response = client.create_and_post_order(order_args)
    return response.get("orderID") or response.get("id") or str(response)


async def place_limit_buy(token_id: str, side: str):
    global active_entry_order_id, position_side, position_token_id, current_shares
    shares, usd = get_next_bet_info()
    current_shares = shares

    print(f"🔥 {side} ASK HIT {ENTRY_THRESHOLD} → LIMIT BUY {shares} shares @ \( {BUY_LIMIT_PRICE} ( \){usd:.2f})")

    if DEMO_MODE:
        active_entry_order_id = f"demo-entry-{int(time.time())}"
        position_side = side
        position_token_id = token_id
        print("🧪 DEMO: Entry limit buy placed")
        await place_tp_and_sl()
        return

    try:
        loop = asyncio.get_event_loop()
        order_id = await loop.run_in_executor(None, place_order_sync, token_id, shares, "BUY", float(BUY_LIMIT_PRICE))
        active_entry_order_id = order_id
        position_side = side
        position_token_id = token_id
        print(f"✅ ENTRY LIMIT BUY PLACED: {order_id}")
        await place_tp_and_sl()
    except Exception as e:
        print(f"❌ Buy failed: {e}")


async def place_tp_and_sl():
    global tp_order_id, sl_order_id
    if not position_token_id:
        return
    print(f"📤 Placing TP @ ${TP_PRICE} and SL @ ${SL_PRICE}...")
    if DEMO_MODE:
        tp_order_id = "demo-tp"
        sl_order_id = "demo-sl"
        print("🧪 DEMO: TP & SL placed")
        return
    try:
        loop = asyncio.get_event_loop()
        tp_id = await loop.run_in_executor(None, place_order_sync, position_token_id, current_shares, "SELL", float(TP_PRICE))
        sl_id = await loop.run_in_executor(None, place_order_sync, position_token_id, current_shares, "SELL", float(SL_PRICE))
        tp_order_id = tp_id
        sl_order_id = sl_id
        print(f"✅ TP: {tp_id} | SL: {sl_id}")
    except Exception as e:
        print(f"❌ TP/SL failed: {e}")


async def monitor_prices():
    global active_entry_order_id, current_window_end
    print_dashboard("BOT STARTED")

    last_print = 0
    while True:
        markets = get_current_btc_5m_markets()
        if markets:
            if markets.get("window_end") != current_window_end:
                print(f"🕒 NEW LIVE 5-MIN WINDOW → {markets['slug']}")
                active_entry_order_id = None
                current_window_end = markets.get("window_end")
                print_dashboard("NEW WINDOW")

            if time.time() - last_print > 2:
                up_ask, up_mid = await get_orderbook_info(markets["up_token_id"])
                down_ask, down_mid = await get_orderbook_info(markets["down_token_id"])

                print(f"[{time.strftime('%H:%M:%S')}] UP Mid: {up_mid:.4f} (Ask {up_ask:.4f})  |  DOWN Mid: {down_mid:.4f} (Ask {down_ask:.4f})")

                # Trigger on ASK price (what you actually pay when buying) - matches your manual monitoring
                if up_ask >= ENTRY_THRESHOLD:
                    print(f"🔥 UP ASK ≥ {ENTRY_THRESHOLD} → ENTRY SIGNAL")
                    await place_limit_buy(markets["up_token_id"], "UP")
                elif down_ask >= ENTRY_THRESHOLD:
                    print(f"🔥 DOWN ASK ≥ {ENTRY_THRESHOLD} → ENTRY SIGNAL")
                    await place_limit_buy(markets["down_token_id"], "DOWN")

                last_print = time.time()

        await asyncio.sleep(0.2)


async def monitor_position():
    while True:
        if active_entry_order_id and DEMO_MODE:
            import random
            if random.random() < 0.18:
                await close_position("TP" if random.random() < 0.65 else "SL")
        await asyncio.sleep(0.25)


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
    else:
        pnl = round(float(shares) * (float(SL_PRICE) - float(BUY_LIMIT_PRICE)), 2)
        last_trade_pnl = pnl
        total_pnl += pnl
        losses += 1
        consecutive_losses += 1
        cumulative_loss += abs(pnl)
        if consecutive_losses >= 6:
            print("🔄 Max increases reached → Reset to base $3")
            consecutive_losses = 0
            cumulative_loss = 0.0
            current_round = 1
        else:
            current_round += 1
            print(f"❌ STOP LOSS -${abs(pnl):.2f} → Recovery Round {current_round}")

    print_dashboard(f"{reason} COMPLETE")
    active_entry_order_id = tp_order_id = sl_order_id = None
    current_shares = 0


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
