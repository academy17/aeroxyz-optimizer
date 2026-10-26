#!/usr/bin/env python3
"""
Aero (aero.xyz) allocation dashboard fetcher.

aero.xyz has no epochs and no relays: Allocation Power (AP) on a gauge earns, per second,
its pro-rata share of the pool's Exchange Revenue (fees) and of every live Incentive Program.
An allocation is locked for the cooldown (47h), so the value of allocating to a pool is the
revenue it streams over the next `horizon_hours`:

    R = fee_rate_usd * H  +  sum(incentive_usd_per_sec * overlap([start, end], [now, now + H]))
    W = VotingRewardsManager.totalSupply() - our allocation on that gauge
"""
import os
import json
import logging
import requests
from web3 import Web3

logger = logging.getLogger(__name__)

LEAF_VOTER = "0x382409fad8503FbCdbB27A4211551fa03e884078"
VOTING_ESCROW = "0xBF7977E1521e9a3032bf7243Ee4712f311f13915"
GAUGE_MANAGER = "0xbF234b32c5e58868243A4a14831626f3EdE7b183"
GAUGE_MANAGER_DEPLOY_BLOCK = 52688318
BLOCK_TIME = 2  # Base
LOG_CHUNK = 9000

GAUGES_CACHE = "data/gauges.json"
DASHBOARD_PATH = "data/dashboard.json"

ERC20_ABI = [
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
]


def load_abi(name):
    with open(f"abi/{name}.json") as f:
        return json.load(f)


