# aeroxyz-optimizer

Allocates sAERO Allocation Power (AP) across aero.xyz gauges on every Aero chain (Base, Ethereum, OP, Arbitrum, Ink, Robinhood Chain, Arc) to maximize the fees and incentives earned over the 47h allocation cooldown.

## Setup

Python 3 (developed on 3.13). Run everything from the repo root, since `abi/` and `data/` are relative paths.

```
pip install -r requirements.txt
cp .env.example .env   # then fill it in
```

| Variable | Required | Notes |
|---|---|---|
| `RPC_URL` | Yes | RPC for every chain, with `{chain}` where the chain goes, e.g. `https://rpc.ankr.com/{chain}/KEY`. It is filled with the Ankr slug (`base`, `eth`, `optimism`, `arbitrum`, `robinhood_mainnet`). A URL without `{chain}` serves Base only. `fetch` scans event logs in 9,000-block chunks, so use a paid or keyed plan rather than a free public endpoint. |
| `RPC_URL_<chain id>` | No | Overrides `RPC_URL` for one chain. Needed for Ink (`RPC_URL_57073`) and Arc (`RPC_URL_5042`), which Ankr doesn't serve. |
| `OWNER_ADDRESS` | Yes, unless `AEROXYZ_TOKEN_ID` is set | Wallet that owns the sAERO stake. The example value is pre-filled; change it to the wallet that owns the stake. |
| `AEROXYZ_TOKEN_ID` | No | sAERO stake id. Defaults to the first stake owned by `OWNER_ADDRESS`. Set it if the wallet has more than one. |

The tool is read-only. It needs no private key and never sends a transaction.

## Usage

```
python manager.py fetch                   # live pools on all chains -> data/dashboard.json (~5-10 min)
python manager.py fetch --chains base arbitrum   # only these chains
python manager.py optimize                # best allocation -> data/allocation.json
python manager.py optimize --pools 0x...  # restrict to chosen pools
```

Run `fetch` first, then `optimize`. `optimize` works offline from `data/dashboard.json`, so you can rerun it with different options without refetching. Refetch before acting, since the data goes stale.

A chain with no RPC, or one the RPC refuses, is skipped with a warning and left out of the allocation. Check the `fetch` log and the `Saved ... pools on ...` line to see which chains were covered.

`fetch` also takes `--horizon-hours` (default 47) and `--fee-lookback-hours` (default 48). `optimize` also takes `--max-pools` to override the on-chain gauge cap (per chain), `--shrink` (default 0.25, 0 turns it off) and `--max-share` (default 25, the most % of AP on one pool; 100 turns it off). See [Model](#model).

Outputs:

- `data/allocation.csv`: the same allocation as a spreadsheet to share, one row per pool plus a total. Pools rounded down to 0% are left out, so its total is slightly under the logged expected value.
- `data/allocation.json`: the allocation per pool and per chain, with expected USD.
- `data/allocation_bot.txt`: `gauge allocated_wei chain_id` lines for `Voter.allocateGauges`, one call per chain id. Submit these yourself; the tool does not. It does not output the cross-chain moves (`allocateChains`) needed to shift AP between chains.

## Model

For each active gauge:

- **R** (pool revenue over the horizon H, default 47h) = trailing-48h `NotifyFeesAmount` rate × H + the USD value of every incentive program overlapping `[now, now+H]`.
- **W** = `VotingRewardsManager.totalSupply()` minus our current allocation.

Pools from all chains compete for the same AP. Our expected return is Σ R·x/(W+x), with Σx = our AP and at most `maxGauges` (20) pools per chain, since each chain has its own `LeafVoter`. `optimizer.py` solves it in two parts:

1. **Split for a fixed pool set.** The return has diminishing returns in x, so the optimum is closed form: `x = sqrt(R·W/λ) − W`, clipped to `[0, cap]`, where the multiplier λ is bisected until the amounts sum to our AP (a Lagrangian / water-filling solution).
2. **Choosing the set.** The gauge cap makes this combinatorial. Start from each chain's top `maxGauges` pools of the uncapped solution, then run a 1-swap local search within each chain over its top 80 candidates, keeping a swap if it adds more than $0.50. This is a heuristic and is not guaranteed to find the global best set.

Other voters pile into high-yield pools during the lock, so a pool's yield at fetch time overstates what it pays over 47h. Two guards handle this:

- **Shrink (`--shrink`, default 0.25).** The market rate is ρ = ΣR / ΣW over every pool with revenue. A pool above it, with yield y = R/W, is planned at y′ = ρ + (1 − s)(y − ρ), by raising its W to R/y′. With s = 0.25, a quarter of each pool's excess over the market is assumed to be competed away. Pools at or below the market rate are unchanged.
- **Cap (`--max-share`, default 25%).** No pool gets more than this share of our AP. If there are fewer pools than 1/cap, the AP is split evenly.

Expected USD in the outputs uses the shrunk W. The "Other AP on pool" column is the live figure.

## Limitations

- Fees are the trailing-48h rate extrapolated forward, so a recent spike or drop skews the estimate.
- Other allocators' pile-in is modelled only by the flat shrink. It does not depend on how fast a pool is filling up. The shrink and cap defaults come from launch-week data (Oct 24–28, Base and Arbitrum), and should be rechecked as voting matures.

## Notes

- Incentive program ids are 1-based; id 0 is an empty placeholder.
- Prices come from DefiLlama. Tokens without a price count as $0.
- `LeafVoter` is at the same address on every chain; each chain's `GaugeManager` is read from it. The first fetch of a chain finds the `GaugeManager` deploy block by bisecting `eth_getCode`, so it needs an archive node.
- The stake lives on Base, and the root `Voter` (`0x49d7d4f17Ec56Ef8fA04E464AF5D1A4942CC31e6`) splits its AP across chains. The tool reads the stake from Base and each chain's current allocation from its `LeafVoter`.
- DefiLlama price prefixes for Robinhood Chain and Arc are untested guesses. If `fetch` warns about many unpriced tokens on a chain, its prices are missing and its pools look worthless.
- `data/gauges_<chain id>.json` caches `GaugeCreated` logs so a rerun doesn't rescan from the deploy block.
