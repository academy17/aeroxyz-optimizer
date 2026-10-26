# aeroxyz-optimizer

Allocates sAERO Allocation Power (AP) across aero.xyz gauges on Base to maximize the fees and incentives earned over the 47h allocation cooldown.

## Setup

Python 3 (developed on 3.13). Run everything from the repo root, since `abi/` and `data/` are relative paths.

```
pip install -r requirements.txt
cp .env.example .env   # then fill it in
```

| Variable | Required | Notes |
|---|---|---|
| `RPC_URL` | Yes | Base mainnet RPC. `fetch` scans event logs in 9,000-block chunks, so a keyed provider (Alchemy, QuickNode) is safer than a free public endpoint. |
| `OWNER_ADDRESS` | Yes, unless `AEROXYZ_TOKEN_ID` is set | Wallet that owns the sAERO stake. The example value is pre-filled; change it to the wallet that owns the stake. |
| `AEROXYZ_TOKEN_ID` | No | sAERO stake id. Defaults to the first stake owned by `OWNER_ADDRESS`. Set it if the wallet has more than one. |

The tool is read-only. It needs no private key and never sends a transaction.

## Usage

```
python manager.py fetch                   # live pools -> data/dashboard.json (~8 min)
python manager.py optimize                # best maxGauges allocation -> data/allocation.json
python manager.py optimize --pools 0x...  # restrict to chosen pools
```

Run `fetch` first, then `optimize`. `optimize` works offline from `data/dashboard.json`, so you can rerun it with different options without refetching. Refetch before acting, since the data goes stale.

`fetch` also takes `--horizon-hours` (default 47) and `--fee-lookback-hours` (default 48). `optimize` also takes `--max-pools` to override the on-chain gauge cap.

Outputs:

- `data/allocation.json`: the allocation per pool, with expected USD.
- `data/allocation_bot.txt`: `gauge allocated_wei` lines for `Voter.allocateGauges`. Submit these yourself; the tool does not.

## Model

For each active gauge:

- **R** (pool revenue over the horizon H, default 47h) = trailing-48h `NotifyFeesAmount` rate × H + the USD value of every incentive program overlapping `[now, now+H]`.
- **W** = `VotingRewardsManager.totalSupply()` minus our current allocation.

Our expected return is Σ R·x/(W+x), with Σx = our AP and at most `maxGauges` (20) pools. `optimizer.py` solves it in two parts:

1. **Split for a fixed pool set.** The return has diminishing returns in x, so the optimum is closed form: `x = sqrt(R·W/λ) − W`, where the multiplier λ is bisected until the amounts sum to our AP (a Lagrangian / water-filling solution).
2. **Choosing the set.** The gauge cap makes this combinatorial. Start from the top `maxGauges` pools of the uncapped solution, then run a 1-swap local search over the top 80 candidates, keeping a swap if it adds more than $0.50. This is a heuristic and is not guaranteed to find the global best set.

## Limitations

- Fees are the trailing-48h rate extrapolated forward, so a recent spike or drop skews the estimate.
- It assumes other allocators' AP stays fixed during the lock.

## Notes

- Incentive program ids are 1-based; id 0 is an empty placeholder.
- Prices come from DefiLlama. Tokens without a price count as $0.
- Only Base is fetched. Other chains (e.g. Robinhood Chain) are not covered.
- `data/gauges.json` caches `GaugeCreated` logs so a first run doesn't rescan from the deploy block.
