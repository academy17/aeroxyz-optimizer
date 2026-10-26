#!/usr/bin/env python3
"""Aero (aero.xyz) allocation manager: fetch live allocations, then optimize our AP across gauges."""
import os
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


def main():
    parser = argparse.ArgumentParser(description="Aero (aero.xyz) Allocation Manager")
    sub = parser.add_subparsers(dest="command")

    fetch_p = sub.add_parser("fetch", help="Fetch live allocations, fees and incentive programs")
    fetch_p.add_argument("--horizon-hours", type=int, default=47, help="Hours the allocation is held (default: 47h cooldown)")
    fetch_p.add_argument("--fee-lookback-hours", type=int, default=48, help="Trailing window for the fee rate (default 48)")

    opt_p = sub.add_parser("optimize", help="Optimize allocation")
    opt_p.add_argument("--max-pools", type=int, help="Max gauges (default: on-chain maxGauges)")
    opt_p.add_argument("--pools", nargs="+", help="Restrict to these pool addresses")

    args = parser.parse_args()
    if args.command == "fetch":
        fetch_allocations.run_fetch(args.horizon_hours, args.fee_lookback_hours)
    elif args.command == "optimize":
        with open(fetch_allocations.DASHBOARD_PATH) as f:
            dashboard = json.load(f)
        only = {p.lower() for p in args.pools} if args.pools else None
        out = optimizer.optimize(dashboard, args.max_pools, only)
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w") as f:
            json.dump(out, f, indent=2)
        # `gauge allocated_wei` lines for Voter.allocateGauges (Base, chainId 8453)
        with open(BOT_PATH, "w") as f:
            f.write("\n".join(f"{r['gauge']} {r['allocated_wei']}" for r in out["allocations"]))
        for r in out["allocations"]:
            logger.info(f"{r['symbol']:<16}{r['pct']:>4}%  exp ${r['exp_usd']:>9,.2f}")
        logger.info(f"{len(out['allocations'])} pools, ${out['exp_usd']:,} over {out['horizon_hours']}h -> {OUT_PATH}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
