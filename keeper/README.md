# keeper/ — weekly keeper-league analysis

Self-contained tooling for a **non-dynasty keeper league** (built for the
Cadre League: half-PPR, 1QB, keep 3). The rest of this repo prices players for
*dynasty* with KTC. That is the wrong lens for a keeper league, where what
matters is the **draft round a player costs to keep versus the round his
production deserves**. This folder models that.

## Run it

From the repo root, once a week (after Tuesday's stats settle):

```bash
python3 keeper/run.py dolanc24football --league "Cadre League"
```

No install needed (it imports the SDK from `python/src`) and it is read-only:
it never uses `SLEEPER_TOKEN` and cannot change anything in Sleeper.

| Flag | Purpose |
|---|---|
| `--top N` | rows per section (default 20) |
| `--min-cost R` | trade-target/waiver floor, default 5 — hides players you'd have to spend an early round to keep |
| `--boost N` | what-if: add N points/week to your scoring in the playoff simulation |
| `--rules PATH` | alternate rules file (default `keeper/rules.json`) |
| `--no-snapshot` | don't write today's snapshot |

## What it prints

1. **Your keepers** — every player on your roster by surplus; ★ marks the best three with positive surplus.
2. **Waiver adds** — free agents ranked by surplus at the round-13 waiver cost.
3. **Trade targets** — other rosters' players with cheap keeper cost and real production. `◆` marks players on teams with more good keepers than keeper slots; their extras are the cheapest to buy.
4. **Playoff odds** — Monte Carlo over the *real* remaining schedule.

## How the number works

```
surplus = keeper_cost_round − implied_round         (higher = better; +10 means a round-1 player costs round 11)
```

- **Cost** (from `rules.json`): drafted in round r → keeper cost `r − 3`; rounds 1–3 can't be kept; waiver adds cost round 13. Trades keep the original draft round. The history is replayed from the league's draft and transactions, not guessed.
- **Production**: your league's real scoring applied to Sleeper's weekly stat lines (the header prints a check against Sleeper's own `pts_half_ppr`; it should be ~0).
- **Implied round**: rank everyone by points above a replacement starter (QB14, RB30, WR42, TE14 for this league, derived from roster settings), 12 players per round. QB value is discounted ×0.6 in a 1QB league.
- **Signal strengthens over the season**: PPG is recency-weighted (0.9/game) and shrunk toward replacement level by 2 games' worth. At 3 games a hot streak is heavily discounted; by week 12 the shrinkage is nearly gone. Players need ≥3 games.
- **Δ column**: each run saves `keeper/snapshots/<date>_w<week>.json`; the next run shows how every surplus moved. Rising surplus is the signal to buy, falling is the signal to sell.

## Known limits (read before trusting a number)

- **Unconfirmed league rule**: a player drafted, dropped, then re-added off waivers is assumed to reset to the waiver round (`readd_resets_cost` in `rules.json`). Rows it affects are flagged. If your commissioner rules otherwise, flip it.
- **Waiver keepers cost r13, r14, r15 in order**, but surplus is computed at r13 for all of them. A second or third waiver keeper is a little less valuable than shown.
- **Replacement-level choices** (and the QB discount) are judgment calls; change them in `keeper_model.py` / `rules.json`.
- **Noise**: with <8 games treat ±1–2 rounds as a tie.
- This ignores injuries, byes ahead, and schedule; it is a keeper-value screen, not a start/sit tool. For weekly lineups use the SDK's `lineup` / `start-sit` commands.

## Files

| File | Role |
|---|---|
| `run.py` | fetch → compute → print → snapshot |
| `keeper_model.py` | pure logic: cost, replacement, shrinkage, surplus, excess-keeper detection, playoff sim |
| `rules.json` | league rules and tuning knobs |
| `tests/` | 19 property tests — `python3 -m pytest keeper/tests -q` |
| `snapshots/` | dated outputs for the Δ column (commit them or not) |
