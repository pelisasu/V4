"""
==============================================================================
DESK 2: CRYPTO DESK (BTCUSD & ETHUSD) — v4.1.0
Instruments : BTCUSD & ETHUSD (24/7 Continuous Markets)
Guards      : 3-Way Live Sanity (Coinbase + Binance + Kraken Fallback),
              Weekend Thin-Liquidity Volume Confirmation (vol_ratio >= 1.25),
              Round-Trip Taker Commission Modeling, CRYPTO_BETA Correlation Cap
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

DESK_NAME = "crypto"
PARAMS_PATH = os.getenv("BOT_PARAMS_CRYPTO_PATH", "calibrated_params_crypto.json")
CHART_PATH  = os.getenv("BOT_CHART_CRYPTO_PATH", "backtest_crypto_v4_1.png")

CRYPTO_INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    "BTCUSD": {
        "source_groups": [
            {"name": "coinbase_btc", "type": "coinbase", "symbol": "BTC-USD", "binance": "BTCUSDT", "kraken": "XBTUSD", "multiplier": 1.0, "offset": 0.0},
            {"name": "yahoo_btc",    "type": "yfinance", "symbol": "BTC-USD", "multiplier": 1.0, "offset": 0.0},
        ],
        "contract": {
            "pip_value_per_lot": float(os.getenv("BTC_PIP_VALUE", "1.0")),
            "min_lot": 0.01, "max_lot": 5.0, "lot_step": 0.01, "decimals": 2,
        },
        "session_filter": False,
        "kind": "crypto",
        "corr_bucket": "CRYPTO_BETA",
    },
    "ETHUSD": {
        "source_groups": [
            {"name": "coinbase_eth", "type": "coinbase", "symbol": "ETH-USD", "binance": "ETHUSDT", "kraken": "ETHUSD", "multiplier": 1.0, "offset": 0.0},
            {"name": "yahoo_eth",    "type": "yfinance", "symbol": "ETH-USD", "multiplier": 1.0, "offset": 0.0},
        ],
        "contract": {
            "pip_value_per_lot": float(os.getenv("ETH_PIP_VALUE", "1.0")),
            "min_lot": 0.01, "max_lot": 50.0, "lot_step": 0.01, "decimals": 2,
        },
        "session_filter": False,
        "kind": "crypto",
        "corr_bucket": "CRYPTO_BETA",
    },
}

def market_open(inst: str, now: Optional[datetime] = None, for_monitoring: bool = False,
                mode: Optional[str] = None) -> bool:
    return True  # Crypto operates 24/7

def is_weekend_utc(now: Optional[datetime] = None) -> bool:
    ref = now or qc.now_utc()
    return ref.weekday() >= 5  # Saturday(5), Sunday(6) UTC

# ==============================================================================
# 3-WAY CRYPTO MICROSTRUCTURE (COINBASE + BINANCE + KRAKEN FALLBACK)
# ==============================================================================
def fetch_crypto_microstructure(inst: str) -> Dict[str, Any]:
    default_sp = float(qc.GLOBAL["spread_pts"].get(inst, 1.0))
    out: Dict[str, Any] = {
        "price": None, "bid": None, "ask": None,
        "spread_pts": default_sp, "div_bps": 0.0, "source": "default", "ref_exchange": "none",
    }
    cb_sym = "BTC-USD" if inst == "BTCUSD" else "ETH-USD"
    bn_sym = "BTCUSDT" if inst == "BTCUSD" else "ETHUSDT"
    kr_sym = "XBTUSD"  if inst == "BTCUSD" else "ETHUSD"
    kr_key = "XXBTZUSD" if inst == "BTCUSD" else "XETHZUSD"

    try:
        r_cb = requests.get(
            f"https://api.exchange.coinbase.com/products/{cb_sym}/ticker",
            timeout=6, headers={"User-Agent": "quantdesk-v4.1"},
        )
        if r_cb.ok:
            d = r_cb.json()
            bid, ask, px = float(d.get("bid", 0) or 0), float(d.get("ask", 0) or 0), float(d.get("price", 0) or 0)
            if bid > 0 and ask >= bid and px > 0:
                out["price"], out["bid"], out["ask"] = px, bid, ask
                out["spread_pts"] = max(ask - bid, 0.01)
                out["source"] = f"coinbase:{cb_sym}"
    except Exception:
        pass

    if out["price"]:
        # Primary reference: Binance; Fallback reference: Kraken (unblocked on US runners)
        ref_mid = None
        try:
            r_bn = requests.get(f"https://api.binance.com/api/v3/bookTicker?symbol={bn_sym}", timeout=4)
            if r_bn.ok:
                bd = r_bn.json()
                ref_mid = 0.5 * (float(bd["bidPrice"]) + float(bd["askPrice"]))
                out["ref_exchange"] = f"binance:{bn_sym}"
        except Exception:
            pass
        if not ref_mid:
            try:
                r_kr = requests.get(f"https://api.kraken.com/0/public/Ticker?pair={kr_sym}", timeout=5)
                if r_kr.ok:
                    res = r_kr.json().get("result", {})
                    pair_data = res.get(kr_key) or (next(iter(res.values())) if res else None)
                    if pair_data:
                        ref_mid = 0.5 * (float(pair_data["b"][0]) + float(pair_data["a"][0]))
                        out["ref_exchange"] = f"kraken:{kr_sym}"
            except Exception:
                pass
        if ref_mid and ref_mid > 0:
            out["div_bps"] = abs(out["price"] - ref_mid) / ref_mid * 10000.0
    return out

# ==============================================================================
# SIGNAL EVALUATION & EXECUTION
# ==============================================================================
def evaluate_crypto_signal(instrument: str, mode: str,
                           calibrated: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cal = (calibrated or {}).get(f"{instrument}:{mode}")
    if cal is not None and not cal.get("enabled", True):
        logging.info(f"[crypto] {instrument} {mode} | skip: disabled ({cal.get('reason', 'oos_fail')})")
        return None

    micro = fetch_crypto_microstructure(instrument)
    if micro["div_bps"] > float(qc.GLOBAL["max_cross_div_bps"]):
        logging.warning(f"[crypto] {instrument} {mode} | skip: divergence {micro['div_bps']:.1f}bps vs {micro['ref_exchange']}")
        return None

    df_htf, df_ent, src = qc.fetch_pair_for_desk(CRYPTO_INSTRUMENTS, instrument, mode, market_open)
    if df_htf is None or df_ent is None:
        logging.info(f"[crypto] {instrument} {mode} | skip: no fresh/sufficient data")
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
    min_vol    = float(qc.GLOBAL["crypto_weekend_vol"]) if is_weekend_utc() else 1.10

    htf_close_ts = qc.to_epoch_sec(df_htf.index) + tf["htf_sec"]
    ent_close_ts = qc.to_epoch_sec(df_ent.index) + tf["entry_sec"]
    eval_idx = len(df_ent) - 1
    htf_idx  = qc.align_htf_idx(htf_close_ts, float(ent_close_ts[eval_idx]))
    if htf_idx < 80:
        return None

    res, diag = qc.evaluate_bar_pair_arrays(
        htf_a, htf_sh, htf_sl, htf_idx, ent_a, eval_idx, threshold,
        instrument=instrument, live_spread_pts=micro["spread_pts"], min_vol_ratio=min_vol,
    )

    if res is None and mode == "SCALPING" and len(df_ent) >= 3:
        prev_bar_close = df_ent.index[-2] + pd.Timedelta(seconds=tf["entry_sec"])
        if (pd.Timestamp.now(tz="UTC") - prev_bar_close).total_seconds() <= 1200:
            prev_htf_idx = qc.align_htf_idx(htf_close_ts, float(ent_close_ts[-2]))
            if prev_htf_idx >= 80:
                res2, diag2 = qc.evaluate_bar_pair_arrays(
                    htf_a, htf_sh, htf_sl, prev_htf_idx, ent_a, len(df_ent) - 2, threshold,
                    instrument=instrument, live_spread_pts=micro["spread_pts"], min_vol_ratio=min_vol,
                )
                if res2 is not None:
                    c_now = float(ent_a["Close"][-1])
                    if abs(c_now - res2[4]["entry"]) <= 0.35 * res2[4]["atr"]:
                        eval_idx = len(df_ent) - 2
                        res = (res2[0], res2[1], res2[2], res2[3], {**res2[4], "entry": c_now})
                        diag = f"{diag2} [jitter-catch -2]"

    close_px = float(ent_a["Close"][-1])
    if res is None:
        logging.info(f"[crypto] {instrument} {mode} | src={src} close={close_px:.2f} | {diag}")
        return None

    direction, score, regime, setup_name, ctx = res
    entry, atr = ctx["entry"], ctx["atr"]
    if direction == "BUY":
        sl, tp1, tp2, tp3 = entry - sl_mult * atr, entry + tf["tp1_r"] * sl_mult * atr, entry + tp2_mult * atr, entry + tp3_mult * atr
    else:
        sl, tp1, tp2, tp3 = entry + sl_mult * atr, entry - tf["tp1_r"] * sl_mult * atr, entry - tp2_mult * atr, entry - tp3_mult * atr

    risk_pct = qc.ACCOUNT["risk_pct_scalp"] if mode == "SCALPING" else qc.ACCOUNT["risk_pct_swing"]
    lot, risk_usd, risk_warn = qc.calc_lot(float(qc.ACCOUNT["balance"]), risk_pct, abs(entry - sl), CRYPTO_INSTRUMENTS[instrument]["contract"])

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
    qc.monitor_desk_open_trades(conn, CRYPTO_INSTRUMENTS, market_open)
    calibrated = qc.load_calibrated_params(PARAMS_PATH)
    emitted: List[Dict[str, Any]] = []
    for inst in CRYPTO_INSTRUMENTS:
        for mode in ("SCALPING", "SWING"):
            try:
                sig = evaluate_crypto_signal(inst, mode, calibrated)
            except Exception as e:
                logging.exception(f"[crypto] {inst} {mode}: {e}")
                continue
            if sig is None or not qc.cooldown_ok(conn, CRYPTO_INSTRUMENTS, inst, mode, sig["direction"], sig["bar_ts"], sig["risk_usd"]):
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

def _precompute_crypto_signals(
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
        min_vol = float(qc.GLOBAL["crypto_weekend_vol"]) if is_weekend_utc(ent_dts[i + 1]) else 1.10
        res, _ = qc.evaluate_bar_pair_arrays(htf_a, htf_sh, htf_sl, htf_idx, ent_a, i, threshold, instrument=instrument, min_vol_ratio=min_vol)
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

    for inst in CRYPTO_INSTRUMENTS:
        df_htf, df_ent, src = qc.fetch_pair_for_desk(CRYPTO_INSTRUMENTS, inst, mode, market_open, history=True)
        if df_htf is None or df_ent is None or len(df_ent) < 400:
            logging.info(f"[crypto-cal] {inst} {mode} insufficient history")
            continue
        logging.info(f"[crypto-cal] {inst} {mode} src={src} htf={len(df_htf)} ent={len(df_ent)}")
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

        sig_caches = {th: _precompute_crypto_signals(htf_a, htf_sh, htf_sl, htf_close_ts, ent_a, ent_close_ts, ent_dts, inst, mode, th) for th in (66, 70)}
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
            logging.info(
                f"[crypto-cal] {inst} {mode} ENABLED folds={oos_passes}/{n_folds} oos_PF={stitched_oos_pf:.2f} "
                f"th={th} sl={slm} tp={tpm} trades={full_r['trades']} PF={full_r['profit_factor']:.2f} "
                f"WR={full_r['winrate']:.1%} SQN={full_r['sqn']:.2f} MDD={full_r['max_dd']:.1%} MC={mc_prob:.0%} DD95={mc_dd95:.1%}"
            )
            summary_lines.append(f"• <b>{inst}</b> ✅ PF <code>{stitched_oos_pf:.2f}</code> | MC <code>{mc_prob:.0%}</code>")
        else:
            logging.info(
                f"[crypto-cal] {inst} {mode} DISABLED folds={oos_passes}/{n_folds} oos_PF={stitched_oos_pf:.2f} "
                f"th={th} sl={slm} tp={tpm} trades={full_r['trades']} PF={full_r['profit_factor']:.2f} "
                f"SQN={full_r['sqn']:.2f} MDD={full_r['max_dd']:.1%} MC={mc_prob:.0%} DD95={mc_dd95:.1%}"
            )
            summary_lines.append(f"• <b>{inst}</b> ⛔ off (PF {stitched_oos_pf:.2f}, MC {mc_prob:.0%})")

    with open(PARAMS_PATH, "w", encoding="utf-8") as f:
        json.dump(calibrated, f, indent=2)
    logging.info(f"[crypto-cal] saved params to {PARAMS_PATH}")
    if summary_lines:
        qc.send_telegram(f"📊 <b>CRYPTO {mode}</b>\n" + "\n".join(summary_lines))
    if plot_chart and curves and qc.HAS_MPL:
        qc.plt.figure(figsize=(10, 5))
        for name, c in curves.items():
            qc.plt.plot(c, label=name)
        qc.plt.title(f"Crypto Desk (BTCUSD & ETHUSD) — {mode}")
        qc.plt.xlabel("Trade #"); qc.plt.ylabel("Equity ($)"); qc.plt.legend(loc="best"); qc.plt.grid(True, alpha=0.3); qc.plt.tight_layout()
        qc.plt.savefig(CHART_PATH, dpi=110); qc.plt.close()

def run_check(conn: sqlite3.Connection) -> List[str]:
    open_cnt = conn.execute("SELECT COUNT(*) FROM open_trades WHERE status='OPEN' AND instrument IN ('BTCUSD','ETHUSD')").fetchone()[0]
    lines = [f"🩺 <b>Crypto</b> (Open: {open_cnt})"]
    for inst in CRYPTO_INSTRUMENTS:
        micro = fetch_crypto_microstructure(inst)
        px = f"{micro['price']:.2f}" if micro["price"] else "-"
        lines.append(f"• {inst}: <code>{px}</code>")
    return lines
