"""
==============================================================================
QUANT CORE v4.1.0 — SHARED HARDENED ENGINE (TRADFI & CRYPTO DESKS)
Shared by: desk_tradfi.py (XAUUSD, NAS100) & desk_crypto.py (BTCUSD, ETHUSD)
==============================================================================
"""
import os
import sys
import html
import json
import time
import math
import random
import sqlite3
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple, Optional, Any

import requests

try:
    import numpy as np
    import pandas as pd
    HAS_DATA_LIBS = True
except Exception:
    HAS_DATA_LIBS = False
    np = None  # type: ignore
    pd = None  # type: ignore

try:
    import yfinance as yf
    HAS_YF = True
except Exception:
    HAS_YF = False
    yf = None  # type: ignore

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except Exception:
    HAS_MPL = False
    plt = None  # type: ignore

# ==============================================================================
# SHARED CONFIG
# ==============================================================================
DB_PATH = os.getenv("BOT_DB_PATH", "trading_signals_v4_1.db")

TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TWELVEDATA_API_KEY  = os.getenv("TWELVEDATA_API_KEY", "").strip()

NY_TZ  = ZoneInfo("America/New_York")
LDN_TZ = ZoneInfo("Europe/London")

ACCOUNT = {
    "balance":              float(os.getenv("BOT_BALANCE", "10000.0")),
    "risk_pct_scalp":       float(os.getenv("BOT_RISK_SCALP", "0.50")),
    "risk_pct_swing":       float(os.getenv("BOT_RISK_SWING", "0.75")),
    "max_corr_bucket_risk": float(os.getenv("BOT_MAX_BUCKET_RISK", "1.00")),
    "max_inst_risk_pct":    float(os.getenv("BOT_MAX_INST_RISK", "1.00")),
}

GLOBAL = {
    "min_score_signal":      70,
    "donchian_bars":         20,
    "bos_fresh_max_bars":    12,
    "rsi_max_buy":           70.0,
    "rsi_min_sell":          30.0,
    "mad_z_max_abs":         3.0,
    "vwap_z_max_abs":        2.2,
    "max_candle_atr":        2.6,
    "max_dist_ema20_atr":    2.1,
    "max_wick_ratio":        0.42,
    "min_room_swing_atr":    1.15,
    "min_atr_cost_mult":     2.0,
    "max_spread_mult":       2.8,
    "max_cross_div_bps":     35.0,
    "stale_max_min":         {"1m": 10, "5m": 20, "15m": 45, "1h": 180, "4h": 480},
    "closed_stale_extra_min": 5 * 24 * 60,
    "spike_mad_threshold":   9.0,
    "nas_open_delay_min":    15,
    "crypto_weekend_vol":    1.25,
    "spread_pts":   {"XAUUSD": 0.35, "NAS100": 1.5, "BTCUSD": 12.0, "ETHUSD": 1.0},
    "slippage_pts": {"XAUUSD": 0.15, "NAS100": 0.5, "BTCUSD":  5.0, "ETHUSD": 0.4},
    "taker_fee_bps": {"XAUUSD": 0.0, "NAS100": 0.0, "BTCUSD": 5.0, "ETHUSD": 5.0},
    # Walk-Forward & Monte Carlo thresholds
    "bt_risk_pct":           0.005,
    "bt_min_equity":         2000.0,
    "bt_max_dd_stop":        0.30,
    "bt_min_fold_trades":    5,
    "bt_min_profit_factor":  1.05,
    "mc_runs":               500,
    "mc_min_prob_profit":    0.55,
}

TIMEFRAMES = {
    "SCALPING": {
        "htf": "1h", "entry": "15m", "htf_sec": 3600, "entry_sec": 900,
        "period_htf": "60d", "period_entry": "15d",
        "min_bars_htf": 160, "min_bars_entry": 160,
        "sl_atr": 1.8, "trail_atr": 1.25, "be_trigger_r": 1.05, "tp1_r": 1.0,
        "tp_atr": [1.8, 3.0, 4.2], "max_hold_bars": 40,
        "cooldown_same_min": 60, "cooldown_flip_min": 120,
    },
    "SWING": {
        "htf": "4h", "entry": "1h", "htf_sec": 14400, "entry_sec": 3600,
        "period_htf": "90d", "period_entry": "90d",
        "min_bars_htf": 160, "min_bars_entry": 240,
        "sl_atr": 2.2, "trail_atr": 1.45, "be_trigger_r": 1.05, "tp1_r": 1.0,
        "tp_atr": [2.2, 4.0, 5.5], "max_hold_bars": 96,
        "cooldown_same_min": 120, "cooldown_flip_min": 240,
    },
}

COINBASE_GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}

# ==============================================================================
# LOGGING & TIME HELPERS
# ==============================================================================
def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    for noisy in ("yfinance", "urllib3", "peewee"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_utc_iso() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_ts(ts: Optional[str]) -> datetime:
    if not ts:
        return now_utc()
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return now_utc()

def to_epoch_sec(idx: Any) -> Any:
    base = pd.Timestamp("1970-01-01", tz="UTC")
    utc_idx = idx.tz_localize("UTC") if idx.tzinfo is None else idx.tz_convert("UTC")
    return ((utc_idx - base) / pd.Timedelta(seconds=1)).to_numpy(dtype=float)

# ==============================================================================
# DATA FETCHING & SANITIZATION
# ==============================================================================
def flatten_yf(df: Any) -> Any:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def fetch_yf(symbol: str, interval: str, period: str, min_bars: int) -> Optional[Any]:
    if not HAS_YF or not HAS_DATA_LIBS:
        return None
    for attempt in range(2):
        try:
            df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
            if df is None or df.empty:
                time.sleep(0.5)
                continue
            df = flatten_yf(df)
            cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
            df = df[cols].copy()
            if "Volume" not in df.columns:
                df["Volume"] = 0.0
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")
            df = df[~df.index.duplicated(keep="last")].sort_index()
            return df if len(df) >= min_bars else None
        except Exception:
            if attempt == 0:
                time.sleep(0.6)
    return None

def fetch_twelvedata(symbol: str, interval: str, outputsize: int, min_bars: int) -> Optional[Any]:
    if not TWELVEDATA_API_KEY or not HAS_DATA_LIBS:
        return None
    iv_map = {"15m": "15min", "1h": "1h", "4h": "4h"}
    td_iv = iv_map.get(interval)
    if not td_iv:
        return None
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": symbol, "interval": td_iv, "outputsize": min(outputsize, 5000),
                "timezone": "UTC", "apikey": TWELVEDATA_API_KEY,
            },
            timeout=15,
        )
        if not r.ok:
            return None
        values = r.json().get("values")
        if not isinstance(values, list) or len(values) < min_bars:
            return None
        rows = [{
            "ts": pd.Timestamp(v["datetime"], tz="UTC"),
            "Open": float(v["open"]), "High": float(v["high"]),
            "Low": float(v["low"]), "Close": float(v["close"]),
            "Volume": float(v.get("volume") or 0.0),
        } for v in values]
        df = pd.DataFrame(rows).set_index("ts").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df if len(df) >= min_bars else None
    except Exception:
        return None

