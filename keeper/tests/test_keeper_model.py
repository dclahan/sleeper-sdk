"""Property tests for the keeper model (pure logic, offline)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keeper_model as km  # noqa: E402

RULES = km.KeeperRules()
CADRE = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 7


def row(pid, pos, ppg, source, owner="x", games=6):
    return km.PlayerRow(pid, pid, pos, owner, games, ppg, ppg, source)


def test_cost_is_three_rounds_earlier():
    assert km.keeper_cost(km.Source("draft", 7), RULES) == 4
    assert km.keeper_cost(km.Source("draft", 9), RULES) == 6


def test_rounds_one_to_three_are_not_keepable():
    for r in (1, 2, 3):
        assert km.keeper_cost(km.Source("draft", r), RULES) is None
    assert km.keeper_cost(km.Source("draft", 4), RULES) == 1


def test_waiver_and_unknown_cost_the_waiver_round():
    assert km.keeper_cost(km.Source("waiver"), RULES) == 13
    assert km.keeper_cost(km.Source("unknown"), RULES) == 13


def test_readd_rule_is_switchable():
    s = km.Source("draft", 10, readded=True)
    assert km.keeper_cost(s, km.KeeperRules(readd_resets_cost=True)) == 13
    assert km.keeper_cost(s, km.KeeperRules(readd_resets_cost=False)) == 7


def test_replacement_ranks_for_cadre_league():
    assert km.replacement_ranks(CADRE, 12) == {"QB": 14, "RB": 30, "WR": 42, "TE": 14}


def test_replacement_ranks_grow_with_league_size():
    small, big = km.replacement_ranks(CADRE, 10), km.replacement_ranks(CADRE, 14)
    assert all(big[p] > small[p] for p in km.SKILL_POSITIONS)


def test_shrinkage_pulls_toward_prior_and_fades_with_sample():
    prior = 8.0
    short = km.shrunk_ppg([20.0] * 3, prior)
    long = km.shrunk_ppg([20.0] * 14, prior)
    assert prior < short < long < 20.0  # a longer hot sample is trusted more


def test_shrinkage_with_no_games_is_the_prior():
    assert km.shrunk_ppg([], 8.0) == 8.0


def test_recency_weights_favor_recent_games():
    w = km.recency_weights(5, 0.9)
    assert w == sorted(w) and w[-1] == 1.0
    assert km.shrunk_ppg([5, 5, 5, 20], 8.0, decay=0.9) > km.shrunk_ppg([20, 5, 5, 5], 8.0, decay=0.9)


def _league():
    rows = [row(f"w{i}", "WR", 20 - i, km.Source("draft", 1)) for i in range(40)]
    return rows


def test_surplus_never_rises_with_cost():
    """Same production, higher round cost -> never more surplus."""
    levels = {"WR": 8.0}
    cheap = row("a", "WR", 15.0, km.Source("draft", 14))   # cost 11
    dear = row("b", "WR", 15.0, km.Source("draft", 7))     # cost 4
    km.score_rows(_league() + [cheap, dear], levels, 12, RULES)
    assert cheap.surplus > dear.surplus


def test_better_production_never_lowers_surplus_at_same_cost():
    levels = {"WR": 8.0}
    hi = row("a", "WR", 16.0, km.Source("draft", 10))
    lo = row("b", "WR", 12.0, km.Source("draft", 10))
    km.score_rows(_league() + [hi, lo], levels, 12, RULES)
    assert hi.surplus >= lo.surplus


def test_below_replacement_has_no_surplus():
    p = row("a", "WR", 5.0, km.Source("waiver"))
    km.score_rows(_league() + [p], {"WR": 8.0}, 12, RULES)
    assert p.surplus is None


def test_ineligible_has_no_surplus():
    p = row("a", "WR", 25.0, km.Source("draft", 2))
    km.score_rows([p], {"WR": 8.0}, 12, RULES)
    assert p.cost is None and p.surplus is None


def test_implied_round_is_monotonic_in_vorp():
    rows = [row(f"p{i}", "WR", 30 - i * 0.5, km.Source("waiver")) for i in range(40)]
    km.score_rows(rows, {"WR": 5.0}, 12, RULES)
    by_rank = sorted(rows, key=lambda r: r.overall_rank)
    rounds = [r.implied_round for r in by_rank]
    assert rounds == sorted(rounds) and rounds[0] == 1


def test_position_weight_discounts_that_position():
    qb = row("qb", "QB", 22.0, km.Source("waiver"))
    wr = row("wr", "WR", 14.0, km.Source("waiver"))
    levels = {"QB": 16.0, "WR": 8.0}
    km.score_rows([qb, wr], levels, 12, RULES, {"QB": 0.6})
    assert qb.vorp == (22 - 16) * 0.6


def test_marginal_replacement_player_is_not_a_keepable_target():
    p = row("a", "WR", 8.3, km.Source("waiver"))
    km.score_rows(_league() + [p], {"WR": 8.0}, 12, RULES)
    assert p not in km.keepable([p], 1)


def test_excess_keepers_returns_only_players_beyond_the_slots():
    rows = []
    for i, s in enumerate((5, 4, 3, 2)):
        r = row(f"k{i}", "WR", 15, km.Source("waiver"), owner="rich")
        r.surplus, r.vorp = s, 5.0
        rows.append(r)
    r = row("solo", "WR", 15, km.Source("waiver"), owner="poor")
    r.surplus, r.vorp = 5, 5.0
    rows.append(r)
    out = km.excess_keepers(rows, 3)
    assert [x.player_id for x in out["rich"]] == ["k3"]
    assert "poor" not in out


def test_playoff_sim_is_sane():
    wins = {1: 6, 2: 0, 3: 3, 4: 3}
    pf = {1: 700, 2: 400, 3: 550, 4: 550}
    mean = {1: 120, 2: 80, 3: 100, 4: 100}
    sched = [[(1, 2), (3, 4)]] * 4
    strong, _ = km.simulate_playoffs(wins, pf, mean, 20, sched, 2, 1, n=3000)
    weak, _ = km.simulate_playoffs(wins, pf, mean, 20, sched, 2, 2, n=3000)
    assert strong > 0.9 and weak < 0.05


def test_playoff_boost_helps():
    wins = {1: 1, 2: 3, 3: 3, 4: 3}
    pf = {k: 500.0 for k in wins}
    mean = {k: 100.0 for k in wins}
    sched = [[(1, 2), (3, 4)]] * 4
    base, _ = km.simulate_playoffs(wins, pf, mean, 20, sched, 2, 1, n=4000)
    up, _ = km.simulate_playoffs(wins, pf, mean, 20, sched, 2, 1, boost=20, n=4000)
    assert up > base
