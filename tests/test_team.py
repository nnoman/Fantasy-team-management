"""Tests for reading the manager's squad from public endpoints.

Free transfers and selling prices both feed straight into the transfer
optimiser: one decides whether a -4 hit is affordable, the other decides how
much money a move actually frees up. Being off by one in either produces advice
that cannot be executed.
"""

from __future__ import annotations

import pytest

from fpl import team as team_mod


def _history(transfers_per_gw):
    return {"current": [{"event": gw, "event_transfers": n, "bank": 15,
                         "value": 1000, "total_points": 0, "overall_rank": 1}
                        for gw, n in enumerate(transfers_per_gw, start=1)],
            "chips": []}


def test_opening_gameweek_does_not_consume_a_transfer():
    # GW1 builds the squad for free, so after it there is exactly one FT for GW2.
    assert team_mod.free_transfers(_history([0])) == 1


def test_unused_transfers_bank_one_at_a_time():
    """Regression: the roll-forward ran once too often, reporting 3 free
    transfers after two untouched gameweeks when the real answer is 2. The
    optimiser would then plan a move the manager could not make without a hit."""
    assert team_mod.free_transfers(_history([0, 0])) == 2
    assert team_mod.free_transfers(_history([0, 0, 0])) == 3
    assert team_mod.free_transfers(_history([0, 0, 0, 0])) == 4


def test_banking_is_capped():
    assert team_mod.free_transfers(_history([0] * 12)) == 5
    assert team_mod.free_transfers(_history([0] * 12), cap=2) == 2


def test_spending_transfers_reduces_the_bank():
    # GW2 has 1 FT; using it leaves 1 for GW3, not 0 — you always get one back.
    assert team_mod.free_transfers(_history([0, 1])) == 1
    # Bank up to two, spend both, and the new week's transfer takes you to one.
    assert team_mod.free_transfers(_history([0, 0, 2])) == 1
    # Bank three, spend two: one survives, plus the new one.
    assert team_mod.free_transfers(_history([0, 0, 0, 2])) == 2


def test_taking_a_hit_never_drops_below_one():
    assert team_mod.free_transfers(_history([0, 4])) == 1


def test_no_completed_gameweeks_is_one_transfer():
    assert team_mod.free_transfers({"current": []}) == 1


@pytest.mark.parametrize("purchase,now,expected", [
    (100, 100, 100),   # unchanged
    (100, 104, 102),   # +0.4 rise, half the profit kept
    (100, 101, 100),   # +0.1 rise rounds down to nothing
    (100, 103, 101),   # +0.3 rise, rounded down
    (100, 95, 95),     # a fall is absorbed in full
])
def test_selling_price_keeps_half_the_profit_rounded_down(purchase, now, expected):
    assert team_mod.selling_price(purchase, now) == expected


def test_budget_is_sellable_value_plus_bank():
    picks = [team_mod.Pick(element=i, position=i, is_captain=False, is_vice=False,
                           element_type=1, purchase_cost=100, now_cost=104,
                           selling_price=102)
             for i in range(1, 4)]
    t = team_mod.Team(entry_id=1, gameweek=2, picks=picks, bank=15, value=306,
                      free_transfers=2)
    assert t.budget == 102 * 3 + 15
    assert t.element_ids == [1, 2, 3]


def test_a_played_match_counts_before_bonus_is_confirmed(tmp_path):
    """FPL leaves `finished` false until bonus points are confirmed, which can be
    days after the whistle. Counting only that reported one game played when two
    had been, and two starts divided by one game reads as a certainty.
    """
    from fpl.store import Store

    store = Store(tmp_path / "fx.sqlite3")
    try:
        store.save_fixtures([
            # Played and fully confirmed.
            {"id": 1, "event": 1, "team_h": 1, "team_a": 2, "team_h_difficulty": 3,
             "team_a_difficulty": 3, "kickoff_time": "2026-08-21T19:00:00Z",
             "finished": True, "finished_provisional": True},
            # Played, bonus not yet confirmed — this is the case that was missed.
            {"id": 2, "event": 2, "team_h": 2, "team_a": 1, "team_h_difficulty": 3,
             "team_a_difficulty": 3, "kickoff_time": "2026-08-28T19:00:00Z",
             "finished": False, "finished_provisional": True},
            # Not played at all.
            {"id": 3, "event": 3, "team_h": 1, "team_a": 2, "team_h_difficulty": 3,
             "team_a_difficulty": 3, "kickoff_time": "2026-09-04T19:00:00Z",
             "finished": False, "finished_provisional": False},
        ])
        played = store.team_games_played()
        assert played[1] == 2, "a provisionally finished match has still been played"
        assert played[2] == 2
    finally:
        store.close()
