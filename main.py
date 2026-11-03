"""
SCALP BOT v1 — BTCUSDT, Binance Futures (Demo)
Strategjia: mean-reversion VWAP + RSI në 5m, hyrje MAKER (limit post-only), TP limit, SL i ngushtë.
Kontroll nga telefoni (Telegram): /status /pause /resume /stats /close
Variablat në Railway: BINANCE_API_KEY, BINANCE_API_SECRET, TELEGRAM_TOKEN, CHAT_ID, STATE_FILE=/data/scalp_state.json
"""
import os, time, json, hmac, hashlib, logging, threading, signal
from datetime import datetime, timezone
from urllib.parse import urlencode
from logging.handlers import RotatingFileHandler
import requests
from requests.adapters import HTTPAdapter

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- KONFIGURIMI I LLOGARISË ---
API_KEY = "vCVPFQecFIZVIin8MlfQENB4RtQ3LvJOEwswEBAeGcUuJx6CHu5AayJvqbvfD3dd"
API_SECRET = "5xSVmS1XGuMIoH0GxB3DUMgcYF49TEoscb6lBeEN1nlyB4ISCAVgwUcInPXeOOkl"
TELEGRAM_TOKEN = "8921013869:AAEGiG2Um3IcWb7RFBN_zyNNj12UUfZx1dA"
CHAT_ID = "5458100041"

BASE_URL = "https://demo-fapi.binance.com"  # Futures DEMO TRADING

STATE_FILE = os.getenv("STATE_FILE", "scalp_state.json")

SYMBOL = "BTCUSDT"
LEVERAGE = 75
MARGIN_USDT = 2.0                 # marzh për tregti (notional = marzh x levë = ~150 USDT; BTC kërkon min ~100 USDT)
TF = "5m"
LOOP_SECONDS = 5

# --- Strategjia ---
RSI_PERIOD = 14
RSI_LONG_MAX = 30
RSI_SHORT_MIN = 70
DEV_ATR_MULT = 1.5                # çmimi duhet të jetë >= 1.5 ATR larg VWAP
ATR_PERIOD = 14
MIN_ATR_PCT = 0.05                # filtër volatiliteti (% e çmimit): as shumë i qetë ...
MAX_ATR_PCT = 0.50                # ... as shumë i dhunshëm
MAX_BODY_ATR = 2.5                # mos hyr kundër kandele gjigante (rënie/rritje e shpejtë)
TP_MIN_PCT = 0.15                 # TP minimal (% çmimi) që të mbulojë komisionet
TP_DEV_FRACTION = 0.7             # TP = 70% e distancës drejt VWAP (por >= TP_MIN_PCT)
SL_ATR_MULT = 1.2
SL_MIN_PCT = 0.12
MIN_RR = 0.8
MAX_HOLD_SECONDS = 20 * 60        # time-stop: mbyll nëse s'ka dalë brenda 20 min

# --- Ekzekutimi ---
MAX_SPREAD_BPS = 1.5              # spread maksimal (bps) për hyrje
ENTRY_TIMEOUT_SECONDS = 45        # limit post-only: nëse s'mbushet, anulohet
TRADE_HOURS_UTC = (7, 21)         # vetëm Londra + New York (orë UTC), (0,24) = gjithë ditën

# --- Rreziku ---
MAX_TRADES_PER_DAY = 12
DAILY_LOSS_LIMIT_USDT = 1.5       # humbje ditore neto => pauzë deri nesër
COOLDOWN_AFTER_LOSS_SECONDS = 600
MAX_CONSECUTIVE_LOSSES = 4        # pas 4 humbjeve radhazi => pauzë (/resume për ta rikthyer)

# ===================== LOGGING / HTTP =====================
logger = logging.getLogger("scalp")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
for h in (logging.StreamHandler(), RotatingFileHandler("scalp.log", maxBytes=3_000_000, backupCount=3, encoding="utf-8")):
    h.setFormatter(_fmt)
    logger.addHandler(h)

