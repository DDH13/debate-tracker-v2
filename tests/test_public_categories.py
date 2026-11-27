"""Content / style / strategy: the judge arithmetic in `app/services/public/categories.py`,
the debater page's category block, the profile rule behind the averages and the list sorts."""

import pytest
from fastapi.testclient import TestClient

from app.services.public import categories as cat
from app.services.public.categories import Mark

API = "/api/v1"


def mark(
    debater: int,
    scores: tuple[float, float, float],
    *,
    ballot: int = 1,
    judge: int = 1,
    position: int = 1,
    side: str = "prop",
    winner: str = "prop",
    discarded: bool = False,
    forfeit: bool = False,
) -> Mark:
    c, s, t = scores
    return Mark(
        debater_id=debater, position=position, content=c, style=s, strategy=t, ballot_id=ballot,
        judge_id=judge, side=side, winner=winner, discarded=discarded, forfeit=forfeit,
    )


# --- judges


def judge_ballots(judge: int, n: int, *, start: int = 0, winner_boost=(2, 2, 1), both: bool = True) -> list[Mark]:
    """n ballots; prop (winner) marks sit `winner_boost` above opp's."""
    marks = []
    for i in range(start, start + n):
        base = 28 + (i % 3)
        marks.append(mark(1, (base + winner_boost[0], base + winner_boost[1] + (i % 2), 14 + winner_boost[2] + (i % 2)),
                          ballot=judge * 1000 + i, judge=judge, side="prop", winner="prop"))
        if both:
            marks.append(mark(2, (base, base, 14), ballot=judge * 1000 + i, judge=judge,
                              side="opp", winner="prop"))
    return marks


def test_judge_categories_none_below_five_ballots() -> None:
    data = cat.build_category_data(judge_ballots(1, 4))
    assert cat.judge_categories(data, 1) is None
    data = cat.build_category_data(judge_ballots(1, 5))
    assert cat.judge_categories(data, 1) is not None
    assert cat.judge_categories(data, 99) is None


def test_decides_is_winner_minus_loser_as_percent_of_scale() -> None:
    data = cat.build_category_data(judge_ballots(1, 6, winner_boost=(4, 2, 1)))
    result = cat.judge_categories(data, 1)
    decides = {c.category: c.decides.value for c in result.categories}
    assert decides["content"] == pytest.approx(4 / 40 * 100)
    assert decides["style"] == pytest.approx(((2 + 0.5)) / 40 * 100, abs=0.5)
    assert decides["strategy"] == pytest.approx(((1 + 0.5)) / 20 * 100, abs=0.5)
    assert result.ballots == 6


def test_decides_uses_only_ballots_with_both_sides_scored() -> None:
    marks = judge_ballots(1, 5, winner_boost=(4, 4, 2)) + judge_ballots(1, 5, start=50, winner_boost=(20, 20, 10), both=False)
    result = cat.judge_categories(cat.build_category_data(marks), 1)
    assert result.ballots == 10
    content = next(c for c in result.categories if c.category == "content")
    assert content.decides.value == pytest.approx(4 / 40 * 100)


def test_separation_needs_thirty_marks_and_is_low_when_categories_move_together() -> None:
    few = cat.build_category_data(judge_ballots(1, 5))  # 10 marks
    assert cat.judge_categories(few, 1).separation is None

    # 16 ballots x 2 marks = 32 marks, all three categories identical to the point: halo.
    marks = []
    for i in range(16):
        for d, side in ((1, "prop"), (2, "opp")):
            level = 24 + (i * 3 + d) % 9
            marks.append(mark(d, (level, level, level / 2), ballot=i, side=side, judge=1))
    halo = cat.judge_categories(cat.build_category_data(marks), 1)
    assert halo.separation is not None and halo.separation.value == pytest.approx(0.0, abs=1e-9)


def test_judge_field_means_are_over_judges_with_enough_ballots() -> None:
    marks = judge_ballots(1, 6) + judge_ballots(2, 6, winner_boost=(0, 0, 0)) + judge_ballots(3, 2)
    data = cat.build_category_data(marks)
    one = cat.judge_categories(data, 1)
    two = cat.judge_categories(data, 2)
    d1 = next(c for c in one.categories if c.category == "content").decides
    d2 = next(c for c in two.categories if c.category == "content").decides
    assert d1.field_mean == d2.field_mean == pytest.approx((d1.value + d2.value) / 2)
    assert d1.delta == pytest.approx(d1.value - d1.field_mean, abs=0.01)


# --- page endpoints


def _scored(client: TestClient, **kw):
    from tests.helpers import build_tournament

    return build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=2, slug=kw.pop("slug", "cat-cup"), categories=True, **kw)


def _page(client: TestClient, debater) -> dict:
    return client.get(f"{API}/public/debaters/{debater['id']}").json()


def test_debater_categories_block(client: TestClient) -> None:
    data = _scored(client)
    debater = data["teams"][0]["debaters"][0]  # position 1 for prop in each debate they play
    page = _page(client, debater)
    block = page["categories"]
    prelim = [s for s in page["speeches"] if s["position"] != 4 and not s["round"]["is_elimination"]]
    assert block["speeches"] == len(prelim) > 0
    for name in ("content", "style", "strategy"):
        expected = sum(s[name] for s in prelim) / len(prelim)
        assert block["averages"][name]["value"] == pytest.approx(expected, abs=1e-3)
    assert [t["tournament"]["slug"] for t in block["by_tournament"]] == ["cat-cup"]
    assert block["by_tournament"][0]["speeches"] == len(prelim)
    assert [p["position"] for p in block["by_position"]] == [1, 2, 3]
    assert sum(p["speeches"] for p in block["by_position"]) == len(prelim)
    result = block["by_result"]
    assert result["won"]["speeches"] + result["lost"]["speeches"] == len(prelim)
    assert block["spread"]["content"] is not None
    assert [b["category"] for b in block["bests"]] == ["content", "style", "strategy"]


