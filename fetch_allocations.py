#!/usr/bin/env python3
"""
Aero (aero.xyz) allocation dashboard fetcher.

aero.xyz has no epochs and no relays: Allocation Power (AP) on a gauge earns, per second,
its pro-rata share of the pool's Exchange Revenue (fees) and of every live Incentive Program.
An allocation is locked for the cooldown (47h), so the value of allocating to a pool is the
revenue it streams over the next `horizon_hours`:

    R = fee_rate_usd * H  +  sum(incentive_usd_per_sec * overlap([start, end], [now, now + H]))
    W = VotingRewardsManager.totalSupply() - our allocation on that gauge

Every chain runs its own LeafVoter (same address everywhere) with its own gauges and maxGauges.
The stake lives on Base and its AP is split across chains by the root Voter, so pools from all
chains compete for the same AP.
"""
import os
import json
import logging
import requests
from web3 import Web3

logger = logging.getLogger(__name__)

LEAF_VOTER = "0x382409fad8503FbCdbB27A4211551fa03e884078"
VOTING_ESCROW = "0xBF7977E1521e9a3032bf7243Ee4712f311f13915"  # Base
BASE = 8453
LOG_CHUNK = 9000

# chain id: (name, Ankr slug or None if Ankr doesn't serve it, DefiLlama price prefix)
CHAINS = {
    8453: ("Base", "base", "base"),
    1: ("Ethereum", "eth", "ethereum"),
    10: ("OP", "optimism", "optimism"),
    42161: ("Arbitrum", "arbitrum", "arbitrum"),
    57073: ("Ink", None, "ink"),
    4663: ("Robinhood", "robinhood_mainnet", "robinhood"),
    5042: ("Arc", None, "arc"),
}

GAUGES_CACHE = "data/gauges_{}.json"
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


def rpc_url(chain_id):
    """RPC_URL_<chain id>, else RPC_URL with {chain} swapped for the Ankr slug. A plain RPC_URL serves Base only."""
    template, slug = os.getenv("RPC_URL", ""), CHAINS[chain_id][1]
    if os.getenv(f"RPC_URL_{chain_id}"):
        return os.getenv(f"RPC_URL_{chain_id}")
    if slug and "{chain}" in template:
        return template.replace("{chain}", slug)
    return template if chain_id == BASE else None


def get_logs(w3, params, from_block, to_block):
    logs, b = [], from_block
    while b <= to_block:
        end = min(b + LOG_CHUNK, to_block)
        logs += w3.eth.get_logs({**params, "fromBlock": b, "toBlock": end})
        b = end + 1
    return logs


