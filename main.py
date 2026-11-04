"""
SCALP BOT DUO v1 — BTCUSDT + XAUUSDT në NJË kod/service, Binance Futures (Demo)
Strategjia: mean-reversion VWAP + RSI në 1m, hyrje MAKER (limit post-only), TP limit, SL i ngushtë.
Telegram: /status /stats /pause [btc|xau] /resume [btc|xau] /close [btc|xau] /backtest [ditë] [btc|xau]
Variablat në Railway: BINANCE_API_KEY, BINANCE_API_SECRET, TELEGRAM_TOKEN, CHAT_ID,
                      STATE_FILE=/data/scalp_state.json, (opsionale) SYMBOLS=BTCUSDT,XAUUSDT
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

BASE_URL = os.getenv("BASE_URL", "https://demo-fapi.binance.com")
STATE_FILE = os.getenv("STATE_FILE", "scalp_state.json")

# Parametrat sipas simbolit (leva 125 = kërkesa; bot-i e zbret vetë në maksimumin e lejuar të simbolit)
PROFILES = {
    "BTCUSDT": dict(lev=125, margin=2.0, min_atr=0.02, max_atr=0.25, tp_min=0.10, sl_min=0.08, max_spread_bps=1.5),
    "XAUUSDT": dict(lev=125, margin=2.0, min_atr=0.015, max_atr=0.15, tp_min=0.10, sl_min=0.08, max_spread_bps=4.0),
}
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,XAUUSDT").split(",") if s.strip().upper() in PROFILES]
ALIASES = {"btc": "BTCUSDT", "btcusdt": "BTCUSDT", "xau": "XAUUSDT", "xauusdt": "XAUUSDT", "gold": "XAUUSDT", "ari": "XAUUSDT"}

TF = "1m"
TF_SECONDS = 60
LOOP_SECONDS = 5

# --- Strategjia (e përbashkët) ---
RSI_PERIOD = 14
RSI_LONG_MAX = 25
RSI_SHORT_MIN = 75
DEV_ATR_MULT = 2.0                # çmimi duhet të jetë >= 2 ATR larg VWAP
ATR_PERIOD = 14
MAX_BODY_ATR = 2.5                # mos hyr kundër kandele gjigante
TP_DEV_FRACTION = 0.7             # TP = 70% e distancës drejt VWAP (por >= tp_min)
SL_ATR_MULT = 1.2
MIN_RR = 0.8
MAX_HOLD_SECONDS = 10 * 60        # time-stop

# --- Ekzekutimi ---
ENTRY_TIMEOUT_SECONDS = 30        # limit post-only: nëse s'mbushet, anulohet
TRADE_HOURS_UTC = (7, 21)         # vetëm Londra + New York; (0,24) = gjithë ditën

# --- Rreziku ---
MAX_TRADES_PER_DAY = 30           # për simbol
DAILY_LOSS_LIMIT_USDT = 2.0       # humbje ditore neto (të dy simbolet bashkë) => pauzë deri nesër
COOLDOWN_AFTER_LOSS_SECONDS = 300
MAX_CONSECUTIVE_LOSSES = 4        # për simbol => pauzë e atij simboli

# ===================== LOGGING / HTTP =====================
logger = logging.getLogger("scalp")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
for h_ in (logging.StreamHandler(), RotatingFileHandler("scalp.log", maxBytes=3_000_000, backupCount=3, encoding="utf-8")):
    h_.setFormatter(_fmt)
    logger.addHandler(h_)

SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=2, pool_maxsize=10))
TIMEOUT = 10
_STOP = threading.Event()
_LOCK = threading.RLock()


def tg(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = SESSION.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=TIMEOUT)
        if r.status_code == 400:
            SESSION.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=TIMEOUT)
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
def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def new_sym_state():
    return {"trades_today": 0, "pnl_today": 0.0, "roi_today": 0.0, "cooldown_until": 0.0, "loss_streak": 0,
            "paused": False, "position": None, "last_candle": 0,
            "stats": {"wins": 0, "losses": 0, "pnl": 0.0, "roi": 0.0}}


def default_state():
    return {"day": _today(), "paused": False, "pause_reason": "", "tg_offset": 0, "sym": {}}


def load_state():
    base = default_state()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            base["day"] = d.get("day", base["day"])
            base["paused"] = d.get("paused", False)
            base["pause_reason"] = d.get("pause_reason", "")
            base["tg_offset"] = d.get("tg_offset", 0)
            base["sym"] = d.get("sym", {})
        except Exception:
            pass
    return base


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
FILTERS = {}
LEV = {sym: PROFILES[sym]["lev"] for sym in PROFILES}
ACTIVE = []          # simbolet që ekzistojnë dhe tregtohen në Binance
_WARN_TS = {}


def S(sym):
    st = STATE["sym"].setdefault(sym, new_sym_state())
    for k, v in new_sym_state().items():
        st.setdefault(k, v)
    return st


def _dec(step):
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def r_price(sym, p):
    t = FILTERS[sym]["tick"]
    return round(round(p / t) * t, _dec(t))


def floor_qty(sym, q):
    s = FILTERS[sym]["step"]
    return round(int(q / s + 1e-9) * s, _dec(s))


def load_filters():
    d = api("GET", "/fapi/v1/exchangeInfo", signed=False)
    found = {}
    for s in d.get("symbols", []):
        sym = s.get("symbol")
        if sym in SYMBOLS:
            f_ = {"tick": 0.1, "step": 0.001, "min_notional": 100.0, "status": s.get("status")}
            for f in s.get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    f_["tick"] = float(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    f_["step"] = float(f["stepSize"])
                elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    f_["min_notional"] = float(f.get("notional") or f.get("minNotional") or 100)
            found[sym] = f_
    FILTERS.update(found)
    ACTIVE.clear()
    for sym in SYMBOLS:
        if sym not in found:
            tg(f"⚠️ {sym} nuk u gjet te exchangeInfo (mund të mos jetë i disponueshëm në Demo) — kalohet.")
        else:
            ACTIVE.append(sym)
    logger.info(f"Filtrat: {FILTERS} | aktive: {ACTIVE}")


# ===================== INDIKATORË =====================
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


def evaluate_setup(sym, o_last, c_last, vwap, a, rs):
    """Logjika e sinjalit (live + backtest). Kthen (side, info) ose (None, arsye)."""
    P = PROFILES[sym]
    px = c_last
    atr_pct = a / px * 100
    if not (P["min_atr"] <= atr_pct <= P["max_atr"]):
        return None, f"ATR% {atr_pct:.3f} jashtë kufijve"
    if abs(c_last - o_last) > MAX_BODY_ATR * a:
        return None, "kandele shumë e madhe"
    dev = px - vwap
    side = None
    if dev <= -DEV_ATR_MULT * a and rs <= RSI_LONG_MAX and c_last > o_last:
        side = "LONG"            # largim poshtë VWAP + RSI i ulët + kandelë kthimi jeshile
    elif dev >= DEV_ATR_MULT * a and rs >= RSI_SHORT_MIN and c_last < o_last:
        side = "SHORT"
    if not side:
        return None, f"s'ka setup (dev {dev / a:+.2f} ATR, RSI {rs:.0f})"
    tp_d = max(P["tp_min"] / 100 * px, TP_DEV_FRACTION * abs(dev))
    sl_d = max(P["sl_min"] / 100 * px, SL_ATR_MULT * a)
    if tp_d / sl_d < MIN_RR:
        return None, f"R:R {tp_d / sl_d:.2f} i ulët"
    return side, {"vwap": vwap, "atr": a, "rsi": rs, "tp_d": tp_d, "sl_d": sl_d, "dev_atr": dev / a}


def get_signal(sym):
    raw = api("GET", "/fapi/v1/klines", {"symbol": sym, "interval": TF, "limit": 1000}, signed=False)
    if not isinstance(raw, list) or len(raw) < 60:
        return None, "pa kandila"
    raw = raw[:-1]
    candle_t = int(raw[-1][0])
    if time.time() * 1000 - candle_t > 15 * 60 * 1000:
        return None, "tregu i mbyllur / pa kandila të freskëta"
    day_start = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    o = [float(x[1]) for x in raw]; h = [float(x[2]) for x in raw]
    l = [float(x[3]) for x in raw]; c = [float(x[4]) for x in raw]; v = [float(x[5]) for x in raw]
    day = [i for i, x in enumerate(raw) if int(x[0]) >= day_start]
    if len(day) < 30:
        return None, "pak kandila sot për VWAP"
    vv = sum(v[i] for i in day)
    if vv <= 0:
        return None, "pa volum"
    vwap = sum(((h[i] + l[i] + c[i]) / 3) * v[i] for i in day) / vv
    a = atr(h, l, c, ATR_PERIOD)
    rs = rsi(c, RSI_PERIOD)
    if a is None or rs is None:
        return None, "pa indikatorë"
    side, info = evaluate_setup(sym, o[-1], c[-1], vwap, a, rs)
    if not side:
        return None, info
    info["candle"] = candle_t
    return side, info


# ===================== EKZEKUTIMI =====================
def book(sym):
    d = api("GET", "/fapi/v1/ticker/bookTicker", {"symbol": sym}, signed=False)
    try:
        return float(d["bidPrice"]), float(d["askPrice"])
    except Exception:
        return None, None


def position_amt(sym):
    d = api("GET", "/fapi/v2/positionRisk", {"symbol": sym})
    if not isinstance(d, list):
        return None
    for p in d:
        if p.get("symbol") == sym:
            return float(p.get("positionAmt", 0))
    return 0.0


def cancel_all_orders(sym):
    api("DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym})
    pos = S(sym).get("position") or {}
    if pos.get("sl_algo"):
        api("DELETE", "/fapi/v1/algoOrder", {"algoId": pos["sl_algo"]})


def market_close(sym, side_open):
    amt = position_amt(sym)
    if not amt:
        return
    api("POST", "/fapi/v1/order", {"symbol": sym, "side": "SELL" if side_open == "LONG" else "BUY",
                                   "type": "MARKET", "quantity": floor_qty(sym, abs(amt)), "reduceOnly": "true"})


def warn_throttled(key, msg, every=3600):
    if time.time() - _WARN_TS.get(key, 0) > every:
        _WARN_TS[key] = time.time()
        tg(msg)


def try_enter(sym, side, sig):
    P, lev, st = PROFILES[sym], LEV[sym], S(sym)
    bid, ask = book(sym)
    if not bid or not ask:
        return False
    spread_bps = (ask - bid) / bid * 10000
    if spread_bps > P["max_spread_bps"]:
        logger.info(f"{sym}: spread {spread_bps:.2f} bps — kalohet")
        return False
    px = bid if side == "LONG" else ask                      # maker: në anën tonë të librit
    qty = floor_qty(sym, P["margin"] * lev / px)
    mn = FILTERS[sym]["min_notional"]
    if qty * px < mn:
        qty = floor_qty(sym, mn / px + FILTERS[sym]["step"])
    if qty <= 0 or qty * px / lev > P["margin"] * 1.6:
        warn_throttled(f"margin_{sym}", f"⚠️ {sym}: marzhi {P['margin']} USDT s'mjafton për min. notional ({mn}). Rrite margin te PROFILES.")
        return False
    order = api("POST", "/fapi/v1/order", {
        "symbol": sym, "side": "BUY" if side == "LONG" else "SELL", "type": "LIMIT",
        "timeInForce": "GTX", "price": r_price(sym, px), "quantity": qty, "newOrderRespType": "RESULT"})
    if is_err(order) or "orderId" not in order:
        logger.warning(f"{sym}: hyrja dështoi: {order}")
        return False
    oid = order["orderId"]
    t0 = time.time()
    filled, avg = 0.0, px
    while time.time() - t0 < ENTRY_TIMEOUT_SECONDS:
        o_ = api("GET", "/fapi/v1/order", {"symbol": sym, "orderId": oid})
        if o_.get("status") == "FILLED":
            filled, avg = float(o_["executedQty"]), float(o_.get("avgPrice") or px)
            break
        if o_.get("status") in ("CANCELED", "EXPIRED", "REJECTED"):
            filled = float(o_.get("executedQty", 0) or 0)
            avg = float(o_.get("avgPrice") or px)
            break
        time.sleep(1.5)
    else:
        api("DELETE", "/fapi/v1/order", {"symbol": sym, "orderId": oid})
        o_ = api("GET", "/fapi/v1/order", {"symbol": sym, "orderId": oid})
        filled = float(o_.get("executedQty", 0) or 0)
        avg = float(o_.get("avgPrice") or px)
    if filled <= 0:
        logger.info(f"{sym}: limit s'u mbush — anuluar")
        return False

    filled = floor_qty(sym, filled)
    if filled <= 0:
        market_close(sym, side)
        return False
    sign = 1 if side == "LONG" else -1
    sl_price = r_price(sym, avg - sign * sig["sl_d"])
    tp_price = r_price(sym, avg + sign * sig["tp_d"])
    exit_side = "SELL" if side == "LONG" else "BUY"
    sl_params = {"algoType": "CONDITIONAL", "symbol": sym, "side": exit_side, "type": "STOP_MARKET",
                 "triggerPrice": sl_price, "closePosition": "true", "workingType": "MARK_PRICE"}
    sl = api("POST", "/fapi/v1/algoOrder", sl_params)
    if is_err(sl) or "algoId" not in sl:
        sl = api("POST", "/fapi/v1/algoOrder", sl_params)
    if is_err(sl) or "algoId" not in sl:
        tg(f"🚨 {sym}: SL dështoi — po mbyll pozicionin me MARKET. {sl}")
        market_close(sym, side)
        return False
    tp = api("POST", "/fapi/v1/order", {
        "symbol": sym, "side": exit_side, "type": "LIMIT", "timeInForce": "GTC",
        "price": tp_price, "quantity": filled, "reduceOnly": "true"})
    margin = round(filled * avg / lev, 6)
    st["position"] = {"side": side, "entry": avg, "qty": filled, "sl": sl_price, "tp": tp_price,
                      "sl_algo": sl.get("algoId"), "tp_order": tp.get("orderId"), "t": time.time(),
                      "risk_usdt": round(filled * sig["sl_d"], 4), "margin": margin, "lev": lev}
    st["trades_today"] += 1
    save_state()
    e = "🟢" if side == "LONG" else "🔴"
    tg(f"{e} *{sym} {side}* (maker, {lev}x)\nEnter `{avg}` | qty {filled}\n"
       f"TP `{tp_price}` ({sig['tp_d'] / avg * 100:.2f}%) | SL `{sl_price}` ({sig['sl_d'] / avg * 100:.2f}%)\n"
       f"RSI {sig['rsi']:.0f} | dev {sig['dev_atr']:+.1f} ATR | marzh {margin:.2f} USDT\n"
       f"SL ≈ {-sig['sl_d'] / avg * lev * 100:.0f}% ROE | TP ≈ +{sig['tp_d'] / avg * lev * 100:.0f}% ROE\n"
       f"Tregti sot ({sym}): {st['trades_today']}/{MAX_TRADES_PER_DAY}")
    return True


def net_pnl_since(sym, ts):
    d = api("GET", "/fapi/v1/income", {"symbol": sym, "startTime": int((ts - 60) * 1000), "limit": 1000})
    tot = 0.0
    if isinstance(d, list):
        for r in d:
            if r.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                try:
                    tot += float(r["income"])
                except Exception:
                    pass
    return tot


def total_pnl_today():
    return sum(S(s)["pnl_today"] for s in ACTIVE)


def finish_position(sym, reason):
    st = S(sym)
    pos = st["position"]
    cancel_all_orders(sym)
    time.sleep(2)
    pnl = round(net_pnl_since(sym, pos["t"]), 4)
    margin = pos.get("margin") or PROFILES[sym]["margin"]
    roi = round(pnl / margin * 100, 2)                      # ROI % mbi marzh (si te bot-i tjetër)
    s = st["stats"]
    st["pnl_today"] = round(st["pnl_today"] + pnl, 4)
    st["roi_today"] = round(st["roi_today"] + roi, 2)
    s["pnl"] = round(s["pnl"] + pnl, 4)
    s["roi"] = round(s.get("roi", 0.0) + roi, 2)
    if pnl >= 0:
        s["wins"] += 1
        st["loss_streak"] = 0
    else:
        s["losses"] += 1
        st["loss_streak"] += 1
        st["cooldown_until"] = time.time() + COOLDOWN_AFTER_LOSS_SECONDS
    mins = (time.time() - pos["t"]) / 60
    st["position"] = None
    save_state()
    tg(f"{'✅' if pnl >= 0 else '🔴'} *{sym} {pos['side']} u mbyll* ({reason})\n"
       f"{'✅ FITIM' if pnl >= 0 else '🔴 HUMBJE'}: `{roi:+.2f}%` (ROI mbi marzh, neto pas komisioneve)\n"
       f"({pnl:+.4f} USDT) | koha {mins:.1f} min\n"
       f"Sot {sym}: {st['roi_today']:+.2f}% | Gjithsej sot: {total_pnl_today():+.3f} USDT")
    if total_pnl_today() <= -DAILY_LOSS_LIMIT_USDT:
        STATE["paused"], STATE["pause_reason"] = True, "kufiri ditor i humbjes"
        save_state()
        tg(f"⛔ Kufiri ditor i humbjes ({DAILY_LOSS_LIMIT_USDT} USDT) u arrit. Bot-i në pauzë deri nesër.")
    elif st["loss_streak"] >= MAX_CONSECUTIVE_LOSSES:
        st["paused"] = True
        save_state()
        tg(f"⛔ {sym}: {MAX_CONSECUTIVE_LOSSES} humbje radhazi. Ky simbol në pauzë — dërgo /resume {sym[:3].lower()}.")


def manage_position(sym):
    pos = S(sym)["position"]
    amt = position_amt(sym)
    if amt is None:
        return
    if abs(amt) < FILTERS[sym]["step"] / 2:
        finish_position(sym, "TP/SL")
        return
    if time.time() - pos["t"] > MAX_HOLD_SECONDS:
        market_close(sym, pos["side"])
        time.sleep(2)
        finish_position(sym, "time-stop")


# ===================== CIKLI =====================
def in_trade_hours():
    return TRADE_HOURS_UTC[0] <= datetime.now(timezone.utc).hour < TRADE_HOURS_UTC[1]


def new_day_check():
    if STATE["day"] != _today():
        STATE["day"] = _today()
        for sym in ACTIVE:
            st = S(sym)
            st.update({"trades_today": 0, "pnl_today": 0.0, "roi_today": 0.0})
        if STATE["paused"] and STATE["pause_reason"] == "kufiri ditor i humbjes":
            STATE["paused"], STATE["pause_reason"] = False, ""
            tg("🌅 Ditë e re — bot-i rifilloi (pauza ditore u hoq).")
        save_state()


def tick_symbol(sym):
    st = S(sym)
    if st["position"]:
        manage_position(sym)
        return
    if STATE["paused"] or st["paused"] or st["trades_today"] >= MAX_TRADES_PER_DAY:
        return
    if time.time() < st["cooldown_until"] or not in_trade_hours():
        return
    bucket = int(time.time() // TF_SECONDS)        # analizo 1 herë për çdo kandelë 1m të re
    if st["last_candle"] == bucket or time.time() % TF_SECONDS < 3:
        return
    st["last_candle"] = bucket
    side, info = get_signal(sym)
    if not side:
        logger.info(f"{sym}: pa sinjal: {info}")
        return
    logger.info(f"{sym}: SINJAL {side}: {info}")
    try_enter(sym, side, info)


def tick():
    new_day_check()
    for sym in ACTIVE:
        try:
            tick_symbol(sym)
        except Exception:
            logger.exception(f"{sym}: gabim")


# ===================== BACKTEST (/backtest 7 btc) =====================
def fetch_history(sym, days):
    end = int(time.time() * 1000)
    cur = end - days * 86400 * 1000
    rows = []
    while cur < end:
        d = api("GET", "/fapi/v1/klines", {"symbol": sym, "interval": TF, "startTime": cur, "limit": 1500}, signed=False)
        if not isinstance(d, list) or not d:
            break
        rows.extend(d)
        nxt = int(d[-1][0]) + TF_SECONDS * 1000
        if nxt <= cur or len(d) < 1500:
            break
        cur = nxt
        time.sleep(0.2)
    return rows[:-1]


def rsi_series(cl, p=14):
    out = [None] * len(cl)
    if len(cl) <= p:
        return out
    g = ls = 0.0
    for i in range(1, p + 1):
        d = cl[i] - cl[i - 1]
        g += max(d, 0)
        ls += max(-d, 0)
    ag, al = g / p, ls / p
    out[p] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(p + 1, len(cl)):
        d = cl[i] - cl[i - 1]
        ag = (ag * (p - 1) + max(d, 0)) / p
        al = (al * (p - 1) + max(-d, 0)) / p
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def backtest(sym, days):
    """Konservativ: nëse SL dhe TP preken në të njëjtën kandelë, llogaritet SL."""
    rows = fetch_history(sym, days)
    n = len(rows)
    if n < 200:
        return None
    lev = LEV[sym]
    t = [int(r[0]) for r in rows]
    o = [float(r[1]) for r in rows]; h = [float(r[2]) for r in rows]
    l = [float(r[3]) for r in rows]; c = [float(r[4]) for r in rows]; v = [float(r[5]) for r in rows]
    rs = rsi_series(c, RSI_PERIOD)
    tr = [0.0] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    hold_max = max(1, MAX_HOLD_SECONDS // TF_SECONDS)
    cool = COOLDOWN_AFTER_LOSS_SECONDS // TF_SECONDS
    trades, busy_until = [], 0
    pv = vv = 0.0
    cur_day, day_n = None, 0
    for i in range(n - 2):
        day = t[i] // 86400000
        if day != cur_day:
            cur_day, pv, vv, day_n = day, 0.0, 0.0, 0
        pv += ((h[i] + l[i] + c[i]) / 3) * v[i]
        vv += v[i]
        day_n += 1
        if i < busy_until or i < 20 or day_n < 30 or vv <= 0 or rs[i] is None:
            continue
        hr = (t[i] // 3600000) % 24
        if not (TRADE_HOURS_UTC[0] <= hr < TRADE_HOURS_UTC[1]):
            continue
        a = sum(tr[i - 13:i + 1]) / 14
        side, info = evaluate_setup(sym, o[i], c[i], pv / vv, a, rs[i])
        if not side:
            continue
        sign = 1 if side == "LONG" else -1
        entry = c[i]
        sl = entry - sign * info["sl_d"]
        tp = entry + sign * info["tp_d"]
        k = i + 1
        if not ((l[k] <= entry) if sign == 1 else (h[k] >= entry)):
            continue                                   # limit maker s'u mbush
        last = min(n - 1, k + hold_max)
        exit_px, reason, j = c[last], "time", last
        for jj in range(k + 1, last + 1):
            hit_sl = (l[jj] <= sl) if sign == 1 else (h[jj] >= sl)
            hit_tp = (h[jj] >= tp) if sign == 1 else (l[jj] <= tp)
            if hit_sl:
                exit_px, reason, j = sl, "SL", jj
                break
            if hit_tp:
                exit_px, reason, j = tp, "TP", jj
                break
        gross = (exit_px - entry) / entry * sign * lev * 100
        fee_exit = 0.0002 if reason == "TP" else 0.0005
        net = gross - (0.0002 + fee_exit) * lev * 100
        trades.append({"gross": gross, "net": net, "reason": reason})
        busy_until = j + 1 + (cool if gross < 0 else 0)
    return {"trades": trades, "candles": n, "lev": lev}


def run_backtest(syms, days):
    for sym in syms:
        try:
            tg(f"⏳ Backtest {sym} {TF}, {days} ditë... (mund të zgjasë 1-2 min)")
            res = backtest(sym, days)
            if not res:
                tg(f"Backtest {sym}: s'u morën të dhëna historike nga Binance.")
                continue
            tr = res["trades"]
            if not tr:
                tg(f"Backtest {sym}: 0 tregti në {days} ditë ({res['candles']} kandela). Filtrat janë shumë të rreptë.")
                continue
            wins = [x["gross"] for x in tr if x["gross"] > 0]
            losses = [x["gross"] for x in tr if x["gross"] <= 0]
            streak = best = 0
            for x in tr:
                streak = streak + 1 if x["gross"] <= 0 else 0
                best = max(best, streak)
            total = sum(x["gross"] for x in tr)
            net_total = sum(x["net"] for x in tr)
            pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
            cnt = {k: sum(1 for x in tr if x["reason"] == k) for k in ("TP", "SL", "time")}
            tg(f"📊 *BACKTEST {sym}* ({TF}, {days} ditë, levë {res['lev']}x, pa komisione)\n"
               f"Tregti: {len(tr)} (≈ {len(tr) / days:.1f}/ditë) | TP {cnt['TP']} | SL {cnt['SL']} | time-stop {cnt['time']}\n"
               f"Win-rate: {len(wins) / len(tr) * 100:.1f}% | profit factor {pf:.2f}\n"
               f"Mesatarisht: fitim {(sum(wins) / len(wins) if wins else 0):+.1f}% | humbje {(sum(losses) / len(losses) if losses else 0):+.1f}% (ROE mbi marzh)\n"
               f"*ROI total bruto: {total:+.0f}%* | mesatarja {total / days:+.1f}%/ditë\n"
               f"Humbje radhazi (max): {best}\n"
               f"(info) me komisione reale: {net_total:+.0f}%\n"
               f"Supozime: SL numërohet i pari kur preken të dyja në një kandelë; limiti maker mbushet nëse çmimi e prek.")
        except Exception as e:
            logger.exception("backtest")
            tg(f"Backtest {sym} dështoi: {e}")


# ===================== TELEGRAM (kontroll nga telefoni) =====================
def status_text():
    lines = [f"🤖 Scalp {TF} | {'⏸ PAUZË (' + STATE['pause_reason'] + ')' if STATE['paused'] else '▶️ aktiv'} | "
             f"orë tregtimi: {'po' if in_trade_hours() else 'jo'} (UTC {TRADE_HOURS_UTC[0]}-{TRADE_HOURS_UTC[1]})"]
    roi_today = roi_total = 0.0
    for sym in ACTIVE:
        st = S(sym)
        s = st["stats"]
        p = st["position"]
        ptxt = f"{p['side']} @ {p['entry']} (TP {p['tp']} | SL {p['sl']})" if p else "pa pozicion"
        px, _ = book(sym)
        n = s["wins"] + s["losses"]
        wr = f"{s['wins'] / n * 100:.0f}%" if n else "-"
        roi_today += st["roi_today"]
        roi_total += s.get("roi", 0.0)
        lines.append(f"\n*{sym}* {'⏸' if st['paused'] else ''} | {px} | {LEV[sym]}x\n{ptxt}\n"
                     f"Sot: {st['roi_today']:+.2f}% | {st['trades_today']}/{MAX_TRADES_PER_DAY} tregti\n"
                     f"Gjithsej: {s['wins']}W/{s['losses']}L ({wr}) | ROI {s.get('roi', 0.0):+.2f}%")
    lines.append(f"\n📈 Totali: sot {roi_today:+.2f}% ({total_pnl_today():+.3f} USDT) | ROI gjithsej {roi_total:+.2f}%")
    return "\n".join(lines)


def _pick_syms(args):
    chosen = [ALIASES[a] for a in args if a in ALIASES and ALIASES[a] in ACTIVE]
    return chosen or list(ACTIVE), bool(chosen)


def handle_command(text):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]
    args = [p.lower() for p in parts[1:]]
    syms, explicit = _pick_syms(args)
    if cmd == "/backtest":
        d = next((int(a) for a in args if a.isdigit()), 7)
        threading.Thread(target=run_backtest, args=(syms, max(1, min(d, 30))), daemon=True).start()
        return
    with _LOCK:
        if cmd in ("/status", "/stats"):
            tg(status_text())
        elif cmd == "/pause":
            if explicit:
                for s_ in syms:
                    S(s_)["paused"] = True
            else:
                STATE["paused"], STATE["pause_reason"] = True, "manual"
            save_state()
            tg(f"⏸ Pauzë {'për ' + ', '.join(syms) if explicit else '(të dy simbolet)'} — pozicionet ekzistuese menaxhohen normalisht.")
        elif cmd == "/resume":
            if explicit:
                for s_ in syms:
                    S(s_)["paused"], S(s_)["loss_streak"] = False, 0
            else:
                STATE["paused"], STATE["pause_reason"] = False, ""
                for s_ in ACTIVE:
                    S(s_)["paused"], S(s_)["loss_streak"] = False, 0
            save_state()
            tg("▶️ Bot-i rifilloi.")
        elif cmd == "/close":
            open_syms = [s_ for s_ in syms if S(s_)["position"]]
            if not open_syms:
                tg("S'ka pozicion të hapur.")
            for s_ in open_syms:
                market_close(s_, S(s_)["position"]["side"])
                time.sleep(2)
                finish_position(s_, "mbyllje manuale")
        else:
            tg("Komandat: /status /stats /pause /resume /close (+ btc ose xau) | /backtest 7 (+ btc ose xau)")


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


def set_leverage_safe(sym):
    """Vendos levën; nëse Binance e refuzon (mbi maksimumin e simbolit), përdor maksimumin e lejuar."""
    api("POST", "/fapi/v1/marginType", {"symbol": sym, "marginType": "CROSSED"})
    want = LEV[sym]
    lv = api("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": want})
    if is_err(lv) or "leverage" not in lv:
        br = api("GET", "/fapi/v1/leverageBracket", {"symbol": sym})
        try:
            mx = max(int(b["initialLeverage"]) for b in br[0]["brackets"])
            LEV[sym] = min(want, mx)
            lv = api("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": LEV[sym]})
        except Exception:
            pass
    if isinstance(lv, dict) and lv.get("leverage"):
        LEV[sym] = int(lv["leverage"])
    logger.info(f"{sym}: leva {lv}")


def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit("Mungojnë BINANCE_API_KEY / BINANCE_API_SECRET")
    signal.signal(signal.SIGTERM, lambda *a: _STOP.set())
    signal.signal(signal.SIGINT, lambda *a: _STOP.set())
    load_filters()
    if not ACTIVE:
        raise SystemExit("Asnjë simbol i disponueshëm")
    for sym in ACTIVE:
        set_leverage_safe(sym)
    try:
        SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook",
                     data={"drop_pending_updates": "true"}, timeout=TIMEOUT)
    except Exception:
        pass
    tg("🚀 Scalp bot u nis (" + TF + "): " + " | ".join(f"{s} {LEV[s]}x marzh {PROFILES[s]['margin']}$" for s in ACTIVE)
       + "\nKomandat: /status /stats /pause /resume /close /backtest 7")
    for sym in ACTIVE:
        if S(sym)["position"] is None and (position_amt(sym) or 0) != 0:
            tg(f"⚠️ {sym}: ka pozicion të hapur në Binance që bot-i s'e njeh. Mbylle manualisht.")
    save_state()
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
