"""
==============================================================================
DESK 1: TRADFI DESK (XAUUSD & NAS100) — v4.1.0
Instruments : XAUUSD (COMEX/Spot Gold) & NAS100 (US Tech 100 / E-mini NQ)
Guards      : CME Futures Calendar, NY Daily Rollover Blackout (16:55-18:05),
              London/NY Overlap (XAUUSD), NAS100 Opening-Bell Filter (09:30-09:45),
              TwelveData + Yahoo Basis Cross-Feed Sanity
==============================================================================
"""
import os
import json
import logging
import sqlite3
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any

import requests
import pandas as pd

import quant_core as qc

DESK_NAME = "tradfi"
PARAMS_PATH = os.getenv("BOT_PARAMS_TRADFI_PATH", "calibrated_params_tradfi.json")
CHART_PATH  = os.getenv("BOT_CHART_TRADFI_PATH", "backtest_tradfi_v4_1.png")

TRADFI_INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    "XAUUSD": {
        "source_groups": [
            {"name": "twelvedata_xau", "type": "twelvedata", "symbol": "XAU/USD",  "multiplier": 1.0, "offset": 0.0},
            {"name": "yahoo_xauusd",   "type": "yfinance",   "symbol": "XAUUSD=X", "multiplier": 1.0, "offset": 0.0},
            {"name": "yahoo_gcf",      "type": "yfinance",   "symbol": "GC=F",     "multiplier": 1.0, "offset": 0.0},
        ],
        "contract": {
            "pip_value_per_lot": float(os.getenv("XAU_PIP_VALUE", "100.0")),
            "min_lot": 0.01, "max_lot": 5.0, "lot_step": 0.01, "decimals": 2,
        },
        "session_filter": True,
        "kind": "commodity",
        "corr_bucket": "METALS",
    },
    "NAS100": {
        "source_groups": [
            {"name": "twelvedata_ndx", "type": "twelvedata", "symbol": "NDX",  "multiplier": 1.0, "offset": 0.0},
            {"name": "yahoo_nq",       "type": "yfinance",   "symbol": "NQ=F", "multiplier": 1.0, "offset": 0.0},
            {"name": "yahoo_ndx",      "type": "yfinance",   "symbol": "^NDX", "multiplier": 1.0, "offset": 0.0},
        ],
        "contract": {
            "pip_value_per_lot": float(os.getenv("NAS100_PIP_VALUE", "1.0")),
            "min_lot": 0.01, "max_lot": 10.0, "lot_step": 0.01, "decimals": 2,
        },
        "session_filter": True,
        "kind": "index",
        "corr_bucket": "US_EQUITY",
    },
}

# ==============================================================================
# TRADFI SESSION & ROLLOVER GUARDS
# ==============================================================================
def _cme_futures_open(ny: datetime) -> bool:
    wd = ny.weekday()
    if wd == 5:
        return False
    if wd == 4 and ny.hour >= 17:
        return False
    if wd == 6 and ny.hour < 18:
        return False
    if ny.hour == 17:
        return False
    return True

def rollover_blackout(inst: str, now: Optional[datetime] = None) -> bool:
    ny = (now or qc.now_utc()).astimezone(qc.NY_TZ)
    mins = ny.hour * 60 + ny.minute
    return (16 * 60 + 55) <= mins <= (18 * 60 + 5)

def nas_opening_bell_hold(inst: str, now: Optional[datetime] = None) -> bool:
    if TRADFI_INSTRUMENTS[inst]["kind"] != "index":
        return False
    ny = (now or qc.now_utc()).astimezone(qc.NY_TZ)
    if ny.weekday() >= 5:
        return False
    mins = ny.hour * 60 + ny.minute
    delay = int(qc.GLOBAL.get("nas_open_delay_min", 15))
    return (9 * 60 + 30) <= mins < (9 * 60 + 30 + delay)