def fetch_coinbase(product: str, granularity: int, min_bars: int, max_batches: int = 2) -> Optional[Any]:
    if not HAS_DATA_LIBS:
        return None
    try:
        all_rows = []
        end_dt = now_utc()
        for b in range(max_batches):
            if b > 0:
                time.sleep(0.15 if max_batches > 4 else 0.05)
            start_dt = pd.Timestamp(end_dt) - pd.Timedelta(seconds=granularity * 290)
            url = (
                f"https://api.exchange.coinbase.com/products/{product}/candles"
                f"?granularity={granularity}"
                f"&start={start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                f"&end={end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            )
            data = None
            delay = 0.6
            for _ in range(3):
                try:
                    r = requests.get(url, timeout=20, headers={"User-Agent": "quantdesk-v4.1"})
                    if r.status_code == 429:
                        time.sleep(delay)
                        delay = min(delay * 2, 5.0)
                        continue
                    r.raise_for_status()
                    data = r.json()
                    break
                except Exception:
                    time.sleep(delay)
                    delay = min(delay * 2, 4.0)
            if isinstance(data, list) and data:
                all_rows.extend(data)
            else:
                break
            end_dt = start_dt.to_pydatetime()
            if max_batches <= 2 and len(all_rows) >= min_bars + 60:
                break
        if not all_rows:
            return None
        arr = sorted({row[0]: row for row in all_rows}.values(), key=lambda x: x[0])
        df = pd.DataFrame(arr, columns=["ts", "Low", "High", "Open", "Close", "Volume"])
        for c in ["Low", "High", "Open", "Close", "Volume"]:
            df[c] = df[c].astype(float)
        df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        return df if len(df) >= min_bars else None
    except Exception:
        return None

def sanitize_and_drop_unclosed(df: Any, interval: str, bar_sec: int,
                               min_bars: int, instrument: str,
                               is_market_open_fn: Any, check_stale: bool = True) -> Optional[Any]:
    if df is None or df.empty:
        return None
    now_ts = pd.Timestamp.now(tz="UTC")
    last_open = df.index[-1]
    if (now_ts - last_open).total_seconds() < bar_sec:
        df = df.iloc[:-1].copy()
    if len(df) < min_bars:
        return None
    if check_stale:
        last_close = df.index[-1] + pd.Timedelta(seconds=bar_sec)
        age_min = (now_ts - last_close).total_seconds() / 60.0
        limit = GLOBAL["stale_max_min"].get(interval, 150)
        if not is_market_open_fn(instrument, for_monitoring=True):
            limit = max(limit, GLOBAL["closed_stale_extra_min"])
        if age_min > limit:
            logging.info(f"[sanitize] {instrument} {interval} stale closed age={age_min:.0f}m > {limit}m")
            return None
    return df

def resample_ohlcv(df: Any, rule_sec: int) -> Optional[Any]:
    if df is None or df.empty:
        return None
    freq = "4h" if rule_sec == 14400 else f"{rule_sec}s"
    agg = df.resample(freq, origin="epoch").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna(subset=["Open", "High", "Low", "Close"])
    return agg if not agg.empty else None

def fetch_pair_for_desk(instruments_map: Dict[str, Any], instrument: str, mode: str,
                        is_market_open_fn: Any, history: bool = False,
                        preferred_source: Optional[str] = None
                        ) -> Tuple[Optional[Any], Optional[Any], Optional[str]]:
    tf = TIMEFRAMES[mode]
    groups = list(instruments_map[instrument]["source_groups"])
    if preferred_source:
        pref_name = preferred_source.split(":")[0]
        matched = [g for g in groups if g["name"] == pref_name]
        if matched:
            groups = matched
    elif history:
        groups = sorted(groups, key=lambda g: 1 if g["type"] == "twelvedata" else 0)

    check_stale = not history
    for src in groups:
        df_htf = df_ent = None
        if mode == "SWING":
            min_ent = 500 if history else tf["min_bars_entry"]
            if src["type"] == "twelvedata":
                df_ent = fetch_twelvedata(src["symbol"], "1h", 1800 if history else 400, min_ent)
            elif src["type"] == "yfinance":
                df_ent = fetch_yf(src["symbol"], "1h", "730d" if history else tf["period_entry"], min_ent)
            elif src["type"] == "coinbase":
                df_ent = fetch_coinbase(src["symbol"], 3600, min_ent, max_batches=20 if history else 4)
            df_ent = sanitize_and_drop_unclosed(df_ent, tf["entry"], tf["entry_sec"], min_ent, instrument, is_market_open_fn, check_stale)
            if df_ent is not None:
                df_htf = resample_ohlcv(df_ent, tf["htf_sec"])
                df_htf = sanitize_and_drop_unclosed(df_htf, tf["htf"], tf["htf_sec"], tf["min_bars_htf"], instrument, is_market_open_fn, check_stale)
        else:
            min_ent = 450 if history else tf["min_bars_entry"]
            min_htf = 220 if history else tf["min_bars_htf"]
            if src["type"] == "twelvedata":
                df_htf = fetch_twelvedata(src["symbol"], tf["htf"], 800 if history else 300, min_htf)
                df_ent = fetch_twelvedata(src["symbol"], tf["entry"], 2000 if history else 350, min_ent)
            elif src["type"] == "yfinance":
                df_htf = fetch_yf(src["symbol"], tf["htf"], "730d" if history else tf["period_htf"], min_htf)
                df_ent = fetch_yf(src["symbol"], tf["entry"], "59d" if history else tf["period_entry"], min_ent)
            elif src["type"] == "coinbase":
                g_htf, g_ent = COINBASE_GRAN.get(tf["htf"]), COINBASE_GRAN.get(tf["entry"])
                if g_htf and g_ent:
                    df_htf = fetch_coinbase(src["symbol"], g_htf, min_htf, max_batches=8 if history else 2)
                    df_ent = fetch_coinbase(src["symbol"], g_ent, min_ent, max_batches=20 if history else 2)
            df_htf = sanitize_and_drop_unclosed(df_htf, tf["htf"], tf["htf_sec"], min_htf, instrument, is_market_open_fn, check_stale)
            df_ent = sanitize_and_drop_unclosed(df_ent, tf["entry"], tf["entry_sec"], min_ent, instrument, is_market_open_fn, check_stale)

        if df_htf is not None and df_ent is not None:
            mul, off = src.get("multiplier", 1.0), src.get("offset", 0.0)
            if mul != 1.0 or off != 0.0:
                for df_x in (df_htf, df_ent):
                    for col in ["Open", "High", "Low", "Close"]:
                        df_x[col] = df_x[col] * mul + off
            return df_htf, df_ent, f"{src['name']}:{src['symbol']}"
    return None, None, None

# ==============================================================================
# WILDER INDICATORS + VWAP + SQUEEZE + CAUSAL SWINGS
# ==============================================================================
def compute_indicators(df: Any, vwap_win: int = 24) -> Any:
    df = df.copy()
    close, high, low = df["Close"], df["High"], df["Low"]
    vol = df["Volume"].where(df["Volume"] > 0, 1.0)

    df["ema20"]  = close.ewm(span=20,  adjust=False).mean()
    df["ema50"]  = close.ewm(span=50,  adjust=False).mean()
    df["ema200"] = close.ewm(span=200, min_periods=80, adjust=False).mean()

    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()

    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()
    df["rsi14"] = 100.0 - (100.0 / (1.0 + gain / (loss + 1e-10)))

    up_move, down_move = high.diff(), -low.diff()
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    rma_tr   = tr.ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()
    plus_di  = 100.0 * pd.Series(plus_dm,  index=df.index).ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean() / (rma_tr + 1e-10)
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean() / (rma_tr + 1e-10)
    dx       = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    df["plus_di"], df["minus_di"] = plus_di, minus_di
    df["adx14"] = dx.ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()

    net_move = (close - close.shift(20)).abs()
    path_sum = close.diff().abs().rolling(20).sum()
    df["er20"] = (net_move / (path_sum + 1e-10)).fillna(0.0)
    df["atr_pct_rank"] = df["atr14"].rolling(100, min_periods=30).apply(
        lambda s: float(np.mean(s <= s[-1])), raw=True
    ).fillna(0.5)

    med20 = close.shift(1).rolling(20).median()
    mad20 = close.shift(1).rolling(20).apply(lambda s: float(np.median(np.abs(s - np.median(s)))), raw=True)
    df["mad_zscore20"] = (close - med20) / (mad20 * 1.4826 + 1e-9)
    df["slope20"]      = df["ema20"].diff(5) / 5.0

    vol_med = df["Volume"].shift(1).rolling(20, min_periods=10).median()
    df["vol_ratio"] = np.where(vol_med > 0, df["Volume"] / (vol_med + 1e-9), 1.0)

    tp = (high + low + close) / 3.0
    pv_sum = (tp * vol).rolling(vwap_win, min_periods=1).sum()
    v_sum  = vol.rolling(vwap_win, min_periods=1).sum()
    vwap   = pv_sum / (v_sum + 1e-9)
    vwap_var = ((tp - vwap) ** 2 * vol).rolling(vwap_win, min_periods=1).sum() / (v_sum + 1e-9)
    vwap_std = np.sqrt(vwap_var.clip(lower=0.0))
    df["vwap"]   = vwap
    df["vwap_z"] = np.where(vwap_std > 1e-9, (close - vwap) / vwap_std, 0.0)

    bb_std = close.rolling(20, min_periods=20).std(ddof=0)
    kc_w   = 3.0 * df["atr14"]
    df["squeeze_ratio"] = np.where(kc_w > 1e-9, (4.0 * bb_std) / kc_w, 1.0)
    df["squeeze_ratio"] = df["squeeze_ratio"].fillna(1.0)
    return df

def extract_arrays(df: Any) -> Dict[str, Any]:
    cols = (
        "Open", "High", "Low", "Close", "Volume",
        "ema20", "ema50", "ema200", "atr14", "rsi14",
        "plus_di", "minus_di", "adx14", "er20",
        "atr_pct_rank", "mad_zscore20", "slope20", "vol_ratio",
        "vwap", "vwap_z", "squeeze_ratio",
    )
    return {c: df[c].to_numpy(dtype=float) for c in cols}

def extract_causal_swings(df: Any, lookback: int = 5) -> Tuple[Any, Any]:
    n = len(df)
    highs, lows = df["High"].to_numpy(dtype=float), df["Low"].to_numpy(dtype=float)
    last_sh, last_sl = np.full(n, np.nan), np.full(n, np.nan)
    cur_h = cur_l = np.nan
    for i in range(n):
        k = i - lookback
        if k >= lookback:
            if highs[k] == np.max(highs[k - lookback : k + lookback + 1]):
                cur_h = highs[k]
            if lows[k] == np.min(lows[k - lookback : k + lookback + 1]):
                cur_l = lows[k]
        last_sh[i], last_sl[i] = cur_h, cur_l
    return last_sh, last_sl

def align_htf_idx(htf_close_ts: Any, ent_close_ts: float) -> int:
    return int(np.searchsorted(htf_close_ts, ent_close_ts, side="right")) - 1

def detect_regime_vals(adx: float, er: float, atr: float, rk: float, e20: float, e50: float) -> str:
    if not (math.isfinite(atr) and atr > 0):
        return "UNKNOWN"
    if rk < 0.12 and er < 0.24 and adx < 19.0:
        return "CHOPPY"
    ema_sep_atr = abs(e20 - e50) / max(atr, 1e-9)
    if (
        (adx >= 21.0 and er >= 0.22)
        or (adx >= 26.0 and er >= 0.15 and ema_sep_atr >= 0.35)
        or (er >= 0.35 and ema_sep_atr >= 0.45)
    ):
        return "TREND"
    if adx < 18.0 and er < 0.22:
        return "CHOPPY"
    return "RANGE"

def spike_ok_arr(closes: Any, idx: int) -> bool:
    if idx < 25:
        return True
    win = closes[idx - 24 : idx + 1]
    rets = np.diff(win) / np.maximum(win[:-1], 1e-9)
    base, cur = rets[:-1], float(rets[-1])
    med = float(np.median(base))
    mad = float(np.median(np.abs(base - med)))
    return abs(cur - med) / max(mad * 1.4826, 1e-6) < GLOBAL["spike_mad_threshold"]

# ==============================================================================
# HARDENED v4.1 SIGNAL ENGINE (SINGLE SOURCE OF TRUTH FOR LIVE & BACKTEST)
# ==============================================================================
def _fresh_bos_bars_ago(htf_a: Dict[str, Any], htf_sh: Any, htf_sl: Any,
                        htf_idx: int, direction: str) -> Optional[int]:
    def is_broken_at(idx: int, d: str) -> bool:
        sw = htf_sh[idx] if d == "BUY" else htf_sl[idx]
        if not math.isfinite(sw):
            return False
        c, o = float(htf_a["Close"][idx]), float(htf_a["Open"][idx])
        rng = max(float(htf_a["High"][idx]) - float(htf_a["Low"][idx]), 1e-9)
        if abs(c - o) / rng < 0.25:
            return False
        return (c > sw * (1.0 + 1e-5) and c > o) if d == "BUY" else (c < sw * (1.0 - 1e-5) and c < o)

    opp_dir = "SELL" if direction == "BUY" else "BUY"
    max_look = min(int(GLOBAL["bos_fresh_max_bars"]), htf_idx - 1)
    for b in range(0, max_look + 1):
        k = htf_idx - b
        if is_broken_at(k, opp_dir):
            return None
        if is_broken_at(k, direction):
            prev_sw = htf_sh[k - 1] if direction == "BUY" else htf_sl[k - 1]
            if not math.isfinite(prev_sw) or not is_broken_at(k - 1, direction):
                return b
    return None

def evaluate_bar_pair_arrays(
    htf_a: Dict[str, Any], htf_sh: Any, htf_sl: Any, htf_idx: int,
    ent_a: Dict[str, Any], ent_idx: int, threshold: int,
    instrument: Optional[str] = None,
    live_spread_pts: Optional[float] = None,
    min_vol_ratio: float = 1.10,
) -> Tuple[Optional[Tuple[str, int, str, str, Dict[str, float]]], str]:
    don_n = int(GLOBAL["donchian_bars"])
    if htf_idx < 80 or ent_idx < max(60, don_n + 1):
        return None, "warmup<80"
    if not (spike_ok_arr(htf_a["Close"], htf_idx) and spike_ok_arr(ent_a["Close"], ent_idx)):
        return None, "spike_guard"
    if not (math.isfinite(htf_sh[htf_idx]) and math.isfinite(htf_sl[htf_idx])):
        return None, "awaiting_swing"

    atr   = float(ent_a["atr14"][ent_idx])
    he200 = float(htf_a["ema200"][htf_idx])
    ec    = float(ent_a["Close"][ent_idx])
    if not math.isfinite(atr) or atr <= 0 or not math.isfinite(he200):
        return None, "invalid_atr_or_ema200"

    if instrument is not None:
        base_sp = float(GLOBAL["spread_pts"].get(instrument, 0.0))
        sp = float(live_spread_pts) if live_spread_pts is not None else base_sp
        if base_sp > 0 and sp > float(GLOBAL["max_spread_mult"]) * base_sp:
            return None, f"spread_spike ({sp:.2f}>{GLOBAL['max_spread_mult']*base_sp:.2f})"
        fee_bps = float(GLOBAL["taker_fee_bps"].get(instrument, 0.0))
        fee_pts = ec * fee_bps / 10000.0
        rt_cost = sp + 2.0 * float(GLOBAL["slippage_pts"].get(instrument, 0.0)) + 2.0 * fee_pts
        if rt_cost > 0 and atr < float(GLOBAL["min_atr_cost_mult"]) * rt_cost:
            return None, f"low_atr_vs_cost ({atr:.2f}<{GLOBAL['min_atr_cost_mult']*rt_cost:.2f})"

    adx     = float(htf_a["adx14"][htf_idx])
    er      = float(htf_a["er20"][htf_idx])
    htf_atr = float(htf_a["atr14"][htf_idx])
    htf_rk  = float(htf_a["atr_pct_rank"][htf_idx])
    he20    = float(htf_a["ema20"][htf_idx])
    he50    = float(htf_a["ema50"][htf_idx])
    hc      = float(htf_a["Close"][htf_idx])

    regime = detect_regime_vals(adx, er, htf_atr, htf_rk, he20, he50)
    if regime in ("CHOPPY", "UNKNOWN"):
        return None, f"regime={regime} (skip chop)"

    htf_trend = 1 if he20 > he50 else -1
    eo = float(ent_a["Open"][ent_idx])
    eh = float(ent_a["High"][ent_idx])
    el = float(ent_a["Low"][ent_idx])
    prev_c = float(ent_a["Close"][ent_idx - 1])

    c_rng = max(eh - el, 1e-9)
    if c_rng > float(GLOBAL["max_candle_atr"]) * atr:
        return None, f"climax_candle ({c_rng/atr:.1f}xATR)"
    close_pos  = (ec - el) / c_rng
    body_ratio = abs(ec - eo) / c_rng
    up_wick_r  = (eh - max(ec, eo)) / c_rng
    lo_wick_r  = (min(ec, eo) - el) / c_rng

    ee20  = float(ent_a["ema20"][ent_idx])
    ee50  = float(ent_a["ema50"][ent_idx])
    slope = float(ent_a["slope20"][ent_idx])
    rsi   = float(ent_a["rsi14"][ent_idx])
    mad_z = float(ent_a["mad_zscore20"][ent_idx]) if math.isfinite(ent_a["mad_zscore20"][ent_idx]) else 0.0
    vwap  = float(ent_a["vwap"][ent_idx]) if math.isfinite(ent_a["vwap"][ent_idx]) else ec
    vwap_z = float(ent_a["vwap_z"][ent_idx]) if math.isfinite(ent_a["vwap_z"][ent_idx]) else 0.0
    sq_ratio = float(ent_a["squeeze_ratio"][ent_idx])
    pdi   = float(htf_a["plus_di"][htf_idx])
    mdi   = float(htf_a["minus_di"][htf_idx])
    ent_atr_rk = float(ent_a["atr_pct_rank"][ent_idx])
    dist_e20_atr = abs(ec - ee20) / max(atr, 1e-9)
    vol_ratio = float(ent_a["vol_ratio"][ent_idx])
    has_vol = float(ent_a["Volume"][ent_idx]) > 1.5

    hi20 = float(np.max(ent_a["High"][ent_idx - don_n : ent_idx]))
    lo20 = float(np.min(ent_a["Low"][ent_idx - don_n : ent_idx]))
    vol_ok = (not has_vol) or (vol_ratio >= min_vol_ratio)
    brk_qual_ok = (regime == "TREND") or (adx >= 20.5 and ent_atr_rk >= 0.35 and sq_ratio >= 0.90 and vol_ok)

    max_wick = float(GLOBAL["max_wick_ratio"])
    bull_brk = (
        brk_qual_ok and (ec > hi20) and (prev_c <= hi20)
        and (close_pos >= 0.60) and (body_ratio >= 0.38)
        and (up_wick_r <= max_wick)
        and (dist_e20_atr <= float(GLOBAL["max_dist_ema20_atr"]))
    )
    bear_brk = (
        brk_qual_ok and (ec < lo20) and (prev_c >= lo20)
        and (close_pos <= 0.40) and (body_ratio >= 0.38)
        and (lo_wick_r <= max_wick)
        and (dist_e20_atr <= float(GLOBAL["max_dist_ema20_atr"]))
    )

    bull_pb = (
        regime == "TREND" and htf_trend == 1 and hc > he50 and he50 > he200 and pdi > mdi
        and ee20 > ee50 and slope > 0 and el <= ee20 + 0.25 * atr
        and ec > ee20 and ec > eo and close_pos >= 0.62 and body_ratio >= 0.40
        and (up_wick_r <= max_wick) and 42.0 <= rsi <= 65.0
    )
    bear_pb = (
        regime == "TREND" and htf_trend == -1 and hc < he50 and he50 < he200 and mdi > pdi
        and ee20 < ee50 and slope < 0 and eh >= ee20 - 0.25 * atr
        and ec < ee20 and ec < eo and close_pos <= 0.38 and body_ratio >= 0.40
        and (lo_wick_r <= max_wick) and 35.0 <= rsi <= 58.0
    )

    bos_buy  = _fresh_bos_bars_ago(htf_a, htf_sh, htf_sl, htf_idx, "BUY")
    bos_sell = _fresh_bos_bars_ago(htf_a, htf_sh, htf_sl, htf_idx, "SELL")

    sb = ss = 0
    if htf_trend == 1 and hc > he50: sb += 18
    elif htf_trend == 1:            sb += 8
    if htf_trend == -1 and hc < he50: ss += 18
    elif htf_trend == -1:             ss += 8

    if hc > he200 and he50 > he200: sb += 18
    elif hc > he200:               sb += 10
    if hc < he200 and he50 < he200: ss += 18
    elif hc < he200:               ss += 10

    if bull_brk or bull_pb: sb += 24
    if bear_brk or bear_pb: ss += 24

    if bos_buy is not None:  sb += 10 if bos_buy <= 6 else 6
    if bos_sell is not None: ss += 10 if bos_sell <= 6 else 6

    if pdi > mdi: sb += 12 if regime == "TREND" else 6
    if mdi > pdi: ss += 12 if regime == "TREND" else 6

    if ec > ee20 and slope > 0: sb += 8
    if ec < ee20 and slope < 0: ss += 8

    if ec >= vwap and vwap_z <= 1.8:  sb += 5
    if ec <= vwap and vwap_z >= -1.8: ss += 5

    if (bull_brk or bull_pb) and has_vol and vol_ratio >= 1.25: sb += 5
    if (bear_brk or bear_pb) and has_vol and vol_ratio >= 1.25: ss += 5

    diag = f"regime={regime} adx={adx:.1f} er={er:.2f} rsi={rsi:.1f} vwap_z={vwap_z:+.1f} sb={sb} ss={ss}"

    if htf_trend == 1 and sb >= threshold and sb >= ss + 10:
        direction, score = "BUY", sb
    elif htf_trend == -1 and ss >= threshold and ss >= sb + 10:
        direction, score = "SELL", ss
    else:
        return None, f"{diag} (score<{threshold})"

    setup_name = ("BREAKOUT" if bull_brk else ("PULLBACK" if bull_pb else None)) if direction == "BUY" else ("BREAKOUT" if bear_brk else ("PULLBACK" if bear_pb else None))
    if setup_name is None:
        return None, f"{diag} (no breakout/pullback)"
    if (direction == "BUY" and (hc <= he200 or hc <= he50)) or (direction == "SELL" and (hc >= he200 or hc >= he50)):
        return None, f"{diag} (counter-HTF EMA50/200)"
    if direction == "BUY" and (rsi > GLOBAL["rsi_max_buy"] or mad_z > GLOBAL["mad_z_max_abs"] or vwap_z > GLOBAL["vwap_z_max_abs"]):
        return None, f"{diag} (overextended BUY)"
    if direction == "SELL" and (rsi < GLOBAL["rsi_min_sell"] or mad_z < -GLOBAL["mad_z_max_abs"] or vwap_z < -GLOBAL["vwap_z_max_abs"]):
        return None, f"{diag} (overextended SELL)"

    if direction == "BUY" and htf_sh[htf_idx] > ec:
        room = (float(htf_sh[htf_idx]) - ec) / max(atr, 1e-9)
        if room < float(GLOBAL["min_room_swing_atr"]):
            return None, f"{diag} (blocked_by_htf_sh {room:.2f}xATR)"
    if direction == "SELL" and htf_sl[htf_idx] < ec:
        room = (ec - float(htf_sl[htf_idx])) / max(atr, 1e-9)
        if room < float(GLOBAL["min_room_swing_atr"]):
            return None, f"{diag} (blocked_by_htf_sl {room:.2f}xATR)"

    return (direction, int(min(100, score)), regime, setup_name, {
        "atr": atr, "rsi": rsi, "mad_z": mad_z, "vwap_z": vwap_z, "adx": adx, "er": er, "entry": ec,
    }), diag

def calc_lot(balance: float, risk_pct: float, sl_dist: float,
             contract: Dict[str, Any]) -> Tuple[float, float, bool]:
    risk_amt = balance * (risk_pct / 100.0)
    if not math.isfinite(sl_dist) or sl_dist <= 0:
        return float(contract["min_lot"]), round(risk_amt, 2), True
    raw = risk_amt / (sl_dist * float(contract["pip_value_per_lot"]))
    step = float(contract["lot_step"])
    lot = math.floor(raw / step) * step
    lot = max(float(contract["min_lot"]), min(float(contract["max_lot"]), lot))
    actual_risk = lot * sl_dist * float(contract["pip_value_per_lot"])
    warn = raw < float(contract["min_lot"]) and actual_risk > risk_amt * 1.25
    return round(lot, 2), round(actual_risk, 2), warn

def load_calibrated_params(params_path: str) -> Dict[str, Any]:
    if os.path.exists(params_path):
        try:
            with open(params_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}

# ==============================================================================
# FAST BACKTESTER + WALK-FORWARD + MONTE CARLO (WITH DD95)
# ==============================================================================
def backtest_mtf_fast(
    ent_a: Dict[str, Any], ent_open_ts: Any, sig_cache: List[Optional[Tuple[str, float]]],
    instrument: str, mode: str, sl_mult: float, tp_target_mult: float,
    min_trades: int, start_idx: int = 80, end_idx: Optional[int] = None,
) -> Dict[str, Any]:
    tf = TIMEFRAMES[mode]
    spread = float(GLOBAL["spread_pts"].get(instrument, 1.0))
    slip   = float(GLOBAL["slippage_pts"].get(instrument, 0.1))
    fee_r  = float(GLOBAL["taker_fee_bps"].get(instrument, 0.0)) / 10000.0
    risk_frac    = float(GLOBAL["bt_risk_pct"])
    trail_mult   = float(tf["trail_atr"])
    be_trigger_r = float(tf["be_trigger_r"])
    tp1_r        = float(tf["tp1_r"])
    max_hold     = int(tf["max_hold_bars"])
    cd_same      = float(tf["cooldown_same_min"])
    cd_flip      = float(tf["cooldown_flip_min"])

    opens, highs, lows, closes = ent_a["Open"], ent_a["High"], ent_a["Low"], ent_a["Close"]
    equity = peak = 10000.0
    curve, trades_pnl, trades_r = [equity], [], []
    last_exit_ts, last_dir = 0.0, None
    i = max(80, int(start_idx))
    n_ent = min(len(opens), int(end_idx)) if end_idx is not None else len(opens)

    while i < n_ent - 2:
        if equity < GLOBAL["bt_min_equity"] or (peak - equity) / max(peak, 1e-9) > GLOBAL["bt_max_dd_stop"]:
            break
        sig = sig_cache[i]
        if sig is None:
            i += 1
            continue
        direction, atr = sig
        if last_dir is not None:
            cd_min = cd_same if last_dir == direction else cd_flip
            if (ent_open_ts[i] - last_exit_ts) / 60.0 < cd_min:
                i += 1
                continue

        enter_idx = i + 1
        next_open = float(opens[enter_idx])
        sl_dist, trail_dist = sl_mult * atr, trail_mult * atr
        fill = (next_open + spread / 2.0 + slip) if direction == "BUY" else (next_open - spread / 2.0 - slip)
        sl_orig = (fill - sl_dist) if direction == "BUY" else (fill + sl_dist)
        tp1_px  = (fill + tp1_r * sl_dist) if direction == "BUY" else (fill - tp1_r * sl_dist)
        tp2_tgt = (fill + tp_target_mult * atr) if direction == "BUY" else (fill - tp_target_mult * atr)

        active_sl, tp1_hit, rem_frac, realized_gross, max_fav = sl_orig, False, 1.0, 0.0, fill
        risk_amt = max(equity * risk_frac, 1e-9)
        units = risk_amt / max(sl_dist, 1e-9)
        max_j = min(enter_idx + max_hold, n_ent - 1)
        exit_px, exit_j = float(closes[max_j]), max_j

        for j in range(enter_idx, max_j + 1):
            o, h, l = float(opens[j]), float(highs[j]), float(lows[j])
            if direction == "BUY":
                bo, bl, bh = o - spread / 2.0, l - spread / 2.0, h - spread / 2.0
                if bo <= active_sl: exit_px, exit_j = bo - slip, j; break
                if bl <= active_sl: exit_px, exit_j = active_sl - slip, j; break
                if not tp1_hit and bh >= tp1_px:
                    tp1_hit = True
                    realized_gross += (tp1_px - fill) * (units * 0.5)
                    rem_frac = 0.5
                    active_sl = max(active_sl, fill + 0.08 * sl_dist)
                if bh >= tp2_tgt: exit_px, exit_j = tp2_tgt, j; break
                if bh > max_fav: max_fav = bh
                if (max_fav - fill) >= be_trigger_r * sl_dist:
                    active_sl = max(active_sl, fill + 0.08 * sl_dist, max_fav - trail_dist)
            else:
                ao, ah, al = o + spread / 2.0, h + spread / 2.0, l + spread / 2.0
                if ao >= active_sl: exit_px, exit_j = ao + slip, j; break
                if ah >= active_sl: exit_px, exit_j = active_sl + slip, j; break
                if not tp1_hit and al <= tp1_px:
                    tp1_hit = True
                    realized_gross += (fill - tp1_px) * (units * 0.5)
                    rem_frac = 0.5
                    active_sl = min(active_sl, fill - 0.08 * sl_dist)
                if al <= tp2_tgt: exit_px, exit_j = tp2_tgt, j; break
                if al < max_fav: max_fav = al
                if (fill - max_fav) >= be_trigger_r * sl_dist:
                    active_sl = min(active_sl, fill - 0.08 * sl_dist, max_fav + trail_dist)
            if j == max_j:
                c = float(closes[j])
                exit_px = (c - spread / 2.0 - slip) if direction == "BUY" else (c + spread / 2.0 + slip)

        tail_gross = ((exit_px - fill) if direction == "BUY" else (fill - exit_px)) * (units * rem_frac)
        fee_cost = (fill + exit_px) * units * fee_r
        pnl = realized_gross + tail_gross - fee_cost
        equity += pnl
        peak = max(peak, equity)
        curve.append(equity)
        trades_pnl.append(pnl)
        trades_r.append(pnl / risk_amt)
        last_exit_ts = float(ent_open_ts[exit_j])
        last_dir = direction
        i = exit_j + 1

    n_tr = len(trades_pnl)
    wins = sum(1 for p in trades_pnl if p > 0)
    wr   = (wins / n_tr) if n_tr else 0.0
    gw   = sum(p for p in trades_pnl if p > 0)
    gl   = abs(sum(p for p in trades_pnl if p < 0))
    pf   = (gw / (gl + 1e-8)) if n_tr else 0.0

    pk, mdd = curve[0], 0.0
    for v in curve:
        pk = max(pk, v)
        mdd = max(mdd, (pk - v) / max(pk, 1e-9))
    exp_r = float(sum(trades_r) / n_tr) if n_tr > 0 else 0.0
    std_r = float(np.std(trades_r, ddof=1)) if n_tr > 1 else 1.0
    sqn   = (math.sqrt(n_tr) * exp_r / std_r) if (n_tr >= 2 and std_r > 1e-9) else 0.0
    if n_tr < min_trades:
        return {"score": -1e9, "trades": n_tr, "winrate": wr, "profit_factor": pf,
                "max_dd": mdd, "sqn": sqn, "equity_curve": curve, "trades_r": trades_r}
    score = exp_r * wr * math.log1p(pf) * (1.0 - mdd) * min(1.0, math.sqrt(n_tr / 40.0))
    return {"score": score, "trades": n_tr, "winrate": wr, "profit_factor": pf,
            "max_dd": mdd, "sqn": sqn, "equity_curve": curve, "trades_r": trades_r}

def monte_carlo_stats(trades_r: List[float], runs: int = 500, risk_frac: float = 0.005) -> Tuple[float, float]:
    n = len(trades_r)
    if n < 5:
        return 0.0, 1.0
    rng = random.Random(20261125)
    profitable = 0
    dds: List[float] = []
    for _ in range(runs):
        eq = pk = 10000.0
        mdd = 0.0
        for _ in range(n):
            eq += eq * risk_frac * rng.choice(trades_r)
            pk = max(pk, eq)
            mdd = max(mdd, (pk - eq) / max(pk, 1e-9))
        if eq > 10000.0:
            profitable += 1
        dds.append(mdd)
    dds.sort()
    dd95 = dds[int(0.95 * (len(dds) - 1))]
    return profitable / float(runs), dd95

# ==============================================================================
# SQLITE SCHEMA & THREAD-SAFE RISK GATES
# ==============================================================================
CANONICAL_DDL = {
    "signals": """
        CREATE TABLE {tbl} (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, bar_ts TEXT,
            desk TEXT DEFAULT 'unified', instrument TEXT NOT NULL, mode TEXT NOT NULL,
            direction TEXT NOT NULL, entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
            lot REAL, risk_usd REAL, score INTEGER, setup TEXT, regime TEXT,
            source TEXT, message TEXT, notified INTEGER DEFAULT 0
        )
    """,
    "open_trades": """
        CREATE TABLE {tbl} (
            id INTEGER PRIMARY KEY AUTOINCREMENT, opened_ts TEXT NOT NULL,
            desk TEXT DEFAULT 'unified', instrument TEXT NOT NULL, mode TEXT NOT NULL,
            direction TEXT NOT NULL, entry REAL NOT NULL, initial_sl REAL NOT NULL,
            sl REAL NOT NULL, tp1 REAL NOT NULL, tp2 REAL NOT NULL, tp3 REAL NOT NULL,
            lot REAL NOT NULL, risk_usd REAL DEFAULT 0, be_active INTEGER DEFAULT 0,
            tp1_hit INTEGER DEFAULT 0, status TEXT DEFAULT 'OPEN', closed_ts TEXT,
            exit_price REAL, r_result REAL, trail_dist REAL DEFAULT 0,
            last_bar_ts TEXT, bars_held INTEGER DEFAULT 0, source TEXT
        )
    """,
    "cooldown": """
        CREATE TABLE {tbl} (
            instrument TEXT NOT NULL, mode TEXT NOT NULL, direction TEXT NOT NULL,
            last_ts TEXT NOT NULL, last_bar_ts TEXT, PRIMARY KEY (instrument, mode)
        )
    """,
    "state": "CREATE TABLE {tbl} (key TEXT PRIMARY KEY, value TEXT)",
}

def db_init(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=8000;")
    for tbl, ddl in CANONICAL_DDL.items():
        conn.execute(ddl.format(tbl=f"IF NOT EXISTS {tbl}"))
    for tbl in ("signals", "open_trades"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
        if "desk" not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN desk TEXT DEFAULT 'unified'")
        if tbl == "open_trades" and "tp1_hit" not in cols:
            conn.execute("ALTER TABLE open_trades ADD COLUMN tp1_hit INTEGER DEFAULT 0")
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_signals_inst_id ON signals(instrument, id DESC);
        CREATE INDEX IF NOT EXISTS idx_signals_bar ON signals(instrument, mode, bar_ts);
        CREATE INDEX IF NOT EXISTS idx_open_trades_status ON open_trades(status, instrument, mode);
    """)
    conn.commit()

# ==============================================================================
# NOTIFICATIONS & LIVE MONITOR (FEE-PARITY R-ACCOUNTING)
# ==============================================================================
def esc(val: Any) -> str:
    return html.escape(str(val), quote=False)

def send_telegram(text: str, chat_id: Optional[str] = None, retries: int = 4) -> bool:
    if not TELEGRAM_BOT_TOKEN or not (chat_id or TELEGRAM_CHAT_ID):
        return False
    url, delay = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 1.0
    cid = chat_id or TELEGRAM_CHAT_ID
    for _ in range(retries):
        try:
            r = requests.post(url, json={"chat_id": cid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
            if r.ok:
                return True
            if r.status_code == 429:
                time.sleep(float(r.json().get("parameters", {}).get("retry_after", delay)))
                continue
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 20.0)
    return False

def send_discord(text: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        return False
    plain = text.replace("<b>", "**").replace("</b>", "**").replace("<code>", chr(96)).replace("</code>", chr(96))
    try:
        return requests.post(DISCORD_WEBHOOK_URL, json={"content": plain}, timeout=20).ok
    except Exception:
        return False

def notify_all(msg: str) -> Tuple[bool, bool]:
    return send_telegram(msg), send_discord(msg)

def format_signal_message(sig: Dict[str, Any]) -> str:
    badge = "🟢" if sig["direction"] == "BUY" else "🔴"
    warn_txt = " ⚠️" if sig["risk_warn"] else ""
    desk_tag = sig.get("desk", "DESK").upper()
    return (
        f"{badge} <b>[{esc(desk_tag)}] {esc(sig['instrument'])} | {esc(sig['mode'])} | {esc(sig['direction'])}</b>\n"
        f"Setup: <code>{esc(sig['setup'])}</code> ({esc(sig['regime'])}) | Skor: <code>{sig['score']}/100</code>\n"
        f"Entry: <code>{sig['entry']:.2f}</code> | SL: <code>{sig['sl']:.2f}</code>\n"
        f"TP1 (50%): <code>{sig['tp1']:.2f}</code> | TP2: <code>{sig['tp2']:.2f}</code> | TP3: <code>{sig['tp3']:.2f}</code>\n"
        f"Lot: <code>{sig['lot']:.2f}</code>{warn_txt} (${sig['risk_usd']:.0f}) | "
        f"ADX {sig['adx']:.1f} | RSI {sig['rsi']:.0f} | <code>{esc(sig['bar_ts'][11:16])} UTC</code>"
    )

def monitor_desk_open_trades(conn: sqlite3.Connection, instruments_map: Dict[str, Any],
                             is_market_open_fn: Any) -> None:
    inst_keys = list(instruments_map.keys())
    if not inst_keys:
        return
    placeholders = ",".join("?" for _ in inst_keys)
    rows = conn.execute(
        f"SELECT id, opened_ts, instrument, mode, direction, entry, initial_sl, sl, tp1, tp2, tp3, "
        f"be_active, COALESCE(tp1_hit, 0), trail_dist, last_bar_ts, bars_held, source "
        f"FROM open_trades WHERE status='OPEN' AND instrument IN ({placeholders})",
        tuple(inst_keys),
    ).fetchall()

    for (tid, opened_ts, inst, mode, direction, entry, initial_sl, sl, tp1, tp2, tp3,
         be_active, tp1_hit_val, trail_dist, last_bar_ts, bars_held, src_locked) in rows:
        if not is_market_open_fn(inst, for_monitoring=True):
            continue
        _, df_ent, _ = fetch_pair_for_desk(instruments_map, inst, mode, is_market_open_fn, preferred_source=src_locked)
        if df_ent is None or df_ent.empty:
            continue

        tf = TIMEFRAMES[mode]
        ent_open_sec = to_epoch_sec(df_ent.index)
        if last_bar_ts:
            fresh = df_ent[ent_open_sec > parse_ts(last_bar_ts).timestamp() + 1.0]
        else:
            fresh = df_ent[(ent_open_sec + tf["entry_sec"]) > parse_ts(opened_ts).timestamp()]
        if fresh.empty:
            continue

        orig_risk = max(abs(float(entry) - float(initial_sl)), 1e-9)
        fee_r     = float(GLOBAL["taker_fee_bps"].get(inst, 0.0)) / 10000.0
        t_dist    = float(trail_dist) if trail_dist and float(trail_dist) > 0 else orig_risk * (tf["trail_atr"] / tf["sl_atr"])
        be_trig   = float(tf["be_trigger_r"]) * orig_risk
        active_sl = float(sl)
        be_on     = int(be_active or 0)
        tp1_hit   = int(tp1_hit_val or 0)
        held      = int(bars_held or 0)
        max_hold  = int(tf["max_hold_bars"])

        def calc_r(px: float) -> float:
            tail_r = ((px - entry) if direction == "BUY" else (entry - px)) / orig_risk
            gross_r = (0.5 * tf["tp1_r"] + 0.5 * tail_r) if tp1_hit else tail_r
            fee_drag_r = ((entry + px) * fee_r) / orig_risk
            return round(gross_r - fee_drag_r, 2)

        for bar_idx, bar in fresh.iterrows():
            bar_iso = bar_idx.strftime("%Y-%m-%dT%H:%M:%SZ")
            op, hi, lo, cl = float(bar["Open"]), float(bar["High"]), float(bar["Low"]), float(bar["Close"])
            held += 1

            if direction == "BUY":
                if op <= active_sl or lo <= active_sl:
                    ex = op if op <= active_sl else active_sl
                    r_res = calc_r(ex)
                    status = "CLOSED_TRAIL" if (be_on or tp1_hit) else "CLOSED_SL"
                    conn.execute(
                        "UPDATE open_trades SET status=?, closed_ts=?, exit_price=?, r_result=?, last_bar_ts=?, bars_held=? WHERE id=?",
                        (status, now_utc_iso(), ex, r_res, bar_iso, held, tid),
                    )
                    conn.commit()
                    notify_all(f"🛑 <b>{esc(inst)} {esc(mode)} BUY</b> {status} @ <code>{ex:.2f}</code> ({r_res:+.2f}R)")
                    break
                if not tp1_hit and hi >= tp1:
                    tp1_hit, be_on = 1, 1
                    active_sl = max(active_sl, entry + 0.08 * orig_risk)
                    conn.execute(
                        "UPDATE open_trades SET tp1_hit=1, be_active=1, sl=?, last_bar_ts=?, bars_held=? WHERE id=?",
                        (round(active_sl, 4), bar_iso, held, tid),
                    )
                    conn.commit()
                    notify_all(f"✅ <b>{esc(inst)} {esc(mode)} BUY</b> TP1 50% @ <code>{tp1:.2f}</code> | BE+ locked (SL <code>{active_sl:.2f}</code>)")
                if hi >= tp2:
                    r_res = calc_r(tp2)
                    conn.execute(
                        "UPDATE open_trades SET status='CLOSED_TP2', closed_ts=?, exit_price=?, r_result=?, last_bar_ts=?, bars_held=? WHERE id=?",
                        (now_utc_iso(), tp2, r_res, bar_iso, held, tid),
                    )
                    conn.commit()
                    notify_all(f"🎯 <b>{esc(inst)} {esc(mode)} BUY</b> TP2 @ <code>{tp2:.2f}</code> ({r_res:+.2f}R)")
                    break
                if (hi - entry) >= be_trig:
                    new_sl = max(active_sl, entry + 0.08 * orig_risk, hi - t_dist)
                    if new_sl > active_sl:
                        active_sl, be_on = new_sl, 1
                        conn.execute("UPDATE open_trades SET be_active=1, sl=?, last_bar_ts=?, bars_held=? WHERE id=?",
                                     (round(active_sl, 4), bar_iso, held, tid))
                        conn.commit()
            else:
                if op >= active_sl or hi >= active_sl:
                    ex = op if op >= active_sl else active_sl
                    r_res = calc_r(ex)
                    status = "CLOSED_TRAIL" if (be_on or tp1_hit) else "CLOSED_SL"
                    conn.execute(
                        "UPDATE open_trades SET status=?, closed_ts=?, exit_price=?, r_result=?, last_bar_ts=?, bars_held=? WHERE id=?",
                        (status, now_utc_iso(), ex, r_res, bar_iso, held, tid),
                    )
                    conn.commit()
                    notify_all(f"🛑 <b>{esc(inst)} {esc(mode)} SELL</b> {status} @ <code>{ex:.2f}</code> ({r_res:+.2f}R)")
                    break
                if not tp1_hit and lo <= tp1:
                    tp1_hit, be_on = 1, 1
                    active_sl = min(active_sl, entry - 0.08 * orig_risk)
                    conn.execute(
                        "UPDATE open_trades SET tp1_hit=1, be_active=1, sl=?, last_bar_ts=?, bars_held=? WHERE id=?",
                        (round(active_sl, 4), bar_iso, held, tid),
                    )
                    conn.commit()
                    notify_all(f"✅ <b>{esc(inst)} {esc(mode)} SELL</b> TP1 50% @ <code>{tp1:.2f}</code> | BE+ locked (SL <code>{active_sl:.2f}</code>)")
                if lo <= tp2:
                    r_res = calc_r(tp2)
                    conn.execute(
                        "UPDATE open_trades SET status='CLOSED_TP2', closed_ts=?, exit_price=?, r_result=?, last_bar_ts=?, bars_held=? WHERE id=?",
                        (now_utc_iso(), tp2, r_res, bar_iso, held, tid),
                    )
                    conn.commit()
                    notify_all(f"🎯 <b>{esc(inst)} {esc(mode)} SELL</b> TP2 @ <code>{tp2:.2f}</code> ({r_res:+.2f}R)")
                    break
                if (entry - lo) >= be_trig:
                    new_sl = min(active_sl, entry - 0.08 * orig_risk, lo + t_dist)
                    if new_sl < active_sl:
                        active_sl, be_on = new_sl, 1
                        conn.execute("UPDATE open_trades SET be_active=1, sl=?, last_bar_ts=?, bars_held=? WHERE id=?",
                                     (round(active_sl, 4), bar_iso, held, tid))
                        conn.commit()

            if held >= max_hold:
                r_res = calc_r(cl)
                conn.execute(
                    "UPDATE open_trades SET status='CLOSED_TIMEOUT', closed_ts=?, exit_price=?, r_result=?, last_bar_ts=?, bars_held=? WHERE id=?",
                    (now_utc_iso(), cl, r_res, bar_iso, held, tid),
                )
                conn.commit()
                notify_all(f"⏱️ <b>{esc(inst)} {esc(mode)} {direction}</b> TIMEOUT @ <code>{cl:.2f}</code> ({r_res:+.2f}R)")
                break

            conn.execute("UPDATE open_trades SET last_bar_ts=?, bars_held=? WHERE id=?", (bar_iso, held, tid))
            conn.commit()

def correlation_cap_ok(conn: sqlite3.Connection, instruments_map: Dict[str, Any],
                       inst: str, proposed_risk_usd: float) -> bool:
    bucket = instruments_map[inst]["corr_bucket"]
    same_bucket = [k for k, v in instruments_map.items() if v["corr_bucket"] == bucket]
    placeholders = ",".join("?" for _ in same_bucket)
    bucket_open = float(conn.execute(
        f"SELECT COALESCE(SUM(risk_usd), 0) FROM open_trades WHERE status='OPEN' AND instrument IN ({placeholders})",
        tuple(same_bucket),
    ).fetchone()[0] or 0.0)
    inst_open = float(conn.execute(
        "SELECT COALESCE(SUM(risk_usd), 0) FROM open_trades WHERE status='OPEN' AND instrument=?",
        (inst,),
    ).fetchone()[0] or 0.0)
    max_bucket_usd = float(ACCOUNT["balance"]) * (float(ACCOUNT["max_corr_bucket_risk"]) / 100.0)
    max_inst_usd   = float(ACCOUNT["balance"]) * (float(ACCOUNT["max_inst_risk_pct"]) / 100.0)
    if (inst_open + proposed_risk_usd) > (max_inst_usd + 0.01):
        logging.info(f"[risk-cap] skip {inst}: inst exposure {inst_open:.2f}+{proposed_risk_usd:.2f} > {max_inst_usd:.2f}")
        return False
    if (bucket_open + proposed_risk_usd) > (max_bucket_usd + 0.01):
        logging.info(f"[corr-cap] skip {inst}: {bucket} {bucket_open:.2f}+{proposed_risk_usd:.2f} > {max_bucket_usd:.2f}")
        return False
    return True

def cooldown_ok(conn: sqlite3.Connection, instruments_map: Dict[str, Any],
                inst: str, mode: str, direction: str, bar_ts: str, proposed_risk_usd: float) -> bool:
    if conn.execute("SELECT COUNT(*) FROM open_trades WHERE instrument=? AND mode=? AND status='OPEN'", (inst, mode)).fetchone()[0] > 0:
        return False
    if conn.execute("SELECT COUNT(*) FROM open_trades WHERE instrument=? AND direction!=? AND status='OPEN'", (inst, direction)).fetchone()[0] > 0:
        logging.info(f"[hedge-guard] skip {inst} {mode} {direction}: opposite direction already open")
        return False
    if conn.execute("SELECT COUNT(*) FROM signals WHERE instrument=? AND mode=? AND bar_ts=?", (inst, mode, bar_ts)).fetchone()[0] > 0:
        return False
    if not correlation_cap_ok(conn, instruments_map, inst, proposed_risk_usd):
        return False
    row = conn.execute("SELECT direction, last_ts, last_bar_ts FROM cooldown WHERE instrument=? AND mode=?", (inst, mode)).fetchone()
    if not row:
        return True
    last_dir, last_ts, last_bar = row
    if last_bar and last_bar == bar_ts:
        return False
    tf = TIMEFRAMES[mode]
    age_min = (now_utc() - parse_ts(last_ts)).total_seconds() / 60.0
    return age_min >= (tf["cooldown_same_min"] if last_dir == direction else tf["cooldown_flip_min"])
