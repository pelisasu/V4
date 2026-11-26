"""
==============================================================================
UNIFIED WORKFLOW ORCHESTRATOR — v4.1.0
Runs Desk 1 (TradFi: XAUUSD & NAS100) and Desk 2 (Crypto: BTCUSD & ETHUSD)
in 1 unified CLI & GitHub Actions workflow.
CLI:
  python bot_runner.py --desk all --mode live
  python bot_runner.py --desk tradfi --mode both
  python bot_runner.py --desk crypto --mode check
==============================================================================
"""
import sys
import sqlite3
import logging
import argparse
import traceback
from concurrent.futures import ThreadPoolExecutor

import quant_core as qc
import desk_tradfi
import desk_crypto

def _run_desk_live(desk: str) -> int:
    conn = sqlite3.connect(qc.DB_PATH, timeout=30.0)
    try:
        if desk == "tradfi":
            emitted = desk_tradfi.run_live(conn)
        else:
            emitted = desk_crypto.run_live(conn)
        return len(emitted)
    finally:
        conn.close()

def _run_desk_calibration(desk: str) -> None:
    if desk == "tradfi":
        desk_tradfi.run_calibration("SWING", plot_chart=True)
        desk_tradfi.run_calibration("SCALPING", plot_chart=False)
    else:
        desk_crypto.run_calibration("SWING", plot_chart=True)
        desk_crypto.run_calibration("SCALPING", plot_chart=False)

def main() -> None:
    parser = argparse.ArgumentParser(description="QuantDesk v4.1 Unified Dual-Desk Runner")
    parser.add_argument("--desk", choices=["all", "tradfi", "crypto"], default="all")
    parser.add_argument("--mode", choices=["live", "backtest", "both", "check"], default="live")
    parser.add_argument("--sequential", action="store_true", help="Disable concurrent desk execution")
    args = parser.parse_args()

    qc.setup_logging()
    logging.info(f"[runner] v4.1.0 desk={args.desk} mode={args.mode} start={qc.now_utc_iso()}")

    conn = sqlite3.connect(qc.DB_PATH, timeout=30.0)
    qc.db_init(conn)
    conn.close()

    desks = ["tradfi", "crypto"] if args.desk == "all" else [args.desk]

    try:
        if args.mode == "check":
            conn = sqlite3.connect(qc.DB_PATH, timeout=30.0)
            all_lines = []
            if "tradfi" in desks:
                all_lines.extend(desk_tradfi.run_check(conn))
            if "crypto" in desks:
                all_lines.extend(desk_crypto.run_check(conn))
            conn.close()
            for l in all_lines:
                logging.info(l)
            qc.send_telegram("\n".join(all_lines))

        if args.mode in ("live", "both"):
            if not qc.HAS_DATA_LIBS:
                raise RuntimeError("Missing numpy/pandas dependencies")
            if len(desks) == 2 and not args.sequential:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futs = {d: pool.submit(_run_desk_live, d) for d in desks}
                    for d, f in futs.items():
                        cnt = f.result()
                        logging.info(f"[runner] desk={d} live completed (new_signals={cnt})")
            else:
                for d in desks:
                    cnt = _run_desk_live(d)
                    logging.info(f"[runner] desk={d} live completed (new_signals={cnt})")

        if args.mode in ("backtest", "both"):
            if not qc.HAS_DATA_LIBS:
                raise RuntimeError("Missing numpy/pandas dependencies")
            if len(desks) == 2 and not args.sequential:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futs = {d: pool.submit(_run_desk_calibration, d) for d in desks}
                    for d, f in futs.items():
                        f.result()
                        logging.info(f"[runner] desk={d} calibration completed")
            else:
                for d in desks:
                    _run_desk_calibration(d)
                    logging.info(f"[runner] desk={d} calibration completed")

    except Exception as e:
        logging.error(f"[fatal] {e}\n{traceback.format_exc()}")
        qc.send_telegram(f"⚠️ <b>QuantDesk v4.1 Error ({qc.esc(args.desk)})</b>\n<code>{qc.esc(str(e)[:300])}</code>")
        sys.exit(1)
    finally:
        logging.info(f"[runner] v4.1.0 end={qc.now_utc_iso()}")

if __name__ == "__main__":
    main()
