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


def _pick(element, position=1, cost=100):
    return team_mod.Pick(element=element, position=position, is_captain=False,
                         is_vice=False, element_type=3, purchase_cost=cost,
                         now_cost=cost, selling_price=cost)


def _team(elements, bank=15, ft=2):
    return team_mod.Team(entry_id=1, gameweek=2, bank=bank, value=1000,
                         free_transfers=ft,
                         picks=[_pick(e, i + 1) for i, e in enumerate(elements)])


def _elements(spec):
    """spec: {id: (web_name, element_type, cost, team)}"""
    return {
        i: {"id": i, "web_name": n, "second_name": n, "first_name": "",
            "element_type": t, "now_cost": c, "team": club}
        for i, (n, t, c, club) in spec.items()
    }


def _full_squad():
    """A legal 15: 2 GKP, 5 DEF, 5 MID, 3 FWD, no more than 3 per club."""
    layout = [(1, 2), (2, 5), (3, 5), (4, 3)]
    spec, picks, i = {}, [], 1
    for kind, count in layout:
        for n in range(count):
            spec[i] = (f"P{i}", kind, 50, i % 6)
            picks.append(team_mod.Pick(element=i, position=i, is_captain=False,
                                       is_vice=False, element_type=kind,
                                       purchase_cost=50, now_cost=50,
                                       selling_price=50))
            i += 1
    return spec, picks


def _team_with(spec_extra=None, bank=15, ft=2):
    spec, picks = _full_squad()
    spec.update(spec_extra or {})
    team = team_mod.Team(entry_id=1, gameweek=2, bank=bank, value=750,
                         free_transfers=ft, picks=picks)
    return team, _elements(spec)


def test_swap_replaces_a_player_and_moves_the_money():
    team, elements = _team_with({99: ("Target", 3, 30, 9)})
    team_mod.apply_swaps(team, "P11>Target", elements)

    ids = [p.element for p in team.picks]
    assert 11 not in ids and 99 in ids
    assert len(team.picks) == 15, "a swap is one out, one in"
    assert team.bank == 15 + (50 - 30)
    assert team.free_transfers == 1, "a swap spends a free transfer"
    bought = next(p for p in team.picks if p.element == 99)
    assert bought.selling_price == 30, "no profit to share on something just bought"


def test_names_match_without_accents():
    """Half the squad carries accents. Requiring them exactly guarantees failure
    on a phone keyboard."""
    team, elements = _team_with({99: ("João Pedro", 3, 30, 9)})
    team_mod.apply_swaps(team, "P11>Joao Pedro", elements)
    assert 99 in [p.element for p in team.picks]


def test_ambiguous_names_are_refused_not_guessed():
    team, elements = _team_with({98: ("Palmer", 3, 30, 8), 99: ("Palmer", 3, 30, 9)})
    with pytest.raises(team_mod.SwapError, match="matches 2 players"):
        team_mod.apply_swaps(team, "P11>Palmer", elements)
    # The element id disambiguates.
    team_mod.apply_swaps(team, "P11>99", elements)
    assert 99 in [p.element for p in team.picks]


def test_a_typo_suggests_the_intended_player():
    team, elements = _team_with({99: ("Watkins", 3, 30, 9)})
    with pytest.raises(team_mod.SwapError, match="Did you mean"):
        team_mod.apply_swaps(team, "P11>Watkinz", elements)


def test_an_unaffordable_swap_is_refused():
    """Otherwise the override hands the optimiser a squad with a negative bank
    and it plans from a position that cannot exist."""
    team, elements = _team_with({99: ("Premium", 3, 200, 9)}, bank=15)
    with pytest.raises(team_mod.SwapError, match="cannot afford"):
        team_mod.apply_swaps(team, "P11>Premium", elements)


def test_a_swap_that_breaks_the_squad_shape_is_refused():
    """Swapping a midfielder for a defender leaves 6 defenders. Without this the
    solver just reports Infeasible, which names no player."""
    team, elements = _team_with({99: ("Defender", 2, 30, 9)})
    with pytest.raises(team_mod.SwapError, match="defenders"):
        team_mod.apply_swaps(team, "P11>Defender", elements)


def test_the_three_per_club_limit_is_enforced():
    team, elements = _team_with({99: ("Fourth", 3, 30, 1)})
    # Club 1 already holds three of the generated squad.
    with pytest.raises(team_mod.SwapError, match="same club"):
        team_mod.apply_swaps(team, "P11>Fourth", elements)


def test_selling_someone_not_in_the_squad_lists_the_squad():
    """Note the deliberate gap: "Target>P11", where the outgoing player is not
    owned and the incoming one is, is structurally identical to re-running
    "P11>Target" after it already succeeded. Both are treated as already applied,
    because both end at the same correct squad and neither can be told apart
    without transfer history."""
    team, elements = _team_with({98: ("Alpha", 3, 30, 9), 99: ("Beta", 3, 30, 9)})
    with pytest.raises(team_mod.SwapError, match="not in the squad"):
        team_mod.apply_swaps(team, "Alpha>Beta", elements)


def test_an_already_applied_swap_is_not_applied_twice():
    """If FPL publishes the transfer between two runs, the override is stale but
    harmless — the intended end state is already true."""
    team, elements = _team_with({99: ("Target", 3, 30, 9)})
    team_mod.apply_swaps(team, "P11>Target", elements)
    before = [p.element for p in team.picks]
    team_mod.apply_swaps(team, "P11>Target", elements)
    assert [p.element for p in team.picks] == before


def test_a_bad_override_never_raises_out_of_load(monkeypatch):
    """A mistyped name must not cost the snapshot, the projection or the page."""
    team, elements = _team_with()
    team.swap_error = None
    try:
        team_mod.apply_swaps(team, "nonsense", elements)
    except team_mod.SwapError as exc:
        assert "expected 'out>in'" in str(exc)
    else:
        pytest.fail("malformed input should raise inside apply_swaps")


def test_empty_swap_is_a_no_op():
    team, elements = _team_with()
    assert team_mod.apply_swaps(team, "", elements) is team
    assert team_mod.apply_swaps(team, "   ", elements) is team
    assert len(team.picks) == 15


def test_pending_transfers_are_replayed_onto_published_picks():
    """FPL publishes picks per completed gameweek, so a transfer made for the
    upcoming one is invisible in any picks payload. Replaying it keeps the squad
    current instead of a week stale."""
    published = [
        {"element": 1, "position": 1, "is_captain": True, "is_vice_captain": False},
        {"element": 2, "position": 2, "is_captain": False, "is_vice_captain": False},
    ]
    out = team_mod._replay(published, [{"event": 3, "element_out": 1, "element_in": 7}])
    ids = {p["element"] for p in out}
    assert ids == {7, 2}
    # Captaincy cannot follow a player who has been sold.
    assert not any(p.get("is_captain") for p in out)


def test_replay_handles_a_player_transferred_in_then_out_again():
    published = [{"element": 1, "position": 1}]
    out = team_mod._replay(published, [
        {"event": 3, "element_out": 1, "element_in": 7, "time": "2026-09-01T10:00:00Z"},
        {"event": 3, "element_out": 7, "element_in": 9, "time": "2026-09-02T10:00:00Z"},
    ])
    assert [p["element"] for p in out] == [9]