def test_category_averages_add_up_to_the_speaker_average(client: TestClient) -> None:
    data = _scored(client)
    client.post(f"{API}/stats/refresh")
    debater = data["teams"][0]["debaters"][0]
    page = _page(client, debater)
    total = sum(page["summary"][name]["value"] for name in ("content", "style", "strategy"))
    assert total == pytest.approx(page["summary"]["average"]["value"], abs=1e-3)
    block = page["categories"]["averages"]
    assert sum(block[name]["value"] for name in ("content", "style", "strategy")) == pytest.approx(total, abs=1e-3)


def test_profile_categories_count_each_panel_speech_once_and_skip_elims_and_replies(client: TestClient) -> None:
    data = _scored(client)
    client.post(f"{API}/stats/refresh")
    debater = data["teams"][0]["debaters"][0]
    page = _page(client, debater)
    prelim = [s for s in page["speeches"] if s["position"] != 4 and not s["round"]["is_elimination"]]
    # The panel's three ballots are one speech: content is the per-speech mean, not 3x the ballots.
    profile = client.get(f"{API}/public/debaters/{debater['id']}").json()["summary"]
    assert profile["speeches"] == len(prelim)
    assert profile["content"]["value"] == pytest.approx(sum(s["content"] for s in prelim) / len(prelim), abs=1e-3)


def test_bests_take_the_highest_and_the_most_recent_on_a_tie(client: TestClient) -> None:
    data = _scored(client)
    debater = data["teams"][0]["debaters"][0]
    page = _page(client, debater)
    marked = [s for s in page["speeches"] if s["position"] != 4]
    best = {b["category"]: b for b in page["categories"]["bests"]}
    for name in ("content", "style", "strategy"):
        top = max(s[name] for s in marked)
        assert best[name]["mark"] == top
        latest = next(s for s in page["speeches"] if s["position"] != 4 and s[name] == top)  # most recent first
        assert best[name]["debate_id"] == latest["debate_id"]
    assert best["content"]["scale"] == 40 and best["strategy"]["scale"] == 20


def test_debater_without_category_marks_gets_an_empty_block(client: TestClient) -> None:
    from tests.helpers import build_tournament

    data = build_tournament(client, teams=4, prelim_rounds=3, elim_rounds=1, slug="plain-cup")
    block = _page(client, data["teams"][0]["debaters"][0])["categories"]
    assert block["speeches"] == 0
    assert block["averages"]["content"]["value"] is None
    assert block["by_tournament"] == [] and block["bests"] == []
    assert block["spread"]["content"] is None


def test_field_position_and_result_means(client: TestClient, session) -> None:
    from app.services.public import field

    _scored(client)
    means = field.field_means(session)
    assert set(means.position_category_means) == {(p, c) for p in (1, 2, 3) for c in ("content", "style", "strategy")}
    # Position 1 content: prop 30.5 / opp 30.0 on every ballot.
    assert means.position_category_means[(1, "content")] == pytest.approx(30.25)
    assert set(means.category_result_gap) == {"content", "style", "strategy"}
    # Prop (lower-indexed team) always wins: content 30.5/30/30 (mean 30.17) against 30/30/29.5 (29.83).
    assert means.category_result_gap["content"] == pytest.approx(1 / 3, abs=1e-3)


def test_list_sorts_by_category_and_carries_the_averages(client: TestClient) -> None:
    _scored(client)
    client.post(f"{API}/stats/refresh")
    for sort in ("content", "style", "strategy"):
        body = client.get(f"{API}/public/debaters?sort={sort}&order=desc&min_rounds=1").json()
        values = [row[sort] for row in body["items"]]
        assert values and None not in values
        assert values == sorted(values, reverse=True)
    assert client.get(f"{API}/public/debaters?sort=nonsense").status_code == 422


def test_judge_page_carries_categories(client: TestClient) -> None:
    data = _scored(client)
    page = client.get(f"{API}/public/judges/{data['judges'][0]['id']}").json()
    block = page["categories"]
    assert block["ballots"] > 0
    assert [c["category"] for c in block["categories"]] == ["content", "style", "strategy"]
    assert block["categories"][0]["average"]["value"] is not None


def test_judge_with_few_ballots_has_no_categories(client: TestClient) -> None:
    from tests.helpers import build_tournament

    data = build_tournament(client, teams=2, prelim_rounds=1, elim_rounds=0, slug="tiny-cup", categories=True)
    page = client.get(f"{API}/public/judges/{data['judges'][0]['id']}").json()
    assert page["categories"] is None


def test_category_pages_query_count_is_constant(client: TestClient, session) -> None:
    from tests.helpers import assert_constant_queries

    assert_constant_queries(
        client, session, lambda d: f"{API}/public/debaters/{d['teams'][0]['debaters'][0]['id']}", tag="-cd", categories=True
    )
    assert_constant_queries(
        client, session, lambda d: f"{API}/public/judges/{d['judges'][0]['id']}", tag="-cj", categories=True
    )
    assert_constant_queries(
        client, session, lambda d: f"{API}/public/debaters?sort=strategy&min_rounds=1", tag="-cl", categories=True
    )
