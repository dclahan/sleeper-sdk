"""Keeper-surplus model — pure logic, no I/O.

A keeper's value is not what he is worth on the market, it is the gap between
the draft round his production deserves and the round he costs to keep:

    surplus = keeper_cost_round - implied_round        (rounds; higher = better)

`implied_round` comes from ranking every player by points above a replacement
starter at his position (VORP) and counting 12 players per round. Cost comes
from the league's keeper rules (see `KeeperRules`).

Everything here is deliberately simple and parameterised, because the inputs
are noisy (a handful of games) and the rules are league-specific.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


@dataclass(frozen=True)
class KeeperRules:
    """Cadre League defaults. Override via keeper/rules.json."""

    max_keepers: int = 3
    round_discount: int = 3          # keeper cost = last year's round - 3
    ww_first_round: int = 13         # waiver adds are kept at round 13, 14, 15
    ww_last_round: int = 15
    # Does a player who was drafted, dropped and re-added by waiver cost the
    # waiver round (True) or his original draft round (False)? Unconfirmed
    # league rule — rows where it matters are flagged.
    readd_resets_cost: bool = True


@dataclass(frozen=True)
class Source:
    """How a player reached his current roster, for keeper-cost purposes."""

    kind: str                        # "draft" | "waiver" | "unknown"
    draft_round: Optional[int] = None
    readded: bool = False            # drafted earlier, then re-added off waivers


def keeper_cost(source: Source, rules: KeeperRules = KeeperRules()) -> Optional[int]:
    """Round it costs to keep this player, or None if he cannot be kept.

    Rounds 1-3 are not keepable: a pick in round r costs r - 3, and a cost
    below round 1 does not exist. Waiver adds cost the first waiver round.
    """
    if source.kind == "draft" and source.draft_round is not None:
        if source.readded and rules.readd_resets_cost:
            return rules.ww_first_round
        cost = source.draft_round - rules.round_discount
        return cost if cost >= 1 else None
    return rules.ww_first_round


def replacement_ranks(
    roster_positions: Sequence[str],
    num_teams: int,
    flex_shares: Mapping[str, float] = {"RB": 0.5, "WR": 0.5},
    buffer: Mapping[str, int] = {"QB": 2, "TE": 2},
) -> dict[str, int]:
    """Positional rank of the replacement-level player (the last 'startable').

    Starters × teams, plus each position's share of the FLEX slots, plus a
    small buffer for the positions where managers hold a spare. For a
    12-team 1QB/2RB/3WR/1TE/1FLEX league: QB14, RB30, WR42, TE14.
    """
    counts = {p: sum(1 for s in roster_positions if s == p) for p in SKILL_POSITIONS}
    flex = sum(1 for s in roster_positions if s in ("FLEX", "REC_FLEX", "WRRB_FLEX"))
    ranks: dict[str, int] = {}
    for p in SKILL_POSITIONS:
        ranks[p] = (
            counts[p] * num_teams
            + int(round(flex * num_teams * flex_shares.get(p, 0.0)))
            + buffer.get(p, 0)
        )
        ranks[p] = max(ranks[p], 1)
    return ranks


def recency_weights(n_games: int, decay: float) -> list[float]:
    """Oldest game first. decay=1.0 weighs every game equally."""
    return [decay ** (n_games - 1 - i) for i in range(n_games)]


def shrunk_ppg(
    game_points: Sequence[float],
    prior: float,
    prior_games: float = 2.0,
    decay: float = 0.9,
) -> float:
    """Recency-weighted points per game, pulled toward `prior`.

    The prior counts as `prior_games` worth of weight, so a 3-game hot streak
    is discounted far more than a 12-game sample. As games accumulate the
    prior washes out, which is how the signal gets stronger week to week.
    """
    if not game_points:
        return prior
    w = recency_weights(len(game_points), decay)
    num = sum(wi * p for wi, p in zip(w, game_points)) + prior_games * prior
    return num / (sum(w) + prior_games)


@dataclass
class PlayerRow:
    player_id: str
    name: str
    position: str
    owner: str                       # display name, or "FA"
    games: int
    ppg: float                       # raw mean
    value_ppg: float                 # shrunk, recency-weighted
    source: Source
    cost: Optional[int] = None
    vorp: float = 0.0
    overall_rank: int = 0
    implied_round: int = 0
    surplus: Optional[int] = None
    flags: list[str] = field(default_factory=list)


def replacement_levels(
    ppg_by_pos: Mapping[str, Sequence[float]], ranks: Mapping[str, int]
) -> dict[str, float]:
    """PPG of the replacement-rank player at each position (best-first lists)."""
    out: dict[str, float] = {}
    for pos, vals in ppg_by_pos.items():
        ordered = sorted(vals, reverse=True)
        if not ordered:
            out[pos] = 0.0
            continue
        idx = min(ranks.get(pos, len(ordered)), len(ordered)) - 1
        out[pos] = ordered[idx]
    return out


def score_rows(
    rows: list[PlayerRow],
    levels: Mapping[str, float],
    num_teams: int,
    rules: KeeperRules = KeeperRules(),
    pos_weight: Mapping[str, float] = {},
) -> list[PlayerRow]:
    """Fill vorp / overall_rank / implied_round / cost / surplus in place.

    `pos_weight` scales a position's VORP before ranking. In a 1QB league
    QB VORP is inflated (only one starts), so the default config discounts it.
    """
    for r in rows:
        r.vorp = (r.value_ppg - levels.get(r.position, 0.0)) * pos_weight.get(r.position, 1.0)
    ranked = sorted(rows, key=lambda r: -r.vorp)
    for i, r in enumerate(ranked, 1):
        r.overall_rank = i
        r.implied_round = math.ceil(i / num_teams)
    for r in rows:
        r.cost = keeper_cost(r.source, rules)
        if r.cost is None or r.vorp <= 0:
            # Ineligible, or below replacement: no keeper slot is worth spending.
            r.surplus = None
        else:
            r.surplus = r.cost - r.implied_round
        if r.source.readded and r.source.draft_round is not None:
            r.flags.append("re-added: cost rule unconfirmed")
        if r.source.kind == "unknown":
            r.flags.append("no draft/waiver record: assumed waiver cost")
    return rows


MIN_VORP_PPG = 1.0  # below this the player is a free replacement, not a keeper


def keepable(rows: Iterable[PlayerRow], min_cost: int = 1, min_surplus: Optional[int] = None) -> list[PlayerRow]:
    """Eligible players at or past `min_cost`, clearly above replacement, best first.

    `min_surplus=1` keeps only players who are a genuine bargain at their price.
    """
    out = [
        r for r in rows
        if r.cost is not None and r.cost >= min_cost and r.surplus is not None
        and _clearly_above_replacement(r)
        and (min_surplus is None or r.surplus >= min_surplus)
    ]
    return sorted(out, key=lambda r: (-(r.surplus or 0), r.overall_rank))


def _clearly_above_replacement(r: PlayerRow) -> bool:
    return r.vorp >= MIN_VORP_PPG


def excess_keepers(
    rows: Iterable[PlayerRow], max_keepers: int, min_surplus: int = 2
) -> dict[str, list[PlayerRow]]:
    """Owners with more good keepers than keeper slots -> the surplus ones.

    The players beyond an owner's top `max_keepers` by surplus are the ones he
    cannot protect, which makes them the cheapest to trade for.
    """
    by_owner: dict[str, list[PlayerRow]] = {}
    for r in rows:
        if r.owner != "FA" and r.surplus is not None and r.surplus >= min_surplus and _clearly_above_replacement(r):
            by_owner.setdefault(r.owner, []).append(r)
    out: dict[str, list[PlayerRow]] = {}
    for owner, rs in by_owner.items():
        rs.sort(key=lambda r: (-(r.surplus or 0), r.overall_rank))
        if len(rs) > max_keepers:
            out[owner] = rs[max_keepers:]
    return out


def simulate_playoffs(
    wins: Mapping[int, float],
    points_for: Mapping[int, float],
    mean_pts: Mapping[int, float],
    sd: float,
    schedule: Sequence[Sequence[tuple[int, int]]],
    playoff_teams: int,
    target: int,
    boost: float = 0.0,
    n: int = 20000,
    seed: int = 0,
) -> tuple[float, float]:
    """Monte Carlo of the rest of the regular season.

    `schedule` is a list of weeks, each a list of (roster_a, roster_b) pairs.
    Weekly scores are Normal(mean, sd); `boost` adds to `target`'s mean.
    Seeds by wins then points for. Returns (P(make playoffs), expected wins).
    """
    rng = random.Random(seed)
    made = 0
    tot_wins = 0.0
    for _ in range(n):
        w = dict(wins)
        pf = dict(points_for)
        for week in schedule:
            for a, b in week:
                ma = mean_pts[a] + (boost if a == target else 0.0)
                mb = mean_pts[b] + (boost if b == target else 0.0)
                sa, sb = rng.gauss(ma, sd), rng.gauss(mb, sd)
                pf[a] += sa
                pf[b] += sb
                if sa > sb:
                    w[a] += 1
                elif sb > sa:
                    w[b] += 1
                else:
                    w[a] += 0.5
                    w[b] += 0.5
        order = sorted(w, key=lambda k: (-w[k], -pf[k]))
        made += order.index(target) < playoff_teams
        tot_wins += w[target]
    return made / n, tot_wins / n
