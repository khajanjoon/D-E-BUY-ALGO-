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
    raise Exception("API keys missing")

BASE_URL = "https://api.india.delta.exchange"
WS_URL = "wss://socket.india.delta.exchange"

# ================= CONFIG =================
SYMBOLS = ["GOOGLXUSD", "AMZNXUSD", "TSLAXUSD", "METAXUSD",
           "NVDAXUSD", "AAPLXUSD","PAXGUSD","SLVONUSD"]

LOT_SIZE = {
    "GOOGLXUSD": 30,
    "AMZNXUSD": 50,
    "TSLAXUSD": 25,
    "METAXUSD": 15,
    "NVDAXUSD": 50,
    "AAPLXUSD": 35,
    "PAXGUSD": 20,
    "SLVONUSD": 10
}

DROP_PERCENT = 3
TP_PERCENT = 1.5
TRADE_COOLDOWN = 20
POSITION_SYNC_INTERVAL = 5
ORDER_SYNC_INTERVAL = 8

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ================= STATE =================
prices = {}
positions = {}
product_ids = {}
open_orders_cache = {}
last_trade = {}
last_trigger_price = {}
active_order_flag = {}
positions_ready = False

lock = threading.Lock()

for s in SYMBOLS:
    prices[s] = None
    positions[s] = None
    open_orders_cache[s] = []
    last_trade[s] = 0
    last_trigger_price[s] = None
    active_order_flag[s] = False

# ================= SIGNATURE =================
def generate_signature(method, path, query="", body=""):
    timestamp = str(int(time.time()))
    message = method + timestamp + path + query + body
    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature, timestamp

# ================= LOAD PRODUCTS =================
def load_products():
    logging.info("Loading product IDs...")
    for sym in SYMBOLS:
        r = requests.get(f"{BASE_URL}/v2/products/{sym}")
        product_ids[sym] = r.json()["result"]["id"]
    logging.info(f"Loaded Products: {product_ids}")

# ================= SYNC POSITIONS =================
def sync_positions():
    global positions_ready

    try:
        signature, timestamp = generate_signature("GET", "/v2/positions/margined")

        headers = {
            "api-key": API_KEY,
            "timestamp": timestamp,
            "signature": signature
        }

        r = requests.get(BASE_URL + "/v2/positions/margined", headers=headers)

        if r.status_code != 200:
            logging.error(f"Position sync failed: {r.text}")
            return

        data = r.json().get("result", [])

        with lock:
            updated_symbols = []

            for pos in data:
                for sym in SYMBOLS:
                    if product_ids[sym] == pos["product_id"]:
                        if float(pos.get("size", 0)) != 0:
                            positions[sym] = pos
                            updated_symbols.append(sym)

            # Reset missing or closed positions
            for sym in SYMBOLS:
                if sym not in updated_symbols:
                    positions[sym] = None

        positions_ready = True

    except Exception as e:
        logging.error(f"Position sync error: {e}")

# ================= POSITION SYNC LOOP =================
def position_sync_loop():
    while True:
        sync_positions()
        time.sleep(POSITION_SYNC_INTERVAL)

# ================= ORDER CACHE =================
def fetch_open_orders_loop():
    while True:
        try:
            query = "?state=open"
            signature, timestamp = generate_signature("GET", "/v2/orders", query)

            headers = {
                "api-key": API_KEY,
                "timestamp": timestamp,
                "signature": signature
            }

            r = requests.get(BASE_URL + "/v2/orders?state=open", headers=headers)

            if r.status_code == 200:
                data = r.json().get("result", [])

                with lock:
                    for sym in SYMBOLS:
                        open_orders_cache[sym] = [
                            o for o in data
                            if o["product_id"] == product_ids[sym]
                            and o["side"] == "sell"
                            and o["reduce_only"] is True
                            and o["state"] in ["open", "pending"]
                        ]

        except Exception as e:
            logging.error(f"Order fetch error: {e}")

        time.sleep(ORDER_SYNC_INTERVAL)

