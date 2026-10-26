"""
Best allocation of our AP across at most K gauges (K = on-chain maxGauges).

Our share of pool i over the horizon is R_i * x_i / (W_i + x_i), where R_i is the pool's revenue
and W_i is everyone else's AP. For a fixed pool set the optimum is closed form:
    x_i = sqrt(R_i * W_i / lam) - W_i,  with lam bisected so sum x_i = P
The gauge cap makes set choice combinatorial, so we start from the K largest pools in the
uncapped solution and swap pools in/out of the set while it improves.
"""
import math

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
    cands = sorted(pools, key=lambda i: -full[i])[:CANDIDATES]
    S = set(cands[:K])
    best, _ = solve(pools, S, P)
    improved = True
    while improved:
        improved = False
        for out in list(S):
            for inn in cands:
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
    P, K = dashboard["our_ap"], max_pools or dashboard["max_gauges"]
    pools = {p["pool"]: {**p, "R": p["total_usd"], "W": p["others_ap"]}
             for p in dashboard["pools"] if p["total_usd"] > 0 and (only is None or p["pool"] in only)}
    x = best_set(pools, P, K) if len(pools) > K else solve(pools, set(pools), P)[1]
    x = {i: xi for i, xi in x.items() if xi > 0}
    pct = round_pcts(x)
    balance = int(dashboard["our_ap_raw"])
    rows = []
    for i in sorted(x, key=lambda i: -x[i]):
        p = pools[i]
        rows.append({
            "symbol": p["symbol"], "pool": i, "gauge": p["gauge"], "pct": pct[i],
            "allocated_wei": str(balance * pct[i] // 100),
            "fees_usd": round(p["fees_usd"]), "incentives_usd": round(p["incentives_usd"]),
            "others_ap": round(p["W"]), "exp_usd": round(p["R"] * x[i] / (p["W"] + x[i]), 2),
        })
    return {
        "timestamp": dashboard["timestamp"],
        "horizon_hours": dashboard["horizon_hours"],
        "token_id": dashboard["token_id"],
        "exp_usd": round(value(pools, x)),
        "allocations": rows,
    }