def market_open(inst: str, now: Optional[datetime] = None, for_monitoring: bool = False,
                mode: Optional[str] = None) -> bool:
    kind = TRADFI_INSTRUMENTS[inst]["kind"]
    ref = now or qc.now_utc()
    ny = ref.astimezone(qc.NY_TZ)
    wd = ny.weekday()
    mins = ny.hour * 60 + ny.minute
    if kind == "index":
        if for_monitoring:
            return _cme_futures_open(ny)
        if mode == "SWING":
            return (wd < 5) and (8 * 60 <= mins <= 16 * 60)
        return (wd < 5) and (9 * 60 + 30 <= mins < 16 * 60)
    return _cme_futures_open(ny)

def scalping_session_ok(inst: str, now: Optional[datetime] = None) -> bool:
    ref = now or qc.now_utc()
    if nas_opening_bell_hold(inst, ref):
        return False
    if TRADFI_INSTRUMENTS[inst]["kind"] == "index":
        return market_open(inst, ref, for_monitoring=False, mode="SCALPING")
    ldn = ref.astimezone(qc.LDN_TZ)
    ny  = ref.astimezone(qc.NY_TZ)
    return market_open(inst, ref, for_monitoring=False, mode="SCALPING") and (ldn.hour >= 8) and (ny.hour < 14)

# ==============================================================================
# TRADFI LIVE MICROSTRUCTURE (TWELVEDATA + YAHOO CROSS-FEED SANITY)
# ==============================================================================
def fetch_tradfi_microstructure(inst: str) -> Dict[str, Any]:
    default_sp = float(qc.GLOBAL["spread_pts"].get(inst, 1.0))
    out: Dict[str, Any] = {
        "price": None, "spread_pts": default_sp, "div_bps": 0.0, "source": "default",
    }
    td_px: Optional[float] = None
    yf_px: Optional[float] = None
    if qc.TWELVEDATA_API_KEY:
        td_sym = "XAU/USD" if inst == "XAUUSD" else "NDX"
        try:
            r = requests.get(
                "https://api.twelvedata.com/price",
                params={"symbol": td_sym, "apikey": qc.TWELVEDATA_API_KEY},
                timeout=6,
            )
            if r.ok and r.json().get("price"):
                td_px = float(r.json()["price"])
                out["price"] = td_px
                out["source"] = f"twelvedata:{td_sym}"
        except Exception:
            pass
    if qc.HAS_YF:
        yf_sym = "XAUUSD=X" if inst == "XAUUSD" else "NQ=F"
        try:
            fi = qc.yf.Ticker(yf_sym).fast_info
            last_p = float(fi.get("lastPrice") or 0.0)
            if last_p > 0:
                yf_px = last_p
                if out["price"] is None:
                    out["price"] = yf_px
                    out["source"] = f"yfinance:{yf_sym}"
        except Exception:
            pass
    if td_px and yf_px and yf_px > 0 and inst == "XAUUSD":
        out["div_bps"] = abs(td_px - yf_px) / yf_px * 10000.0
    return out

