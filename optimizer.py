"""
Best allocation of our AP across pools on all chains, at most K[chain] gauges per chain
(K = that chain's on-chain maxGauges). Pools are keyed (chain_id, pool address).

Our share of pool i over the horizon is R_i * x_i / (W_i + x_i), where R_i is the pool's revenue
and W_i is everyone else's AP. For a fixed pool set the optimum is closed form:
    x_i = sqrt(R_i * W_i / lam) - W_i,  with lam bisected so sum x_i = P
The gauge cap makes set choice combinatorial, so we start from each chain's K largest pools in
the uncapped solution and swap pools in/out of the set (within a chain) while it improves.
"""
import math
from collections import Counter

CANDIDATES = 80


def solve(pools, S, P):
    """Optimal x over pool set S with total AP P. Returns (value, {pool: x})."""
    lo, hi = 1e-18, 1e6
    for _ in range(200):
        lam = math.sqrt(lo * hi)
        x = {i: max(math.sqrt(pools[i]["R"] * pools[i]["W"] / lam) - pools[i]["W"], 0) for i in S}
        lo, hi = (lam, hi) if sum(x.values()) > P else (lo, lam)
    return value(pools, x), x


def value(pools, x):
    return sum(pools[i]["R"] * xi / (pools[i]["W"] + xi) for i, xi in x.items())


def best_set(pools, P, K):
    _, full = solve(pools, set(pools), P)
    cands = {}
    for i in sorted(pools, key=lambda i: -full[i]):
        cands.setdefault(i[0], []).append(i)
    cands = {c: l[:CANDIDATES] for c, l in cands.items()}
    S = {i for c, l in cands.items() for i in l[:K[c]]}
    best, _ = solve(pools, S, P)
    improved = True
    while improved:
        improved = False
        for out in sorted(S):  # fixed order: set order varies per run and lands on different local optima
            for inn in cands[out[0]]:
                if inn in S:
                    continue
                v, _ = solve(pools, S - {out} | {inn}, P)
                if v > best + 0.5:
                    best, S, improved = v, S - {out} | {inn}, True
                    break
            if improved:
                break
    return solve(pools, S, P)[1]


def round_pcts(x):
    """Integer percentages summing to 100 (largest remainder)."""
    total = sum(x.values())
    raw = {i: 100 * xi / total for i, xi in x.items()}
    pct = {i: int(r) for i, r in raw.items()}
    for i in sorted(raw, key=lambda i: pct[i] - raw[i])[:100 - sum(pct.values())]:
        pct[i] += 1
    return pct


def optimize(dashboard, max_pools=None, only=None):
    """Allocation rows for the dashboard; `only` restricts to a set of pool addresses."""
    P = dashboard["our_ap"]
    K = {int(c): max_pools or m["max_gauges"] for c, m in dashboard["chains"].items()}
    pools = {(p["chain_id"], p["pool"]): {**p, "R": p["total_usd"], "W": p["others_ap"]}
             for p in dashboard["pools"] if p["total_usd"] > 0 and (only is None or p["pool"] in only)}
    per_chain = Counter(c for c, _ in pools)
    x = best_set(pools, P, K) if any(n > K[c] for c, n in per_chain.items()) else solve(pools, set(pools), P)[1]
    x = {i: xi for i, xi in x.items() if xi > 0}
    pct = round_pcts(x)
    balance = int(dashboard["our_ap_raw"])
    rows, chains = [], {}
    for i in sorted(x, key=lambda i: -x[i]):
        p = pools[i]
        wei = balance * pct[i] // 100
        rows.append({
            "chain": p["chain"], "chain_id": i[0], "symbol": p["symbol"], "pool": i[1], "gauge": p["gauge"], "pct": pct[i],
            "allocated_wei": str(wei),
            "fees_usd": round(p["fees_usd"]), "incentives_usd": round(p["incentives_usd"]),
            "others_ap": round(p["W"]), "exp_usd": round(p["R"] * x[i] / (p["W"] + x[i]), 2),
        })
        c = chains.setdefault(i[0], {"chain": p["chain"], "pct": 0, "allocated_wei": 0})
        c["pct"] += pct[i]
        c["allocated_wei"] += wei
    return {
        "timestamp": dashboard["timestamp"],
        "horizon_hours": dashboard["horizon_hours"],
        "token_id": dashboard["token_id"],
        "exp_usd": round(value(pools, x)),
        "chains": [{**c, "chain_id": cid, "allocated_wei": str(c["allocated_wei"])} for cid, c in chains.items()],
        "allocations": rows,
    }
