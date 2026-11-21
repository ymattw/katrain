import pytest

from katrain.core import ogs
from katrain.core.ogs import OgsError, OgsGameSource, ogs_rank_label, ogs_result
from katrain.core.online import RemoteGame, RemoteUser


def _player(pid, username, ranking=18.0, professional=False):
    return {"id": pid, "username": username, "ranking": ranking, "professional": professional}


def _game(
    gid,
    black=None,
    white=None,
    ended="2020-01-01T00:00:00.000000Z",
    outcome="Resignation",
    black_lost=False,
    white_lost=True,
    annulled=False,
):
    return {
        "id": gid,
        "started": "2020-01-01T09:03:00.000000Z",
        "ended": ended,
        "outcome": outcome,
        "black_lost": black_lost,
        "white_lost": white_lost,
        "annulled": annulled,
        "players": {"black": black or _player(1, "A"), "white": white or _player(2, "B")},
    }


class TestRank:
    def test_kyu(self):
        assert ogs_rank_label(30) == "1d"  # 30 is dan, not kyu
        assert ogs_rank_label(29.1) == "1k"
        assert ogs_rank_label(18.97) == "12k"

    def test_dan(self):
        assert ogs_rank_label(30) == "1d"
        assert ogs_rank_label(31.5) == "2d"
        assert ogs_rank_label(38) == "9d"

    def test_pro(self):
        assert ogs_rank_label(45, professional=True) == "9p"
        assert ogs_rank_label(1039, professional=True) == "3p"

    def test_invalid(self):
        assert ogs_rank_label(None) is None
        assert ogs_rank_label("x") is None


class TestResult:
    def test_resignation(self):
        assert ogs_result("Resignation", False, True) == "B+R"  # white lost
        assert ogs_result("Resignation", True, False) == "W+R"  # black lost

    def test_timeout(self):
        assert ogs_result("Timeout", True, False) == "W+T"

    def test_points(self):
        assert ogs_result("12.5 points", False, True) == "B+12.5"

    def test_annulled_or_void_is_draw(self):
        assert ogs_result("Cancellation", True, False, annulled=True) == "Draw"
        assert ogs_result("", True, True) == "Draw"

    def test_unknown_outcome(self):
        assert ogs_result("Something else", True, False) == "W+?"


class TestOgsGameSource:
    def test_lookup_exact_match(self, monkeypatch):
        payload = {"results": [_player(1, "foobar"), _player(2, "foo")]}
        monkeypatch.setattr(ogs, "_get_json", lambda url, **k: payload)

        user = OgsGameSource().lookup("foo")

        assert user.user_id == "2"
        assert user.name == "foo"

    def test_lookup_ambiguous_without_exact_match(self, monkeypatch):
        payload = {"results": [_player(1, "foobar"), _player(2, "foobaz")]}
        monkeypatch.setattr(ogs, "_get_json", lambda url, **k: payload)

        assert OgsGameSource().lookup("foo") is None

    def test_lookup_no_results(self, monkeypatch):
        monkeypatch.setattr(ogs, "_get_json", lambda url, **k: {"results": []})
        assert OgsGameSource().lookup("nobody") is None

    def test_list_games_page_maps_and_url(self, monkeypatch):
        captured = {}

        def fake_get_json(url, **kwargs):
            captured["url"] = url
            return {
                "count": 42,
                "next": "https://online-go.com/api/v1/players/1/games?ordering=-id&page=2&page_size=2",
                "results": [
                    _game(101, black=_player(1, "A"), white=_player(2, "B")),
                    _game(
                        100,
                        black=_player(2, "B"),
                        white=_player(1, "A"),
                        outcome="9.5 points",
                        black_lost=False,
                        white_lost=True,
                    ),
                ],
            }

        monkeypatch.setattr(ogs, "_get_json", fake_get_json)
        user = RemoteUser(source="ogs", user_id="1", name="A")

        page = OgsGameSource().list_games_page(user, 1, 2)

        assert "ordering=-id" in captured["url"]
        assert "page=1" in captured["url"]
        assert "page_size=2" in captured["url"]
        assert page.total == 42
        assert page.has_more is True
        assert [g.game_id for g in page.games] == ["101", "100"]
        assert page.games[0].user_color == "B"
        assert page.games[1].user_color == "W"
        assert page.games[1].result == "B+9.5"
        assert page.games[0].date == "2020-01-01 09:03:00"

    def test_list_games_page_skips_ongoing_and_other_players(self, monkeypatch):
        payload = {
            "count": 3,
            "next": None,
            "results": [
                _game(1, ended=None),  # ongoing: no SGF available
                _game(2, black=_player(3, "C"), white=_player(4, "D")),  # user not playing
                _game(3),
            ],
        }
        monkeypatch.setattr(ogs, "_get_json", lambda url, **k: payload)
        user = RemoteUser(source="ogs", user_id="1", name="A")

        page = OgsGameSource().list_games_page(user, 1, 10)

        assert [g.game_id for g in page.games] == ["3"]
        assert page.has_more is False

    def test_fetch_sgf(self, monkeypatch):
        monkeypatch.setattr(ogs, "_get_text", lambda url, **k: "(;GM[1])")
        assert OgsGameSource().fetch_sgf(RemoteGame(source="ogs", game_id="1")) == "(;GM[1])"

    def test_fetch_sgf_empty(self, monkeypatch):
        monkeypatch.setattr(ogs, "_get_text", lambda url, **k: "   ")
        with pytest.raises(OgsError):
            OgsGameSource().fetch_sgf(RemoteGame(source="ogs", game_id="1"))