def deploy_block(w3, address, head):
    """First block with code at `address` (needs an archive node)."""
    lo, hi = 0, head
    while lo < hi:
        mid = (lo + hi) // 2
        if w3.eth.get_code(address, block_identifier=mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def fetch_gauges(w3, chain_id, gauge_manager, head):
    """All gauges from GaugeManager.GaugeCreated, cached per chain and extended incrementally."""
    path = GAUGES_CACHE.format(chain_id)
    if os.path.exists(path):
        with open(path) as f:
            cache = json.load(f)
    else:
        cache = {"last_block": deploy_block(w3, gauge_manager, head) - 1, "gauges": []}
    gm = w3.eth.contract(gauge_manager, abi=load_abi("GaugeManager"))
    event = gm.events.GaugeCreated()
    params = {"address": gauge_manager, "topics": [event.topic]}
    for log in get_logs(w3, params, cache["last_block"] + 1, head):
        a = event.process_log(log).args
        cache["gauges"].append({"pool": a._target.lower(), "gauge": a._gauge, "vrm": a._votingRewardsManager})
    cache["last_block"] = head
    save_json(cache, path)
    return cache["gauges"]


def fetch_fee_amounts(w3, vrms, head, lookback_hours):
    """Sum NotifyFeesAmount (fees credited to allocators) per VRM per token over the lookback."""
    vrm_set = {v.lower() for v in vrms}
    event = w3.eth.contract(abi=load_abi("VotingRewardsManager")).events.NotifyFeesAmount()
    block_time = (w3.eth.get_block(head)["timestamp"] - w3.eth.get_block(head - 1000)["timestamp"]) / 1000
    from_block = head - int(lookback_hours * 3600 / block_time)
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


def fetch_prices(tokens, prefix):
    """USD prices from DefiLlama, keyed by lowercase address."""
    prices, tokens = {}, sorted(tokens)
    for i in range(0, len(tokens), 100):
        keys = ",".join(f"{prefix}:{t}" for t in tokens[i:i + 100])
        try:
            coins = requests.get(f"https://coins.llama.fi/prices/current/{keys}", timeout=30).json()["coins"]
            prices.update({k.split(":")[1].lower(): v["price"] for k, v in coins.items()})
        except Exception as e:
            logger.warning(f"DefiLlama price error: {e}")
    return prices


def fetch_chain(chain_id, w3, token_id, horizon_hours, fee_lookback_hours):
    """Pools of one chain with revenue over the horizon. Returns (pools, maxGauges, chain time)."""
    name, _, llama = CHAINS[chain_id]
    if w3.eth.chain_id != chain_id:
        raise ValueError(f"RPC is chain {w3.eth.chain_id}, expected {chain_id}")
    if not w3.eth.get_code(LEAF_VOTER):
        raise ValueError(f"no LeafVoter at {LEAF_VOTER}")
    head = w3.eth.block_number
    now = w3.eth.get_block(head)["timestamp"]
    horizon_end = now + horizon_hours * 3600

    lv = w3.eth.contract(LEAF_VOTER, abi=load_abi("LeafVoter"))
    vrm_abi = load_abi("VotingRewardsManager")
    max_gauges = lv.functions.maxGauges().call()

    gauges = fetch_gauges(w3, chain_id, lv.functions.GAUGE_MANAGER().call(), head)
    logger.info(f"{name}: {len(gauges)} gauges, max {max_gauges}")
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
    prices = fetch_prices(tokens, llama)
    if len(prices) < len(tokens):
        logger.warning(f"{name}: {len(tokens) - len(prices)}/{len(tokens)} tokens have no price (count as $0)")

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
            "chain_id": chain_id,
            "chain": name,
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
    return pools, max_gauges, now


def run_fetch(horizon_hours=47, fee_lookback_hours=48, chains=None):
    """`chains`: lowercase chain names to fetch, default all. Chains without a working RPC are skipped."""
    base_url = rpc_url(BASE)
    if not base_url:
        raise SystemExit("RPC_URL is not set")
    base = Web3(Web3.HTTPProvider(base_url, request_kwargs={"timeout": 60}))
    ve = base.eth.contract(VOTING_ESCROW, abi=load_abi("VotingEscrow"))
    token_id = os.getenv("AEROXYZ_TOKEN_ID")
    if token_id:
        token_id = int(token_id)
    else:
        token_id = ve.functions.tokenOfOwnerByIndex(Web3.to_checksum_address(os.getenv("OWNER_ADDRESS")), 0).call()
    our_power_raw = ve.functions.balanceOfNFT(token_id).call()
    our_power = our_power_raw / 1e18
    logger.info(f"Stake #{token_id}: {our_power:,.0f} AP")

    pools, meta, now = [], {}, 0
    for chain_id, (name, _, _) in CHAINS.items():
        if chains and name.lower() not in chains:
            continue
        url = rpc_url(chain_id)
        if not url:
            logger.warning(f"{name}: skipped, no RPC (put {{chain}} in RPC_URL or set RPC_URL_{chain_id})")
            continue
        try:
            w3 = base if chain_id == BASE else Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
            chain_pools, max_gauges, chain_now = fetch_chain(chain_id, w3, token_id, horizon_hours, fee_lookback_hours)
        except Exception as e:
            logger.warning(f"{name}: skipped, {type(e).__name__}: {str(e).replace(url, '<rpc>')}")
            continue
        pools += chain_pools
        meta[chain_id] = {"name": name, "max_gauges": max_gauges}
        now = max(now, chain_now)
    pools.sort(key=lambda p: p["total_usd"], reverse=True)

    dashboard = {
        "timestamp": now,
        "horizon_hours": horizon_hours,
        "fee_lookback_hours": fee_lookback_hours,
        "token_id": token_id,
        "our_ap": our_power,
        "our_ap_raw": str(our_power_raw),
        "chains": meta,
        "total_ap": sum(p["allocated_total"] for p in pools),
        "pools": pools,
    }
    save_json(dashboard, DASHBOARD_PATH)
    logger.info(f"Saved {len(pools)} pools on {', '.join(m['name'] for m in meta.values()) or 'no chains'} to {DASHBOARD_PATH}; "
                f"${sum(p['total_usd'] for p in pools):,.0f} revenue over next {horizon_hours}h")
    return dashboard