SESSION = requests.Session()
_ad = HTTPAdapter(max_retries=2, pool_maxsize=10)
SESSION.mount("https://", _ad)
TIMEOUT = 10
_STOP = threading.Event()
_LOCK = threading.RLock()


def tg(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        r = SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                         data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=TIMEOUT)
        if r.status_code == 400:
            SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                         data={"chat_id": CHAT_ID, "text": msg}, timeout=TIMEOUT)
    except Exception as e:
        logger.error(f"Telegram: {e}")


def api(method, endpoint, params=None, signed=True):
    params = dict(params or {})
    headers = {"X-MBX-APIKEY": API_KEY}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        q = urlencode(params)
        params["signature"] = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    try:
        r = SESSION.request(method, BASE_URL + endpoint, params=params, headers=headers, timeout=TIMEOUT)
        return r.json()
    except Exception as e:
        logger.error(f"API {endpoint}: {e}")
        return {}


def is_err(resp):
    return isinstance(resp, dict) and isinstance(resp.get("code"), int) and resp["code"] < 0


# ===================== GJENDJA =====================
def default_state():
    return {"day": _today(), "trades_today": 0, "pnl_today": 0.0, "paused": False, "pause_reason": "",
            "cooldown_until": 0.0, "loss_streak": 0, "position": None,
            "stats": {"wins": 0, "losses": 0, "pnl": 0.0, "roi": 0.0}, "roi_today": 0.0, "last_candle": 0, "tg_offset": 0}


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            base = default_state()
            base.update(d)
            return base
        except Exception:
            pass
    return default_state()


