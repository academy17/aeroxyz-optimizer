#!/usr/bin/env python3
"""Aero (aero.xyz) allocation manager: fetch live allocations, then optimize our AP across gauges."""
import os
import csv
import json
import argparse
import logging
from dotenv import load_dotenv

import fetch_allocations
import optimizer

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

OUT_PATH = "data/allocation.json"
BOT_PATH = "data/allocation_bot.txt"
CSV_PATH = "data/allocation.csv"


def write_csv(out):
    """Human-readable copy of the allocation for sharing. Pools at 0% get nothing, so they are left out."""
    rows = [r for r in out["allocations"] if r["pct"] > 0]
    ap = lambda r: round(int(r["allocated_wei"]) / 1e18)
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads symbols like USD₮0
        w = csv.writer(f)
        w.writerow(["Chain", "Pool", "Share of AP (%)", "AP", f"Expected USD ({out['horizon_hours']}h)", "Fees USD",
                    "Incentives USD", "Other AP on pool", "Gauge", "Pool address"])
        for r in rows:
            w.writerow([r["chain"], r["symbol"], r["pct"], ap(r), r["exp_usd"], r["fees_usd"], r["incentives_usd"],
                        r["others_ap"], r["gauge"], r["pool"]])
        w.writerow(["Total", "", sum(r["pct"] for r in rows), sum(ap(r) for r in rows), round(sum(r["exp_usd"] for r in rows), 2)])


def main():
    parser = argparse.ArgumentParser(description="Aero (aero.xyz) Allocation Manager")
    sub = parser.add_subparsers(dest="command")

    fetch_p = sub.add_parser("fetch", help="Fetch live allocations, fees and incentive programs")
    fetch_p.add_argument("--horizon-hours", type=int, default=47, help="Hours the allocation is held (default: 47h cooldown)")
    fetch_p.add_argument("--fee-lookback-hours", type=int, default=48, help="Trailing window for the fee rate (default 48)")
    fetch_p.add_argument("--chains", nargs="+", type=str.lower, help="Chain names to fetch (default: all with an RPC)")

    opt_p = sub.add_parser("optimize", help="Optimize allocation")
    opt_p.add_argument("--max-pools", type=int, help="Max gauges per chain (default: on-chain maxGauges)")
    opt_p.add_argument("--pools", nargs="+", help="Restrict to these pool addresses")

    args = parser.parse_args()
    if args.command == "fetch":
        fetch_allocations.run_fetch(args.horizon_hours, args.fee_lookback_hours, args.chains)
    elif args.command == "optimize":
        with open(fetch_allocations.DASHBOARD_PATH) as f:
            dashboard = json.load(f)
        only = {p.lower() for p in args.pools} if args.pools else None
        out = optimizer.optimize(dashboard, args.max_pools, only)
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w") as f:
            json.dump(out, f, indent=2)
        # `gauge allocated_wei chain_id` lines for Voter.allocateGauges (one call per chain id)
        with open(BOT_PATH, "w") as f:
            f.write("\n".join(f"{r['gauge']} {r['allocated_wei']} {r['chain_id']}" for r in out["allocations"]))
        write_csv(out)
        for r in out["allocations"]:
            logger.info(f"{r['chain']:<10}{r['symbol']:<16}{r['pct']:>4}%  exp ${r['exp_usd']:>9,.2f}")
        for c in out["chains"]:
            logger.info(f"{c['chain']:<10}{c['pct']:>4}% of AP")
        logger.info(f"{len(out['allocations'])} pools, ${out['exp_usd']:,} over {out['horizon_hours']}h -> {OUT_PATH}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
