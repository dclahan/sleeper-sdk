#!/usr/bin/env python3
"""Keeper-league weekly report.  See keeper/README.md.

    python3 keeper/run.py dolanc24football --league "Cadre League"

Run it from the repo root. It imports the SDK from python/src, so no install
is needed. Read-only: it never touches SLEEPER_TOKEN and never writes to Sleeper.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "python" / "src"))

from sleeper import SleeperClient  # noqa: E402
from sleeper.enrichment.projections import score_projection  # noqa: E402

import keeper_model as km  # noqa: E402

DEFAULTS: dict[str, Any] = {
    "decay": 0.9,            # recency weight per game back
    "prior_games": 2.0,      # shrinkage strength toward replacement level
    "min_games": 3,          # ignore players with fewer games than this
    "pos_weight": None,      # None = auto (QB 0.6 in single-QB leagues)
    "min_cost": 5,           # trade-target / waiver floor: don't want early-round keeps
}


def load_config(path: Path) -> tuple[km.KeeperRules, dict[str, Any]]:
    raw = json.loads(path.read_text()) if path.exists() else {}
    rule_keys = km.KeeperRules.__dataclass_fields__.keys()
    rules = km.KeeperRules(**{k: v for k, v in raw.items() if k in rule_keys})
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k not in rule_keys}}
    return rules, cfg


async def fetch(client: SleeperClient, user: str, league_name: str) -> dict[str, Any]:
    state = await client.state.get_state()
    season = str(state.season)
    u = await client.users.get_user(user)
    leagues = await client.users.get_user_leagues(u.user_id, season=season)
    league = next((lg for lg in leagues if lg.name.lower() == league_name.lower()), None)
    if league is None:
        names = ", ".join(lg.name for lg in leagues)
        raise SystemExit(f"No league '{league_name}' in {season}. You have: {names}")
    lid = league.league_id
    played = int(state.week) - 1
    if state.season_type != "regular" or played < 1:
        raise SystemExit(f"Needs at least one completed regular-season week (week={state.week}).")

    players = await client.players.get_all_players()
    rosters = await client.leagues.get_rosters(lid)
    users = {x.user_id: x.display_name for x in await client.leagues.get_users(lid)}

    draft_round: dict[str, int] = {}
    for d in await client.drafts.get_drafts_for_league(lid):
        for p in await client.drafts.get_picks(d.draft_id):
            if p.player_id:
                draft_round[p.player_id] = p.round

    txns = []
    for wk in range(0, int(state.week) + 1):
        try:
            txns += await client.leagues.get_transactions(lid, wk)
        except Exception:
            pass

    weekly: dict[str, list[float]] = {}
    pos_of: dict[str, str] = {}
    diffs: list[float] = []
    for wk in range(1, played + 1):
        for r in await client.projections.get_week_stats(season, wk, position=list(km.SKILL_POSITIONS)):
            st = r.stats or {}
            if st.get("gp", 0) < 1:
                continue
            pts = score_projection(r, league.scoring_settings).points
            weekly.setdefault(r.player_id, []).append(pts)
            if "pts_half_ppr" in st:
                diffs.append(abs(pts - st["pts_half_ppr"]))
    for pid in weekly:
        pl = players.get(pid)
        if pl and pl.position in km.SKILL_POSITIONS:
            pos_of[pid] = pl.position

    matchups: dict[int, list[Any]] = {}
    horizon = int(league.settings.get("playoff_week_start", 15))
    for wk in range(1, horizon):
        try:
            matchups[wk] = await client.leagues.get_matchups(lid, wk)
        except Exception:
            matchups[wk] = []

    return dict(
        league=league, state=state, played=played, players=players, rosters=rosters,
        users=users, draft_round=draft_round, txns=txns, weekly=weekly, pos_of=pos_of,
        scoring_diff=diffs, matchups=matchups, me=u.user_id, season=season,
    )


def acquisition_sources(txns: list[Any], draft_round: dict[str, int]) -> dict[str, km.Source]:
    """Replay history: draft sets the round, a waiver/FA add resets to waiver,
    a trade changes nothing (the player keeps his original draft round)."""
    src: dict[str, km.Source] = {
        pid: km.Source("draft", rnd) for pid, rnd in draft_round.items()
    }
    done = [t for t in txns if t.status == "complete" and t.type in ("waiver", "free_agent")]
    for t in sorted(done, key=lambda t: t.created or 0):
        for pid in (t.adds or {}):
            prev = src.get(pid)
            src[pid] = km.Source(
                "waiver",
                prev.draft_round if prev else None,
                readded=bool(prev and prev.kind == "draft"),
            )
    return src


def build_rows(d: dict[str, Any], rules: km.KeeperRules, cfg: dict[str, Any]) -> tuple[list[km.PlayerRow], dict[str, float]]:
    league = d["league"]
    n_teams = league.total_rosters
    ranks = km.replacement_ranks(league.roster_positions, n_teams)
    raw_ppg = {p: [] for p in km.SKILL_POSITIONS}
    for pid, pts in d["weekly"].items():
        if len(pts) >= cfg["min_games"] and pid in d["pos_of"]:
            raw_ppg[d["pos_of"][pid]].append(sum(pts) / len(pts))
    levels = km.replacement_levels(raw_ppg, ranks)

    owner = {}
    for r in d["rosters"]:
        for pid in r.players or []:
            owner[pid] = d["users"].get(r.owner_id, f"roster{r.roster_id}")
    sources = acquisition_sources(d["txns"], d["draft_round"])

    rows = []
    for pid, pts in d["weekly"].items():
        pos = d["pos_of"].get(pid)
        if pos is None or len(pts) < cfg["min_games"]:
            continue
        who = owner.get(pid, "FA")
        src = sources.get(pid, km.Source("unknown"))
        if who == "FA":  # a free agent costs the waiver round to anyone who adds him
            src = km.Source("waiver")
        rows.append(km.PlayerRow(
            player_id=pid, name=d["players"][pid].full_name, position=pos, owner=who,
            games=len(pts), ppg=sum(pts) / len(pts),
            value_ppg=km.shrunk_ppg(pts, levels[pos], cfg["prior_games"], cfg["decay"]),
            source=src,
        ))

    pw = cfg["pos_weight"]
    if pw is None:
        single_qb = sum(1 for s in league.roster_positions if s == "QB") == 1 and \
            "SUPER_FLEX" not in league.roster_positions
        pw = {"QB": 0.6} if single_qb else {}
    km.score_rows(rows, levels, n_teams, rules, pw)
    return rows, levels


def playoff_odds(d: dict[str, Any], me_roster: int, boost: float) -> Optional[tuple[float, float, int]]:
    league = d["league"]
    played = d["played"]
    pts: dict[int, list[float]] = {r.roster_id: [] for r in d["rosters"]}
    for wk in range(1, played + 1):
        for m in d["matchups"].get(wk, []):
            if m.points is not None:
                pts[m.roster_id].append(m.points)
    if not all(pts.values()):
        return None
    mean = {k: sum(v) / len(v) for k, v in pts.items()}
    pf = {k: sum(v) for k, v in pts.items()}
    var = [sum((x - mean[k]) ** 2 for x in v) / max(len(v) - 1, 1) for k, v in pts.items()]
    sd = math.sqrt(sum(var) / len(var))
    wins = {r.roster_id: r.settings.wins + 0.5 * r.settings.ties for r in d["rosters"]}
    schedule = []
    for wk in range(played + 1, int(league.settings.get("playoff_week_start", 15))):
        groups: dict[int, list[int]] = {}
        for m in d["matchups"].get(wk, []):
            if m.matchup_id:
                groups.setdefault(m.matchup_id, []).append(m.roster_id)
        schedule.append([(g[0], g[1]) for g in groups.values() if len(g) == 2])
    if not any(schedule):
        return None
    p, w = km.simulate_playoffs(
        wins, pf, mean, sd, schedule, int(league.settings.get("playoff_teams", 6)),
        me_roster, boost=boost,
    )
    return p, w, len(schedule)


def previous_snapshot(snap_dir: Path, today_tag: str) -> Optional[dict[str, Any]]:
    files = sorted(f for f in snap_dir.glob("*.json") if f.stem != today_tag)
    return json.loads(files[-1].read_text()) if files else None


def fmt_cost(c: Optional[int]) -> str:
    return "—" if c is None else f"r{c}"


def table(rows: list[km.PlayerRow], prev: dict[str, Any], limit: Optional[int] = None) -> None:
    print(f"  {'player':22} {'pos':3} {'gp':>2} {'ppg':>5} {'val':>5} {'implied':>7} {'cost':>5} {'surplus':>7} {'Δ':>3}  owner")
    for r in rows[:limit]:
        old = prev.get(r.player_id, {}).get("surplus")
        delta = "" if old is None or r.surplus is None else f"{r.surplus - old:+d}"
        sur = "—" if r.surplus is None else f"{r.surplus:+d}"
        imp = f"r{r.implied_round}" if r.vorp > 0 else "<repl"
        flag = f"  [{'; '.join(r.flags)}]" if r.flags else ""
        print(f"  {r.name:22} {r.position:3} {r.games:>2} {r.ppg:5.1f} {r.value_ppg:5.1f} {imp:>7} "
              f"{fmt_cost(r.cost):>5} {sur:>7} {delta:>3}  {r.owner}{flag}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("user")
    ap.add_argument("--league", required=True)
    ap.add_argument("--top", type=int, default=20, help="rows per section")
    ap.add_argument("--min-cost", type=int, help="floor for trade targets/waivers (default from rules.json)")
    ap.add_argument("--boost", type=float, default=0.0, help="what-if: add N points/week to your scoring in the playoff sim")
    ap.add_argument("--rules", type=Path, default=HERE / "rules.json")
    ap.add_argument("--no-snapshot", action="store_true")
    args = ap.parse_args()

    rules, cfg = load_config(args.rules)
    min_cost = args.min_cost if args.min_cost is not None else cfg["min_cost"]

    async with SleeperClient() as client:
        d = await fetch(client, args.user, args.league)

    rows, levels = build_rows(d, rules, cfg)
    league, state = d["league"], d["state"]
    me_roster = next(r.roster_id for r in d["rosters"] if r.owner_id == d["me"])
    me_name = d["users"][d["me"]]

    tag = f"{date.today().isoformat()}_w{state.week}"
    snap_dir = HERE / "snapshots"
    prev = previous_snapshot(snap_dir, tag) or {}
    prev_rows = prev.get("rows", {})

    print(f"\nKEEPER REPORT — {league.name} ({d['season']}, through week {d['played']}, {d['played']} games of sample)")
    print(f"Rules: keep {rules.max_keepers}, cost = last round - {rules.round_discount}, "
          f"rounds 1-{rules.round_discount} ineligible, waiver adds r{rules.ww_first_round}+")
    print("Replacement PPG: " + ", ".join(f"{p} {v:.1f}" for p, v in levels.items()))
    if d["scoring_diff"]:
        md = sum(d["scoring_diff"]) / len(d["scoring_diff"])
        print(f"Scoring check: mean |mine - Sleeper pts_half_ppr| = {md:.2f} pts over {len(d['scoring_diff'])} player-games "
              "(small = your league's rules are close to stock half-PPR; sanity check only)")
    if prev:
        print(f"Δ column = surplus change vs {prev.get('tag')}")

    mine = sorted([r for r in rows if r.owner == me_name], key=lambda r: (r.surplus is None, -(r.surplus or 0), r.overall_rank))
    print(f"\n== YOUR KEEPERS ({me_name}) — top {rules.max_keepers} by surplus marked ★")
    picks = {r.player_id for r in km.keepable(mine, 1, min_surplus=1)[: rules.max_keepers]}
    table(mine, prev_rows)
    print("  ★ " + (", ".join(r.name for r in mine if r.player_id in picks) or "none with positive surplus"))

    fas = km.keepable([r for r in rows if r.owner == "FA"], min_cost)
    print(f"\n== WAIVER ADDS — free agents who would cost r{rules.ww_first_round} (top {args.top})")
    table(fas, prev_rows, args.top)

    others = [r for r in rows if r.owner not in (me_name, "FA")]
    excess = km.excess_keepers(others, rules.max_keepers)
    excess_ids = {r.player_id for rs in excess.values() for r in rs}
    targets = km.keepable(others, min_cost)
    print(f"\n== TRADE TARGETS — other rosters, keeper cost >= r{min_cost} (top {args.top}); ◆ = owner has more good keepers than slots")
    for r in targets[: args.top]:
        if r.player_id in excess_ids:
            r.flags.insert(0, "◆ excess for owner")
    table(targets, prev_rows, args.top)
    if excess:
        print("\n  Owners with more good keepers than slots (their extras are cheapest to buy):")
        for o, rs in sorted(excess.items(), key=lambda kv: -len(kv[1])):
            print(f"    {o}: " + ", ".join(f"{r.name} ({fmt_cost(r.cost)}, {r.surplus:+d})" for r in rs))

    odds = playoff_odds(d, me_roster, args.boost)
    print("\n== PLAYOFF ODDS")
    if odds:
        p, w, n = odds
        extra = f" (+{args.boost:g} pts/wk scenario)" if args.boost else ""
        print(f"  {me_name}: {p:.1%} to make the {league.settings.get('playoff_teams', 6)}-team playoffs, "
              f"expected final wins {w:.1f}, {n} weeks left{extra}")
    else:
        print("  Schedule or scores not available yet.")

    if not args.no_snapshot:
        snap_dir.mkdir(exist_ok=True)
        (snap_dir / f"{tag}.json").write_text(json.dumps({
            "tag": tag, "week": state.week, "league": league.name,
            "rows": {r.player_id: {"name": r.name, "owner": r.owner, "cost": r.cost,
                                   "implied_round": r.implied_round, "surplus": r.surplus,
                                   "value_ppg": round(r.value_ppg, 2)} for r in rows},
        }, indent=1))
        print(f"\nSnapshot saved: keeper/snapshots/{tag}.json")


if __name__ == "__main__":
    asyncio.run(main())
