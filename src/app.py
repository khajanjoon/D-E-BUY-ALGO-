import websocket
import requests
import json
import time
import os
import hmac
import hashlib
import logging
import threading
from dotenv import load_dotenv

# ================= ENV =================
load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

if not API_KEY or not API_SECRET:
    raise Exception("❌ API keys missing. Check your .env file.")

BASE_URL = "https://api.india.delta.exchange"
WS_URL   = "wss://socket.india.delta.exchange"

# ================= CONFIG =================
SYMBOLS = [
    "GOOGLXUSD", "AMZNXUSD", "TSLAXUSD", "METAXUSD",
    "NVDAXUSD",  "AAPLXUSD", "PAXGUSD",  "SLVONUSD",
    "BTCUSD",    "ETHUSD"
]

LOT_SIZE = {
    "GOOGLXUSD": 30,
    "AMZNXUSD":  50,
    "TSLAXUSD":  25,
    "METAXUSD":  15,
    "NVDAXUSD":  50,
    "AAPLXUSD":  35,
    "PAXGUSD":   20,
    "SLVONUSD":  10,
    "BTCUSD":     1,
    "ETHUSD":     4,
}

DROP_PERCENT           = 3
TP_PERCENT             = 1.5
TRADE_COOLDOWN         = 20       # seconds between trades per symbol
POSITION_SYNC_INTERVAL = 5        # seconds
ORDER_SYNC_INTERVAL    = 8        # seconds
MAX_RETRIES            = 3        # API call retries
REQUEST_TIMEOUT        = 10       # seconds

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ================= STATE =================
prices            = {s: None  for s in SYMBOLS}
positions         = {s: None  for s in SYMBOLS}
product_ids       = {}
open_orders_cache = {s: []    for s in SYMBOLS}
last_trade        = {s: 0     for s in SYMBOLS}
last_trigger_price= {s: None  for s in SYMBOLS}
active_order_flag = {s: False for s in SYMBOLS}
positions_ready   = False

lock = threading.Lock()

# ================= HELPERS =================
def generate_signature(method, path, query="", body=""):
    timestamp = str(int(time.time()))
    message   = method + timestamp + path + query + body
    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature, timestamp


def auth_headers(method, path, query="", body=""):
    sig, ts = generate_signature(method, path, query, body)
    return {
        "api-key":      API_KEY,
        "timestamp":    ts,
        "signature":    sig,
        "Content-Type": "application/json"
    }