def save_state():
    try:
        folder = os.path.dirname(STATE_FILE)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(STATE, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        logger.error(f"save_state: {e}")


STATE = load_state()
FILTERS = {"tick": 0.1, "step": 0.001, "min_notional": 100.0}


def _dec(step):
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def r_price(p):
    t = FILTERS["tick"]
    return round(round(p / t) * t, _dec(t))


def floor_qty(q):
    s = FILTERS["step"]
    return round(int(q / s + 1e-9) * s, _dec(s))


def load_filters():
    d = api("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in d.get("symbols", []):
        if s.get("symbol") == SYMBOL:
            for f in s.get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    FILTERS["tick"] = float(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    FILTERS["step"] = float(f["stepSize"])
                elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    FILTERS["min_notional"] = float(f.get("notional") or f.get("minNotional") or 100)
    logger.info(f"Filtrat: {FILTERS}")


# ===================== INDIKATORË =====================
def ema(vals, p):
    k = 2 / (p + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def atr(h, l, c, p=14):
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(h))]
    return sum(trs[-p:]) / p if len(trs) >= p else None


def rsi(closes, p=14):
    if len(closes) < p + 1:
        return None
    g = ls = 0.0
    for i in range(1, p + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        ls += max(-d, 0)
    ag, al = g / p, ls / p
    for i in range(p + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (p - 1) + max(d, 0)) / p
        al = (al * (p - 1) + max(-d, 0)) / p
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def get_signal():
    """Kthen (side, info) ose (None, arsye). Përdor vetëm kandela të mbyllura."""
    raw = api("GET", "/fapi/v1/klines", {"symbol": SYMBOL, "interval": TF, "limit": 300}, signed=False)
    if not isinstance(raw, list) or len(raw) < 60:
        return None, "pa kandila"
    raw = raw[:-1]
    candle_t = int(raw[-1][0])
    day_start = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    o = [float(x[1]) for x in raw]; h = [float(x[2]) for x in raw]
    l = [float(x[3]) for x in raw]; c = [float(x[4]) for x in raw]; v = [float(x[5]) for x in raw]
    day = [i for i, x in enumerate(raw) if int(x[0]) >= day_start]
    if len(day) < 12:
        return None, "pak kandila sot për VWAP"
    pv = sum(((h[i] + l[i] + c[i]) / 3) * v[i] for i in day)
    vv = sum(v[i] for i in day)
    if vv <= 0:
        return None, "pa volum"
    vwap = pv / vv
    a = atr(h, l, c, ATR_PERIOD)
    rs = rsi(c, RSI_PERIOD)
    if a is None or rs is None:
        return None, "pa indikatorë"
    px = c[-1]
    atr_pct = a / px * 100
    if not (MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT):
        return None, f"ATR% {atr_pct:.3f} jashtë kufijve"
    if abs(c[-1] - o[-1]) > MAX_BODY_ATR * a:
        return None, "kandele shumë e madhe"
    dev = px - vwap
    side = None
    if dev <= -DEV_ATR_MULT * a and rs <= RSI_LONG_MAX and c[-1] > o[-1]:
        side = "LONG"            # largim poshtë VWAP + RSI i ulët + kandelë kthimi jeshile
    elif dev >= DEV_ATR_MULT * a and rs >= RSI_SHORT_MIN and c[-1] < o[-1]:
        side = "SHORT"
    if not side:
        return None, f"s'ka setup (dev {dev / a:+.2f} ATR, RSI {rs:.0f})"
    dist = abs(dev)
    tp_d = max(TP_MIN_PCT / 100 * px, TP_DEV_FRACTION * dist)
    sl_d = max(SL_MIN_PCT / 100 * px, SL_ATR_MULT * a)
    if tp_d / sl_d < MIN_RR:
        return None, f"R:R {tp_d / sl_d:.2f} i ulët"
    return side, {"candle": candle_t, "vwap": vwap, "atr": a, "rsi": rs, "tp_d": tp_d, "sl_d": sl_d, "dev_atr": dev / a}


# ===================== EKZEKUTIMI =====================
def book():
    d = api("GET", "/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL}, signed=False)
    try:
        return float(d["bidPrice"]), float(d["askPrice"])
    except Exception:
        return None, None


def position_amt():
    d = api("GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL})
    if not isinstance(d, list):
        return None
    for p in d:
        if p.get("symbol") == SYMBOL:
            return float(p.get("positionAmt", 0))
    return 0.0


def cancel_all_orders():
    api("DELETE", "/fapi/v1/allOpenOrders", {"symbol": SYMBOL})
    pos = STATE.get("position") or {}
    if pos.get("sl_algo"):
        api("DELETE", "/fapi/v1/algoOrder", {"algoId": pos["sl_algo"]})


def market_close(side_open):
    amt = position_amt()
    if not amt:
        return
    side = "SELL" if side_open == "LONG" else "BUY"
    api("POST", "/fapi/v1/order", {"symbol": SYMBOL, "side": side, "type": "MARKET",
                                   "quantity": floor_qty(abs(amt)), "reduceOnly": "true"})


def try_enter(side, sig):
    bid, ask = book()
    if not bid or not ask:
        return False
    spread_bps = (ask - bid) / bid * 10000
    if spread_bps > MAX_SPREAD_BPS:
        logger.info(f"spread {spread_bps:.2f} bps — kalohet")
        return False
    px = bid if side == "LONG" else ask                      # maker: në anën tonë të librit
    qty = floor_qty(MARGIN_USDT * LEVERAGE / px)
    if qty * px < FILTERS["min_notional"]:
        qty = floor_qty(FILTERS["min_notional"] / px + FILTERS["step"])
    if qty * px / LEVERAGE > MARGIN_USDT * 1.6:
        tg(f"⚠️ Marzhi {MARGIN_USDT} USDT s'mjafton për min. notional ({FILTERS['min_notional']}). Rrite MARGIN_USDT.")
        return False
    order = api("POST", "/fapi/v1/order", {
        "symbol": SYMBOL, "side": "BUY" if side == "LONG" else "SELL", "type": "LIMIT",
        "timeInForce": "GTX", "price": r_price(px), "quantity": qty, "newOrderRespType": "RESULT"})
    if is_err(order) or "orderId" not in order:
        logger.warning(f"hyrja dështoi: {order}")
        return False
    oid = order["orderId"]
    t0 = time.time()
    filled, avg = 0.0, px
    while time.time() - t0 < ENTRY_TIMEOUT_SECONDS:
        st = api("GET", "/fapi/v1/order", {"symbol": SYMBOL, "orderId": oid})
        if st.get("status") == "FILLED":
            filled, avg = float(st["executedQty"]), float(st.get("avgPrice") or px)
            break
        if st.get("status") in ("CANCELED", "EXPIRED", "REJECTED"):
            filled = float(st.get("executedQty", 0) or 0)
            avg = float(st.get("avgPrice") or px)
            break
        time.sleep(1.5)
    else:
        api("DELETE", "/fapi/v1/order", {"symbol": SYMBOL, "orderId": oid})
        st = api("GET", "/fapi/v1/order", {"symbol": SYMBOL, "orderId": oid})
        filled = float(st.get("executedQty", 0) or 0)
        avg = float(st.get("avgPrice") or px)
    if filled <= 0:
        logger.info("limit s'u mbush — anuluar")
        return False

    filled = floor_qty(filled)
    sign = 1 if side == "LONG" else -1
    sl_price = r_price(avg - sign * sig["sl_d"])
    tp_price = r_price(avg + sign * sig["tp_d"])
    exit_side = "SELL" if side == "LONG" else "BUY"
    sl = api("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL", "symbol": SYMBOL, "side": exit_side, "type": "STOP_MARKET",
        "triggerPrice": sl_price, "closePosition": "true", "workingType": "MARK_PRICE"})
    if is_err(sl) or "algoId" not in sl:
        sl = api("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": SYMBOL, "side": exit_side, "type": "STOP_MARKET",
            "triggerPrice": sl_price, "closePosition": "true", "workingType": "MARK_PRICE"})
    if is_err(sl) or "algoId" not in sl:
        tg(f"🚨 SL dështoi — po mbyll pozicionin me MARKET. {sl}")
        market_close(side)
        return False
    tp = api("POST", "/fapi/v1/order", {
        "symbol": SYMBOL, "side": exit_side, "type": "LIMIT", "timeInForce": "GTC",
        "price": tp_price, "quantity": filled, "reduceOnly": "true"})
    STATE["position"] = {"side": side, "entry": avg, "qty": filled, "sl": sl_price, "tp": tp_price,
                         "sl_algo": sl.get("algoId"), "tp_order": tp.get("orderId"), "t": time.time(),
                         "risk_usdt": round(filled * sig["sl_d"], 4),
                         "margin": round(filled * avg / LEVERAGE, 6)}
    STATE["trades_today"] += 1
    save_state()
    e = "🟢" if side == "LONG" else "🔴"
    tg(f"{e} *{SYMBOL} {side}* (maker)\nEnter `{avg}` | qty {filled}\n"
       f"TP `{tp_price}` ({sig['tp_d'] / avg * 100:.2f}%) | SL `{sl_price}` ({sig['sl_d'] / avg * 100:.2f}%)\n"
       f"RSI {sig['rsi']:.0f} | dev {sig['dev_atr']:+.1f} ATR | marzh {STATE['position']['margin']:.2f} USDT\n"
       f"SL ≈ {-sig['sl_d'] / avg * LEVERAGE * 100:.0f}% ROE | TP ≈ +{sig['tp_d'] / avg * LEVERAGE * 100:.0f}% ROE\n"
       f"Tregti sot: {STATE['trades_today']}/{MAX_TRADES_PER_DAY}")
    return True


def net_pnl_since(ts):
    d = api("GET", "/fapi/v1/income", {"symbol": SYMBOL, "startTime": int((ts - 60) * 1000), "limit": 1000})
    tot = 0.0
    if isinstance(d, list):
        for r in d:
            if r.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                try:
                    tot += float(r["income"])
                except Exception:
                    pass
    return tot


def finish_position(reason):
    pos = STATE["position"]
    cancel_all_orders()
    time.sleep(2)
    pnl = round(net_pnl_since(pos["t"]), 4)
    margin = pos.get("margin") or MARGIN_USDT
    roi = round(pnl / margin * 100, 2)                      # ROI % mbi marzh (si te bot-i tjetër)
    s = STATE["stats"]
    STATE["pnl_today"] = round(STATE["pnl_today"] + pnl, 4)
    STATE["roi_today"] = round(STATE.get("roi_today", 0.0) + roi, 2)
    s["pnl"] = round(s["pnl"] + pnl, 4)
    s["roi"] = round(s.get("roi", 0.0) + roi, 2)
    if pnl >= 0:
        s["wins"] += 1
        STATE["loss_streak"] = 0
    else:
        s["losses"] += 1
        STATE["loss_streak"] += 1
        STATE["cooldown_until"] = time.time() + COOLDOWN_AFTER_LOSS_SECONDS
    mins = (time.time() - pos["t"]) / 60
    STATE["position"] = None
    save_state()
    tg(f"{'✅' if pnl >= 0 else '🔴'} *{SYMBOL} {pos['side']} u mbyll* ({reason})\n"
       f"{'✅ FITIM' if pnl >= 0 else '🔴 HUMBJE'}: `{roi:+.2f}%` (ROI mbi marzh, neto pas komisioneve)\n"
       f"({pnl:+.4f} USDT) | koha {mins:.1f} min\n"
       f"Sot: {STATE['roi_today']:+.2f}% ({STATE['pnl_today']:+.3f} USDT) | {STATE['trades_today']} tregti")
    if STATE["pnl_today"] <= -DAILY_LOSS_LIMIT_USDT:
        STATE["paused"], STATE["pause_reason"] = True, "kufiri ditor i humbjes"
        save_state()
        tg(f"⛔ Kufiri ditor i humbjes ({DAILY_LOSS_LIMIT_USDT} USDT) u arrit. Bot-i në pauzë deri nesër.")
    elif STATE["loss_streak"] >= MAX_CONSECUTIVE_LOSSES:
        STATE["paused"], STATE["pause_reason"] = True, f"{MAX_CONSECUTIVE_LOSSES} humbje radhazi"
        save_state()
        tg(f"⛔ {MAX_CONSECUTIVE_LOSSES} humbje radhazi. Bot-i në pauzë — dërgo /resume kur të duash.")


def manage_position():
    pos = STATE["position"]
    amt = position_amt()
    if amt is None:
        return
    if abs(amt) < FILTERS["step"] / 2:
        finish_position("TP/SL")
        return
    if time.time() - pos["t"] > MAX_HOLD_SECONDS:
        market_close(pos["side"])
        time.sleep(2)
        finish_position("time-stop")


# ===================== CIKLI =====================
def in_trade_hours():
    hr = datetime.now(timezone.utc).hour
    return TRADE_HOURS_UTC[0] <= hr < TRADE_HOURS_UTC[1]


def new_day_check():
    if STATE["day"] != _today():
        STATE.update({"day": _today(), "trades_today": 0, "pnl_today": 0.0, "roi_today": 0.0})
        if STATE["paused"] and STATE["pause_reason"] == "kufiri ditor i humbjes":
            STATE["paused"], STATE["pause_reason"] = False, ""
            tg("🌅 Ditë e re — bot-i rifilloi (pauza ditore u hoq).")
        save_state()


def tick():
    new_day_check()
    if STATE["position"]:
        manage_position()
        return
    if STATE["paused"] or STATE["trades_today"] >= MAX_TRADES_PER_DAY:
        return
    if time.time() < STATE["cooldown_until"] or not in_trade_hours():
        return
    bucket = int(time.time() // 300)               # analizo 1 herë për çdo kandelë 5m të re
    if STATE["last_candle"] == bucket:
        return
    if time.time() % 300 < 3:                      # prit pak që kandela të mbyllet te Binance
        return
    STATE["last_candle"] = bucket
    side, info = get_signal()
    if not side:
        logger.info(f"pa sinjal: {info}")
        return
    logger.info(f"SINJAL {side}: {info}")
    try_enter(side, info)


# ===================== TELEGRAM (kontroll nga telefoni) =====================
def status_text():
    s = STATE["stats"]
    n = s["wins"] + s["losses"]
    wr = f"{s['wins'] / n * 100:.0f}%" if n else "-"
    p = STATE["position"]
    ptxt = (f"{p['side']} @ {p['entry']} | TP {p['tp']} | SL {p['sl']}") if p else "asnjë"
    bid, ask = book()
    return (f"🤖 Scalp {SYMBOL} | {'⏸ PAUZË (' + STATE['pause_reason'] + ')' if STATE['paused'] else '▶️ aktiv'}\n"
            f"Çmimi: {bid}\nPozicion: {ptxt}\n"
            f"Sot: {STATE.get('roi_today', 0.0):+.2f}% ({STATE['pnl_today']:+.3f} USDT) | {STATE['trades_today']}/{MAX_TRADES_PER_DAY} tregti\n"
            f"Gjithsej: {s['wins']}W/{s['losses']}L ({wr}) | ROI total {s.get('roi', 0.0):+.2f}% ({s['pnl']:+.3f} USDT)\n"
            f"Orë tregtimi: {'po' if in_trade_hours() else 'jo'} (UTC {TRADE_HOURS_UTC[0]}-{TRADE_HOURS_UTC[1]})")


def handle_command(text):
    cmd = text.strip().split()[0].lower().split("@")[0]
    with _LOCK:
        if cmd in ("/status", "/stats"):
            tg(status_text())
        elif cmd == "/pause":
            STATE["paused"], STATE["pause_reason"] = True, "manual"
            save_state()
            tg("⏸ Pauzë (pozicioni ekzistues menaxhohet normalisht).")
        elif cmd == "/resume":
            STATE["paused"], STATE["pause_reason"], STATE["loss_streak"] = False, "", 0
            save_state()
            tg("▶️ Bot-i rifilloi.")
        elif cmd == "/close":
            p = STATE["position"]
            if not p:
                tg("S'ka pozicion të hapur.")
            else:
                market_close(p["side"])
                time.sleep(2)
                finish_position("mbyllje manuale")
        else:
            tg("Komandat: /status /pause /resume /stats /close")


def telegram_loop():
    while not _STOP.is_set():
        try:
            r = SESSION.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                            params={"offset": STATE.get("tg_offset", 0), "timeout": 20}, timeout=30).json()
            for u in r.get("result", []):
                STATE["tg_offset"] = u["update_id"] + 1
                m = u.get("message") or {}
                if str((m.get("chat") or {}).get("id")) != CHAT_ID:
                    continue                                   # vetëm ti mund ta komandosh
                if m.get("text", "").startswith("/"):
                    handle_command(m["text"])
            save_state()
        except Exception as e:
            logger.error(f"telegram_loop: {e}")
            time.sleep(5)


def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit("Mungojnë BINANCE_API_KEY / BINANCE_API_SECRET")
    signal.signal(signal.SIGTERM, lambda *a: _STOP.set())
    signal.signal(signal.SIGINT, lambda *a: _STOP.set())
    load_filters()
    api("POST", "/fapi/v1/marginType", {"symbol": SYMBOL, "marginType": "CROSSED"})
    lv = api("POST", "/fapi/v1/leverage", {"symbol": SYMBOL, "leverage": LEVERAGE})
    logger.info(f"Leva: {lv}")
    tg(f"🚀 Scalp bot u nis: {SYMBOL} | {BASE_URL}\nKomandat: /status /pause /resume /stats /close")
    if STATE["position"] is None and (position_amt() or 0) != 0:
        tg("⚠️ Ka pozicion të hapur në Binance që bot-i s'e njeh. Mbylle manualisht ose me /close pas rinisjes.")
    threading.Thread(target=telegram_loop, daemon=True).start()
    while not _STOP.is_set():
        try:
            with _LOCK:
                tick()
        except Exception:
            logger.exception("gabim në cikël")
        _STOP.wait(LOOP_SECONDS)
    save_state()
    tg("🛑 Scalp bot u ndal. Pozicionet/porositë ekzistuese mbeten në Binance.")


if __name__ == "__main__":
    main()
