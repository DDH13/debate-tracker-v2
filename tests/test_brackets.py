from app.services.public.brackets import ElimDebate, ElimRound, derive_brackets


def _round(seq: int, name: str, *pairs: tuple[int, int, int | None]) -> ElimRound:
    return ElimRound(
        round_id=seq,
        seq=seq,
        name=name,
        debates=[
            ElimDebate(debate_id=seq * 100 + i, prop_team_id=p, opp_team_id=o, winner_team_id=w)
            for i, (p, o, w) in enumerate(pairs)
        ],
    )


def test_single_bracket_champion_and_finishes() -> None:
    rounds = [
        _round(5, "Semi", (1, 4, 1), (2, 3, 3)),
        _round(6, "Grand Final", (1, 3, 1)),
    ]
    (bracket,) = derive_brackets(rounds)
    assert bracket.primary
    assert bracket.label == "Grand Final"
    assert bracket.champion_team_id == 1
    assert bracket.finalist_team_id == 3
    assert bracket.finishes == {1: "champion", 3: "finalist", 4: "semi", 2: "semi"}


def test_partial_octos_use_depth_from_the_final() -> None:
    rounds = [
        _round(5, "Octos", (9, 10, 9), (11, 12, 11)),  # only two octo debates, the rest had byes
        _round(6, "Quarters", (1, 9, 1), (2, 11, 2), (3, 4, 3), (5, 6, 5)),
        _round(7, "Semis", (1, 2, 1), (3, 5, 3)),
        _round(8, "Final", (1, 3, 3)),
    ]
    (bracket,) = derive_brackets(rounds)
    assert bracket.champion_team_id == 3
    assert bracket.finishes[3] == "champion"
    assert bracket.finishes[1] == "finalist"
    assert bracket.finishes[2] == "semi"
    assert bracket.finishes[9] == "quarter"  # lost in quarters after winning an octo
    assert bracket.finishes[4] == "quarter"  # bye in octos, lost in quarters
    assert bracket.finishes[10] == "octo"
    assert bracket.finishes[12] == "octo"


def test_novice_bracket_running_after_the_grand_final() -> None:
    rounds = [
        _round(7, "Semi Finals", (1, 4, 1), (2, 3, 3)),
        _round(9, "Grand Final", (1, 3, 1)),
        _round(10, "Novice Semis", (21, 24, 21), (22, 23, 23)),
        _round(12, "Novice Finals", (21, 23, 23)),
    ]
    open_bracket, novice = derive_brackets(rounds)
    assert open_bracket.primary and not novice.primary
    assert open_bracket.label == "Grand Final"
    assert open_bracket.champion_team_id == 1
    assert novice.label == "Novice Finals"
    assert novice.champion_team_id == 23
    assert novice.finishes[21] == "finalist"
    assert novice.finishes[24] == "semi"
    assert set(open_bracket.finishes).isdisjoint(novice.finishes)


def test_no_champion_when_final_is_undecided_or_has_several_debates() -> None:
    (undecided,) = derive_brackets([_round(5, "Final", (1, 2, None))])
    assert undecided.champion_team_id is None
    (quarters,) = derive_brackets([_round(5, "Quarters", (1, 2, 1), (3, 4, 3), (5, 6, 5), (7, 8, 7))])
    assert quarters.champion_team_id is None
    # Winners of the last imported round have won through to the semis.
    assert quarters.finishes[1] == "semi"
    assert quarters.finishes[2] == "quarter"


def test_empty_and_deep_rounds() -> None:
    assert derive_brackets([]) == []
    sixteen = [(2 * i + 1, 2 * i + 2, 2 * i + 1) for i in range(16)]
    (bracket,) = derive_brackets([_round(1, "Round of 32", *sixteen)])
    assert bracket.finishes[2] == "broke"
    assert bracket.finishes[1] == "octo"