def safe_request(method, url, **kwargs):
    """Retries failed HTTP requests up to MAX_RETRIES times."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method, url, timeout=REQUEST_TIMEOUT, **kwargs
            )
            return resp
        except requests.RequestException as e:
            log.warning(f"Request failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)   # exponential back-off
    return None

# ================= LOAD PRODUCTS =================
def load_products():
    log.info("Loading product IDs...")
    for sym in SYMBOLS:
        resp = safe_request("GET", f"{BASE_URL}/v2/products/{sym}")
        if resp and resp.status_code == 200:
            product_ids[sym] = resp.json()["result"]["id"]
        else:
            raise Exception(f"Failed to load product ID for {sym}")
    log.info(f"Products loaded: {product_ids}")

# ================= SYNC POSITIONS =================
def sync_positions():
    global positions_ready
    try:
        headers = auth_headers("GET", "/v2/positions/margined")
        resp    = safe_request("GET", BASE_URL + "/v2/positions/margined", headers=headers)

        if not resp or resp.status_code != 200:
            log.error(f"Position sync failed: {getattr(resp, 'text', 'no response')}")
            return

        data = resp.json().get("result", [])

        with lock:
            updated = set()
            for pos in data:
                for sym in SYMBOLS:
                    if product_ids.get(sym) == pos["product_id"]:
                        if float(pos.get("size", 0)) != 0:
                            positions[sym] = pos
                            updated.add(sym)
            for sym in SYMBOLS:
                if sym not in updated:
                    positions[sym] = None

        positions_ready = True

    except Exception as e:
        log.error(f"Position sync error: {e}")


def position_sync_loop():
    while True:
        sync_positions()
        time.sleep(POSITION_SYNC_INTERVAL)

# ================= ORDER CACHE =================
def fetch_open_orders_loop():
    while True:
        try:
            query   = "?state=open"
            headers = auth_headers("GET", "/v2/orders", query)
            resp    = safe_request("GET", BASE_URL + "/v2/orders" + query, headers=headers)

            if resp and resp.status_code == 200:
                data = resp.json().get("result", [])
                with lock:
                    for sym in SYMBOLS:
                        pid = product_ids.get(sym)
                        open_orders_cache[sym] = [
                            o for o in data
                            if  o["product_id"] == pid
                            and o["side"]        == "sell"
                            and o["reduce_only"] is True
                            and o["state"]       in ("open", "pending")
                        ]
            else:
                log.warning("Open orders fetch returned unexpected response.")

        except Exception as e:
            log.error(f"Order fetch error: {e}")

        time.sleep(ORDER_SYNC_INTERVAL)


def get_lowest_open_sell(sym):
    sell_prices = [
        float(o["limit_price"])
        for o in open_orders_cache.get(sym, [])
        if o.get("limit_price")
    ]
    return min(sell_prices) if sell_prices else None

# ================= PLACE TARGET =================
def place_target_order(sym, entry_price):
    tp_price  = round(entry_price * (1 + TP_PERCENT / 100), 4)
    body_data = {
        "product_id":   product_ids[sym],
        "size":         LOT_SIZE[sym],
        "side":         "sell",
        "order_type":   "limit_order",
        "limit_price":  str(tp_price),
        "time_in_force":"gtc",
        "reduce_only":  True
    }
    body    = json.dumps(body_data, separators=(',', ':'))
    headers = auth_headers("POST", "/v2/orders", "", body)
    resp    = safe_request("POST", BASE_URL + "/v2/orders", headers=headers, data=body)

    if resp and resp.status_code == 200:
        log.info(f"✅ TARGET SET  {sym} | TP: {tp_price}")
    else:
        log.error(f"❌ Target order failed {sym}: {getattr(resp, 'text', 'no response')}")

# ================= PLACE LONG =================
def place_long(sym):
    entry = prices[sym]
    if entry is None:
        return False

    body_data = {
        "product_id":   product_ids[sym],
        "size":         LOT_SIZE[sym],
        "side":         "buy",
        "order_type":   "market_order",
        "time_in_force":"ioc",
        "reduce_only":  False
    }
    body    = json.dumps(body_data, separators=(',', ':'))
    headers = auth_headers("POST", "/v2/orders", "", body)
    resp    = safe_request("POST", BASE_URL + "/v2/orders", headers=headers, data=body)

    if not resp:
        log.error(f"❌ Long order failed {sym}: no response")
        return False

    result = resp.json()
    if resp.status_code == 200 and result.get("success"):
        log.info(f"🟢 LONG  {sym} | Entry: {entry}")
        time.sleep(1)
        place_target_order(sym, entry)
        return True

    log.error(f"❌ Long order rejected {sym}: {resp.text}")
    return False

# ================= TRADE LOGIC =================
def trade_logic(sym):
    # Read shared state under lock, then release before any HTTP call
    with lock:
        if not positions_ready:
            return
        if prices[sym] is None:
            return
        if active_order_flag[sym]:
            return
        if time.time() - last_trade[sym] < TRADE_COOLDOWN:
            return

        pos = positions.get(sym)
        if pos and float(pos.get("size", 0)) == 0:
            positions[sym] = None
            pos = None

        current_price = prices[sym]
        orders        = list(open_orders_cache[sym])   # snapshot
        active_order_flag[sym] = True                  # reserve slot

    # ── All HTTP work happens OUTSIDE the lock ──────────────────────────
    try:
        # FIRST ENTRY
        if not pos:
            if orders:
                return
            log.info(f"📈 Opening FIRST LONG {sym}")
            success = place_long(sym)
            with lock:
                if success:
                    last_trade[sym] = time.time()
            return

        # LADDER ENTRY
        lowest_sell = get_lowest_open_sell(sym)
        if not lowest_sell:
            return

        trigger = lowest_sell * (1 - DROP_PERCENT / 100)

        with lock:
            if last_trigger_price[sym] == trigger:
                return

        if current_price <= trigger:
            log.info(f"📉 {sym} Drop trigger hit → Averaging LONG")
            success = place_long(sym)
            with lock:
                if success:
                    last_trade[sym]         = time.time()
                    last_trigger_price[sym] = trigger

    finally:
        with lock:
            active_order_flag[sym] = False

# ================= DASHBOARD =================
def dashboard_loop():
    while True:
        time.sleep(5)

        print("\n" * 2)
        print("=" * 80)
        print("🔥 DELTA LONG ENGINE - LIVE DASHBOARD 🔥")
        print("=" * 80)

        total_unrealized = 0
        total_exposure = 0

        with lock:
            for sym in SYMBOLS:

                price = prices.get(sym)
                pos = positions.get(sym)

                print("\n------------------------------------------------------------")
                print(f"SYMBOL: {sym}")
                print("------------------------------------------------------------")
                print(f"Mark Price        : {price}")

                if not pos or not price:
                    print("Position          : None")
                    continue

                size = float(pos["size"])
                entry = float(pos["entry_price"])

                unrealized = (price - entry) * abs(size)
                exposure = abs(size) * price

                lowest_sell = get_lowest_open_sell(sym)
                trigger = None

                if lowest_sell:
                    trigger = round(lowest_sell * (1 - DROP_PERCENT / 100), 4)

                tp_price = round(entry * (1 + TP_PERCENT / 100), 4)

                print(f"Direction         : LONG")
                print(f"Position Size     : {size}")
                print(f"Entry Price       : {entry}")
                print(f"Take Profit       : {tp_price}")
                print(f"Unrealized PnL    : {round(unrealized,4)}")
                print(f"Current Exposure  : {round(exposure,4)}")
                print(f"Lowest SELL TP    : {lowest_sell}")
                print(f"Next Trigger      : {trigger}")

                total_unrealized += unrealized
                total_exposure += exposure

        print("\n============================================================")
        print("PORTFOLIO SUMMARY")
        print("============================================================")
        print(f"Total Unrealized PnL : {round(total_unrealized,4)}")
        print(f"Total Exposure       : {round(total_exposure,4)}")
        print("=" * 80)

# ================= WEBSOCKET =================
def on_open(ws):
    sig, ts = generate_signature("GET", "/live")
    ws.send(json.dumps({
        "type": "auth",
        "payload": {"api-key": API_KEY, "signature": sig, "timestamp": ts}
    }))
    time.sleep(1)
    ws.send(json.dumps({
        "type": "subscribe",
        "payload": {"channels": [{"name": "v2/ticker", "symbols": SYMBOLS}]}
    }))
    log.info("✅ WebSocket connected & subscribed")


def on_message(ws, message):
    try:
        data = json.loads(message)
        sym  = data.get("symbol")
        if sym in SYMBOLS and "mark_price" in data:
            prices[sym] = float(data["mark_price"])
            threading.Thread(target=trade_logic, args=(sym,), daemon=True).start()
    except Exception as e:
        log.error(f"on_message error: {e}")


def on_error(ws, error):
    log.error(f"WebSocket error: {error}")


def on_close(ws, code, msg):
    log.warning(f"WebSocket closed ({code}): {msg}")


def start_ws():
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            log.error(f"WebSocket crashed: {e}")
        log.info("Reconnecting in 5s...")
        time.sleep(5)

# ================= MAIN =================
if __name__ == "__main__":
    load_products()
    sync_positions()

    threading.Thread(target=position_sync_loop,   daemon=True).start()
    threading.Thread(target=fetch_open_orders_loop, daemon=True).start()
    threading.Thread(target=dashboard_loop,        daemon=True).start()

    start_ws()