# ================= GET LOWEST SELL =================
def get_lowest_open_sell(sym):
    orders = open_orders_cache.get(sym, [])
    prices_list = [
        float(o["limit_price"])
        for o in orders
        if o.get("limit_price")
    ]
    return min(prices_list) if prices_list else None

# ================= PLACE TARGET =================
def place_target_order(sym, entry_price):

    product_id = product_ids[sym]
    lot = LOT_SIZE[sym]
    tp_price = round(entry_price * (1 + TP_PERCENT / 100), 4)

    body_data = {
        "product_id": product_id,
        "size": lot,
        "side": "sell",
        "order_type": "limit_order",
        "limit_price": str(tp_price),
        "time_in_force": "gtc",
        "reduce_only": True
    }

    body = json.dumps(body_data, separators=(',', ':'))
    signature, timestamp = generate_signature("POST", "/v2/orders", "", body)

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json"
    }

    r = requests.post(BASE_URL + "/v2/orders", headers=headers, data=body)

    logging.info(f"TARGET SET {sym} | TP:{tp_price}")
    logging.info(r.text)

# ================= PLACE LONG =================
def place_long(sym):

    product_id = product_ids[sym]
    lot = LOT_SIZE[sym]
    entry = prices[sym]

    if entry is None:
        return False

    body_data = {
        "product_id": product_id,
        "size": lot,
        "side": "buy",
        "order_type": "market_order",
        "time_in_force": "ioc",
        "reduce_only": False
    }

    body = json.dumps(body_data, separators=(',', ':'))
    signature, timestamp = generate_signature("POST", "/v2/orders", "", body)

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json"
    }

    r = requests.post(BASE_URL + "/v2/orders", headers=headers, data=body)

    logging.info(f"LONG {sym} | Entry:{entry}")
    logging.info(r.text)

    if r.status_code == 200 and r.json().get("success"):
        time.sleep(1)
        place_target_order(sym, entry)
        return True

    return False

# ================= TRADE LOGIC =================
def trade_logic(sym):

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

        # Safety reset
        if pos and float(pos.get("size", 0)) == 0:
            positions[sym] = None
            pos = None

        # FIRST ENTRY
        if not pos:
            if open_orders_cache[sym]:
                return

            logging.info(f"Opening FIRST LONG {sym}")

            active_order_flag[sym] = True
            if place_long(sym):
                last_trade[sym] = time.time()
            active_order_flag[sym] = False
            return

        # LADDER ENTRY
        lowest_sell = get_lowest_open_sell(sym)
        if not lowest_sell:
            return

        trigger = lowest_sell * (1 - DROP_PERCENT / 100)

        if last_trigger_price[sym] == trigger:
            return

        if prices[sym] <= trigger:

            logging.info(f"{sym} Drop Trigger Hit → Averaging LONG")

            active_order_flag[sym] = True

            if place_long(sym):
                last_trade[sym] = time.time()
                last_trigger_price[sym] = trigger

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
    signature, timestamp = generate_signature("GET", "/live")

    ws.send(json.dumps({
        "type": "auth",
        "payload": {
            "api-key": API_KEY,
            "signature": signature,
            "timestamp": timestamp
        }
    }))

    time.sleep(1)

    ws.send(json.dumps({
        "type": "subscribe",
        "payload": {
            "channels": [
                {"name": "v2/ticker", "symbols": SYMBOLS}
            ]
        }
    }))

def on_message(ws, message):
    data = json.loads(message)

    if "symbol" in data and "mark_price" in data:
        sym = data["symbol"]
        if sym in SYMBOLS:
            prices[sym] = float(data["mark_price"])
            trade_logic(sym)

def start_ws():
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message
            )
            ws.run_forever()
        except Exception as e:
            logging.error(e)
            time.sleep(5)

# ================= MAIN =================
if __name__ == "__main__":

    load_products()
    sync_positions()

    threading.Thread(target=position_sync_loop, daemon=True).start()
    threading.Thread(target=fetch_open_orders_loop, daemon=True).start()
    threading.Thread(target=dashboard_loop, daemon=True).start()

    start_ws()