# ==============================================================================
# SIGNAL EVALUATION & EXECUTION
# ==============================================================================
def evaluate_tradfi_signal(instrument: str, mode: str,
                           calibrated: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cal = (calibrated or {}).get(f"{instrument}:{mode}")
    if cal is not None and not cal.get("enabled", True):
        logging.info(f"[tradfi] {instrument} {mode} | skip: disabled ({cal.get('reason', 'oos_fail')})")
        return None
    if rollover_blackout(instrument):
        logging.info(f"[tradfi] {instrument} {mode} | skip: daily rollover blackout")
        return None
    if mode == "SCALPING" and not scalping_session_ok(instrument):
        logging.info(f"[tradfi] {instrument} {mode} | skip: outside scalp/opening-bell window")
        return None

    micro = fetch_tradfi_microstructure(instrument)
    if micro["div_bps"] > float(qc.GLOBAL["max_cross_div_bps"]):
        logging.warning(f"[tradfi] {instrument} {mode} | skip: cross-feed divergence {micro['div_bps']:.1f}bps")
        return None

    df_htf, df_ent, src = qc.fetch_pair_for_desk(TRADFI_INSTRUMENTS, instrument, mode, market_open)
    if df_htf is None or df_ent is None:
        logging.info(f"[tradfi] {instrument} {mode} | skip: no fresh/sufficient data")
        return None

    df_htf, df_ent = qc.compute_indicators(df_htf), qc.compute_indicators(df_ent)
    htf_a, ent_a = qc.extract_arrays(df_htf), qc.extract_arrays(df_ent)
    htf_sh, htf_sl = qc.extract_causal_swings(df_htf, lookback=5)

    tf = qc.TIMEFRAMES[mode]
    cal_d = cal or {}
    threshold  = int(cal_d.get("threshold", qc.GLOBAL["min_score_signal"]))
    sl_mult    = float(cal_d.get("sl_mult", tf["sl_atr"]))
    tp2_mult   = float(cal_d.get("tp_mult", tf["tp_atr"][1]))
    trail_mult = float(cal_d.get("trail_mult", tf["trail_atr"]))
    tp3_mult   = max(float(tf["tp_atr"][2]), round(tp2_mult * 1.25, 2))

    htf_close_ts = qc.to_epoch_sec(df_htf.index) + tf["htf_sec"]
    ent_close_ts = qc.to_epoch_sec(df_ent.index) + tf["entry_sec"]
    eval_idx = len(df_ent) - 1
    htf_idx  = qc.align_htf_idx(htf_close_ts, float(ent_close_ts[eval_idx]))
    if htf_idx < 80:
        return None

    res, diag = qc.evaluate_bar_pair_arrays(
        htf_a, htf_sh, htf_sl, htf_idx, ent_a, eval_idx, threshold,
        instrument=instrument, live_spread_pts=micro["spread_pts"],
    )

    if res is None and mode == "SCALPING" and len(df_ent) >= 3:
        prev_bar_close = df_ent.index[-2] + pd.Timedelta(seconds=tf["entry_sec"])
        if (pd.Timestamp.now(tz="UTC") - prev_bar_close).total_seconds() <= 1200:
            prev_htf_idx = qc.align_htf_idx(htf_close_ts, float(ent_close_ts[-2]))
            if prev_htf_idx >= 80:
                res2, diag2 = qc.evaluate_bar_pair_arrays(
                    htf_a, htf_sh, htf_sl, prev_htf_idx, ent_a, len(df_ent) - 2, threshold,
                    instrument=instrument, live_spread_pts=micro["spread_pts"],
                )
                if res2 is not None:
                    c_now = float(ent_a["Close"][-1])
                    if abs(c_now - res2[4]["entry"]) <= 0.35 * res2[4]["atr"]:
                        eval_idx = len(df_ent) - 2
                        res = (res2[0], res2[1], res2[2], res2[3], {**res2[4], "entry": c_now})
                        diag = f"{diag2} [jitter-catch -2]"

    close_px = float(ent_a["Close"][-1])
    if res is None:
        logging.info(f"[tradfi] {instrument} {mode} | src={src} close={close_px:.2f} | {diag}")
        return None

    direction, score, regime, setup_name, ctx = res
    entry, atr = ctx["entry"], ctx["atr"]
    if direction == "BUY":
        sl, tp1, tp2, tp3 = entry - sl_mult * atr, entry + tf["tp1_r"] * sl_mult * atr, entry + tp2_mult * atr, entry + tp3_mult * atr
    else:
        sl, tp1, tp2, tp3 = entry + sl_mult * atr, entry - tf["tp1_r"] * sl_mult * atr, entry - tp2_mult * atr, entry - tp3_mult * atr

    risk_pct = qc.ACCOUNT["risk_pct_scalp"] if mode == "SCALPING" else qc.ACCOUNT["risk_pct_swing"]
    lot, risk_usd, risk_warn = qc.calc_lot(float(qc.ACCOUNT["balance"]), risk_pct, abs(entry - sl), TRADFI_INSTRUMENTS[instrument]["contract"])

    return {
        "desk": DESK_NAME, "instrument": instrument, "mode": mode, "direction": direction,
        "setup": setup_name, "entry": round(entry, 4), "sl": round(sl, 4),
        "tp1": round(tp1, 4), "tp2": round(tp2, 4), "tp3": round(tp3, 4),
        "trail_dist": round(trail_mult * atr, 4), "lot": lot, "risk_usd": risk_usd,
        "risk_warn": risk_warn, "score": score, "regime": regime, "source": src,
        "atr": round(atr, 4), "rsi": round(ctx["rsi"], 1), "adx": round(ctx["adx"], 1),
        "zscore": round(ctx["mad_z"], 2), "ts": qc.now_utc_iso(),
        "bar_ts": df_ent.index[eval_idx].strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

def run_live(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    qc.monitor_desk_open_trades(conn, TRADFI_INSTRUMENTS, market_open)
    calibrated = qc.load_calibrated_params(PARAMS_PATH)
    emitted: List[Dict[str, Any]] = []
    for inst in TRADFI_INSTRUMENTS:
        for mode in ("SCALPING", "SWING"):
            if not market_open(inst, for_monitoring=False, mode=mode):
                continue
            try:
                sig = evaluate_tradfi_signal(inst, mode, calibrated)
            except Exception as e:
                logging.exception(f"[tradfi] {inst} {mode}: {e}")
                continue
            if sig is None or not qc.cooldown_ok(conn, TRADFI_INSTRUMENTS, inst, mode, sig["direction"], sig["bar_ts"], sig["risk_usd"]):
                continue
            msg = qc.format_signal_message(sig)
            with conn:
                cur = conn.execute(
                    "INSERT INTO signals(ts,bar_ts,desk,instrument,mode,direction,entry,sl,tp1,tp2,tp3,lot,risk_usd,score,setup,regime,source,message,notified) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (sig["ts"], sig["bar_ts"], DESK_NAME, inst, mode, sig["direction"], sig["entry"], sig["sl"],
                     sig["tp1"], sig["tp2"], sig["tp3"], sig["lot"], sig["risk_usd"], sig["score"],
                     sig["setup"], sig["regime"], sig["source"], msg),
                )
                sig_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO open_trades(opened_ts,desk,instrument,mode,direction,entry,initial_sl,sl,tp1,tp2,tp3,lot,risk_usd,trail_dist,last_bar_ts,bars_held,source) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                    (sig["ts"], DESK_NAME, inst, mode, sig["direction"], sig["entry"], sig["sl"], sig["sl"],
                     sig["tp1"], sig["tp2"], sig["tp3"], sig["lot"], sig["risk_usd"], sig["trail_dist"],
                     sig["bar_ts"], sig["source"]),
                )
                conn.execute(
                    "INSERT INTO cooldown(instrument,mode,direction,last_ts,last_bar_ts) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(instrument,mode) DO UPDATE SET direction=excluded.direction, last_ts=excluded.last_ts, last_bar_ts=excluded.last_bar_ts",
                    (inst, mode, sig["direction"], sig["ts"], sig["bar_ts"]),
                )
            tg_ok, dc_ok = qc.notify_all(msg)
            if tg_ok or dc_ok:
                conn.execute("UPDATE signals SET notified=1 WHERE id=?", (sig_id,))
                conn.commit()
            emitted.append(sig)
    return emitted

def _precompute_tradfi_signals(
    htf_a: Dict[str, Any], htf_sh: Any, htf_sl: Any, htf_close_ts: Any,
    ent_a: Dict[str, Any], ent_close_ts: Any, ent_dts: List[datetime],
    instrument: str, mode: str, threshold: int,
) -> List[Optional[Tuple[str, float]]]:
    n_ent = len(ent_dts)
    sig_cache: List[Optional[Tuple[str, float]]] = [None] * n_ent
    for i in range(80, n_ent - 2):
        htf_idx = qc.align_htf_idx(htf_close_ts, float(ent_close_ts[i]))
        if htf_idx < 80:
            continue
        bar_dt = ent_dts[i + 1]
        if not market_open(instrument, bar_dt, for_monitoring=False, mode=mode):
            continue
        if rollover_blackout(instrument, bar_dt):
            continue
        if mode == "SCALPING" and not scalping_session_ok(instrument, bar_dt):
            continue
        res, _ = qc.evaluate_bar_pair_arrays(htf_a, htf_sh, htf_sl, htf_idx, ent_a, i, threshold, instrument=instrument)
        if res is not None:
            sig_cache[i] = (res[0], float(res[4]["atr"]))
    return sig_cache

def run_calibration(mode: str = "SWING", plot_chart: bool = True) -> None:
    calibrated = qc.load_calibrated_params(PARAMS_PATH)
    curves: Dict[str, List[float]] = {}
    summary_lines: List[str] = []
    tf = qc.TIMEFRAMES[mode]
    sl_grid = (1.7, 2.0) if mode == "SCALPING" else (2.0, 2.3)
    tp_grid = (3.0, 3.8) if mode == "SCALPING" else (3.8, 4.8)

    for inst in TRADFI_INSTRUMENTS:
        df_htf, df_ent, src = qc.fetch_pair_for_desk(TRADFI_INSTRUMENTS, inst, mode, market_open, history=True)
        if df_htf is None or df_ent is None or len(df_ent) < 400:
            logging.info(f"[tradfi-cal] {inst} {mode} insufficient history")
            continue
        df_htf, df_ent = qc.compute_indicators(df_htf), qc.compute_indicators(df_ent)
        htf_a, ent_a = qc.extract_arrays(df_htf), qc.extract_arrays(df_ent)
        htf_sh, htf_sl = qc.extract_causal_swings(df_htf, lookback=5)

        htf_close_ts = qc.to_epoch_sec(df_htf.index) + tf["htf_sec"]
        ent_open_ts  = qc.to_epoch_sec(df_ent.index)
        ent_close_ts = ent_open_ts + tf["entry_sec"]
        ent_dts      = [ts.to_pydatetime() for ts in df_ent.index]
        n_ent, usable = len(df_ent), len(df_ent) - 80

        if n_ent >= 5500:
            folds = [(80 + int(usable * 0.5), 80 + int(usable * 0.625)), (80 + int(usable * 0.625), 80 + int(usable * 0.75)),
                     (80 + int(usable * 0.75), 80 + int(usable * 0.875)), (80 + int(usable * 0.875), n_ent)]
            min_passes_req = 2
        else:
            folds = [(80 + int(usable * 0.5), 80 + int(usable * 0.75)), (80 + int(usable * 0.75), n_ent)]
            min_passes_req = 1

        sig_caches = {th: _precompute_tradfi_signals(htf_a, htf_sh, htf_sl, htf_close_ts, ent_a, ent_close_ts, ent_dts, inst, mode, th) for th in (66, 70)}
        oos_passes, stitched_oos_r = 0, []
        latest_pick = (70, tf["sl_atr"], tf["tp_atr"][1])

        for is_end, oos_end in folds:
            best_is = None
            for th, sc in sig_caches.items():
                for slm in sl_grid:
                    for tpm in tp_grid:
                        if tpm <= slm * 1.35:
                            continue
                        is_r = qc.backtest_mtf_fast(ent_a, ent_open_ts, sc, inst, mode, slm, tpm, min_trades=5, start_idx=80, end_idx=is_end)
                        if best_is is None or is_r["score"] > best_is[0]:
                            best_is = (is_r["score"], th, slm, tpm)
            if best_is is None:
                continue
            _, th, slm, tpm = best_is
            latest_pick = (th, slm, tpm)
            oos_r = qc.backtest_mtf_fast(ent_a, ent_open_ts, sig_caches[th], inst, mode, slm, tpm, min_trades=1, start_idx=is_end, end_idx=oos_end)
            stitched_oos_r.extend(oos_r["trades_r"])
            if oos_r["trades"] >= 2 and oos_r["profit_factor"] >= 0.98 and oos_r["max_dd"] <= 0.25:
                oos_passes += 1

        oos_gw = sum(r for r in stitched_oos_r if r > 0)
        oos_gl = abs(sum(r for r in stitched_oos_r if r < 0))
        stitched_oos_pf = (oos_gw / (oos_gl + 1e-8)) if stitched_oos_r else 0.0
        th, slm, tpm = latest_pick
        full_r = qc.backtest_mtf_fast(ent_a, ent_open_ts, sig_caches[th], inst, mode, slm, tpm, min_trades=8, start_idx=80, end_idx=n_ent)
        mc_prob, mc_dd95 = qc.monte_carlo_stats(full_r["trades_r"], runs=int(qc.GLOBAL["mc_runs"]), risk_frac=float(qc.GLOBAL["bt_risk_pct"]))
        n_folds = len(folds)
        enabled = (oos_passes >= min_passes_req) and (len(stitched_oos_r) >= 4) and (stitched_oos_pf >= 1.02) and (full_r["trades"] >= 8) and (full_r["profit_factor"] >= float(qc.GLOBAL["bt_min_profit_factor"])) and (mc_prob >= float(qc.GLOBAL["mc_min_prob_profit"]))

        calibrated[f"{inst}:{mode}"] = {
            "enabled": enabled, "desk": DESK_NAME,
            "reason": f"passed_{n_folds}f_wf_mc" if enabled else f"failed_wf(folds={oos_passes}/{n_folds},oos_pf={stitched_oos_pf:.2f},mc={mc_prob:.0%})",
            "sl_mult": slm, "tp_mult": tpm, "trail_mult": tf["trail_atr"], "threshold": th,
            "oos_pf": round(stitched_oos_pf, 2), "full_pf": round(full_r["profit_factor"], 2),
            "sqn": round(full_r["sqn"], 2), "mc_prob": round(mc_prob, 3), "mc_dd95": round(mc_dd95, 3),
            "folds_passed": oos_passes, "updated_at": qc.now_utc_iso(),
        }
        if enabled:
            curves[inst] = full_r["equity_curve"]
            summary_lines.append(f"• <b>{inst}</b> ✅ {oos_passes}/{n_folds}F | OOS PF <code>{stitched_oos_pf:.2f}</code> | SQN <code>{full_r['sqn']:.2f}</code> | MC <code>{mc_prob:.0%}</code> (DD95 {mc_dd95:.1%})")
        else:
            summary_lines.append(f"• <b>{inst}</b> ⛔ off ({oos_passes}/{n_folds}F, OOS PF {stitched_oos_pf:.2f}, MC {mc_prob:.0%})")

    with open(PARAMS_PATH, "w", encoding="utf-8") as f:
        json.dump(calibrated, f, indent=2)
    if summary_lines:
        qc.send_telegram(f"📊 <b>[TRADFI] Walk-Forward v4.1 ({mode})</b>\n" + "\n".join(summary_lines))
    if plot_chart and curves and qc.HAS_MPL:
        qc.plt.figure(figsize=(10, 5))
        for name, c in curves.items():
            qc.plt.plot(c, label=name)
        qc.plt.title(f"TradFi Desk (XAUUSD & NAS100) — {mode}")
        qc.plt.xlabel("Trade #"); qc.plt.ylabel("Equity ($)"); qc.plt.legend(loc="best"); qc.plt.grid(True, alpha=0.3); qc.plt.tight_layout()
        qc.plt.savefig(CHART_PATH, dpi=110); qc.plt.close()

def run_check(conn: sqlite3.Connection) -> List[str]:
    open_cnt = conn.execute("SELECT COUNT(*) FROM open_trades WHERE status='OPEN' AND instrument IN ('XAUUSD','NAS100')").fetchone()[0]
    lines = [f"🩺 <b>TradFi Desk Check</b> | Open: <code>{open_cnt}</code>"]
    for inst in TRADFI_INSTRUMENTS:
        micro = fetch_tradfi_microstructure(inst)
        px = f"{micro['price']:.2f}" if micro["price"] else "feed-fallback"
        sess = "OPEN" if market_open(inst, mode="SCALPING") else ("SWING-ONLY" if market_open(inst, mode="SWING") else "CLOSED")
        lines.append(f"• {inst} [{sess}]: px={px} spread={micro['spread_pts']:.2f} div={micro['div_bps']:.1f}bps ({micro['source']})")
    return lines