def save_json(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_logs(w3, params, from_block, to_block):
    logs, b = [], from_block
    while b <= to_block:
        end = min(b + LOG_CHUNK, to_block)
        logs += w3.eth.get_logs({**params, "fromBlock": b, "toBlock": end})
        b = end + 1
    return logs


def fetch_gauges(w3, head):
    """All gauges from GaugeManager.GaugeCreated, cached and extended incrementally."""
    cache = {"last_block": GAUGE_MANAGER_DEPLOY_BLOCK - 1, "gauges": []}
    if os.path.exists(GAUGES_CACHE):
        with open(GAUGES_CACHE) as f:
            cache = json.load(f)
    gm = w3.eth.contract(GAUGE_MANAGER, abi=load_abi("GaugeManager"))
    event = gm.events.GaugeCreated()
    params = {"address": GAUGE_MANAGER, "topics": [event.topic]}
    for log in get_logs(w3, params, cache["last_block"] + 1, head):
        a = event.process_log(log).args
        cache["gauges"].append({"pool": a._target.lower(), "gauge": a._gauge, "vrm": a._votingRewardsManager})
    cache["last_block"] = head
    save_json(cache, GAUGES_CACHE)
    return cache["gauges"]


def fetch_fee_amounts(w3, vrms, head, lookback_hours):
    """Sum NotifyFeesAmount (fees credited to allocators) per VRM per token over the lookback."""
    vrm_set = {v.lower() for v in vrms}
    event = w3.eth.contract(abi=load_abi("VotingRewardsManager")).events.NotifyFeesAmount()
    from_block = head - lookback_hours * 3600 // BLOCK_TIME
    out = {}
    for log in get_logs(w3, {"topics": [event.topic]}, from_block, head):
        vrm = log["address"].lower()
        if vrm not in vrm_set:
            continue
        a = event.process_log(log).args
        token = a._token.lower()
        out.setdefault(vrm, {}).setdefault(token, 0)
        out[vrm][token] += a._amount
    return out


def fetch_prices(tokens):
    """USD prices from DefiLlama, keyed by lowercase address."""
    prices, tokens = {}, sorted(tokens)
    for i in range(0, len(tokens), 100):
        keys = ",".join(f"base:{t}" for t in tokens[i:i + 100])
        try:
            coins = requests.get(f"https://coins.llama.fi/prices/current/{keys}", timeout=30).json()["coins"]
            prices.update({k.split(":")[1].lower(): v["price"] for k, v in coins.items()})
        except Exception as e:
            logger.warning(f"DefiLlama price error: {e}")
    return prices


def run_fetch(horizon_hours=47, fee_lookback_hours=48):
    w3 = Web3(Web3.HTTPProvider(os.getenv("RPC_URL"), request_kwargs={"timeout": 60}))
    head = w3.eth.block_number
    now = w3.eth.get_block(head)["timestamp"]
    horizon_end = now + horizon_hours * 3600

    lv = w3.eth.contract(LEAF_VOTER, abi=load_abi("LeafVoter"))
    ve = w3.eth.contract(VOTING_ESCROW, abi=load_abi("VotingEscrow"))
    vrm_abi = load_abi("VotingRewardsManager")

    token_id = os.getenv("AEROXYZ_TOKEN_ID")
    if token_id:
        token_id = int(token_id)
    else:
        token_id = ve.functions.tokenOfOwnerByIndex(Web3.to_checksum_address(os.getenv("OWNER_ADDRESS")), 0).call()
    our_power_raw = ve.functions.balanceOfNFT(token_id).call()
    our_power = our_power_raw / 1e18
    max_gauges = lv.functions.maxGauges().call()
    logger.info(f"Stake #{token_id}: {our_power:,.0f} AP, cooldown {lv.functions.allocationCooldown().call() / 3600:.0f}h, "
                f"max {max_gauges} gauges")

    gauges = fetch_gauges(w3, head)
    logger.info(f"{len(gauges)} gauges")
    fees = fetch_fee_amounts(w3, [g["vrm"] for g in gauges], head, fee_lookback_hours)

    raw, tokens = [], set()
    for g in gauges:
        if not lv.functions.isActivated(g["gauge"]).call():
            continue
        v = w3.eth.contract(g["vrm"], abi=vrm_abi)
        n = v.functions.incentiveCount().call()
        # Program ids are 1-based (id 0 is an empty placeholder)
        programs = [p for p in (v.functions.incentives(list(range(1, n + 1))).call() if n else []) if p[4] > now and p[3] < horizon_end]
        t0, t1 = v.functions.token0().call().lower(), v.functions.token1().call().lower()
        raw.append((g, v.functions.totalSupply().call() / 1e18,
                    lv.functions.allocations(token_id, g["gauge"]).call() / 1e18, t0, t1, programs))
        tokens |= {t0, t1} | {p[0].lower() for p in programs}
    tokens |= {t for f in fees.values() for t in f}

    erc20 = {t: w3.eth.contract(Web3.to_checksum_address(t), abi=ERC20_ABI) for t in tokens}
    decimals = {t: c.functions.decimals().call() for t, c in erc20.items()}
    symbols = {}
    for t, c in erc20.items():
        try:
            symbols[t] = c.functions.symbol().call()
        except Exception:
            symbols[t] = t[:8]
    prices = fetch_prices(tokens)

    def usd(token, amount):
        return amount / 10 ** decimals[token] * prices.get(token, 0)

    pools = []
    for g, supply, ours, t0, t1, programs in raw:
        fee_lookback_usd = sum(usd(t, a) for t, a in fees.get(g["vrm"].lower(), {}).items())
        fees_usd = fee_lookback_usd / fee_lookback_hours * horizon_hours
        incentives = []
        for token, amount, _rate, start, end, _creator in programs:
            token = token.lower()
            overlap = min(end, horizon_end) - max(start, now)
            value = usd(token, amount) * overlap / (end - start)
            incentives.append({"token": token, "symbol": symbols[token], "amount_token": amount / 10 ** decimals[token],
                               "start": start, "end": end, "amount_usd": value})
        incentives_usd = sum(i["amount_usd"] for i in incentives)
        pools.append({
            "pool": g["pool"],
            "gauge": g["gauge"],
            "symbol": f"{symbols[t0]}/{symbols[t1]}",
            "fees_usd": fees_usd,
            "incentives_usd": incentives_usd,
            "incentives": incentives,
            "total_usd": fees_usd + incentives_usd,
            # Floored at 1 AP so the closed-form solver can price empty pools
            "others_ap": max(supply - ours, 1.0),
            "allocated_total": supply,
            "our_ap": ours,
        })
    pools.sort(key=lambda p: p["total_usd"], reverse=True)

    dashboard = {
        "timestamp": now,
        "horizon_hours": horizon_hours,
        "fee_lookback_hours": fee_lookback_hours,
        "token_id": token_id,
        "our_ap": our_power,
        "our_ap_raw": str(our_power_raw),
        "max_gauges": max_gauges,
        "total_ap": sum(p["allocated_total"] for p in pools),
        "pools": pools,
    }
    save_json(dashboard, DASHBOARD_PATH)
    logger.info(f"Saved {len(pools)} pools to {DASHBOARD_PATH}; "
                f"${sum(p['total_usd'] for p in pools):,.0f} revenue over next {horizon_hours}h")
    return dashboard
