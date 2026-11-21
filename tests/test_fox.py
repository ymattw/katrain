import pytest

from katrain.core import fox
from katrain.core.fox import FoxError, FoxGameSource, fox_rank_label, parse_result
from katrain.core.online import RemoteGame, RemoteUser


class TestRank:
    def test_dan(self):
        assert fox_rank_label(18) == "1d"
        assert fox_rank_label(19) == "2d"
        assert fox_rank_label(26) == "9d"

    def test_kyu(self):
        assert fox_rank_label(17) == "1k"
        assert fox_rank_label(1) == "17k"

    def test_pro(self):
        assert fox_rank_label(103) == "P4d"

    def test_invalid(self):
        assert fox_rank_label(None) is None
        assert fox_rank_label("x") is None


class TestResult:
    def test_win_by_points_is_centipoints(self):
        assert parse_result(1, 350, 1) == "B+3.5"
        assert parse_result(1, 2275, 1) == "B+22.75"
        assert parse_result(2, 25, 1) == "W+0.25"

    def test_win_by_resign(self):
        assert parse_result(2, -1, 3) == "W+R"

    def test_timeout(self):
        assert parse_result(1, 0, 2) == "B+T"

    def test_draw(self):
        assert parse_result(0, 0, 0) == "Draw"


class TestFoxGameSource:
    def test_numeric_nickname_is_looked_up(self, monkeypatch):
        payload = {"result": 0, "uid": "999", "username": "12345", "dan": 18}
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: payload)

        user = FoxGameSource().lookup("12345")

        assert user.user_id == "999"
        assert user.name == "12345"

    def test_lookup_by_name(self, monkeypatch):
        payload = {
            "result": 0,
            "uid": "32499445",
            "username": "KataGo",
            "dan": 26,
            "totalwin": 10,
            "totallost": 2,
            "totalequal": 1,
        }
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: payload)

        user = FoxGameSource().lookup("KataGo")

        assert user.user_id == "32499445"
        assert user.rank == "9d"
        assert user.record == "10W 2L 1D"

    def test_lookup_error(self, monkeypatch):
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: {"result": 1, "resultstr": "nope"})
        with pytest.raises(FoxError):
            FoxGameSource().lookup("nobody")

    def test_list_games(self, monkeypatch):
        payload = {
            "result": 0,
            "chesslist": [
                {
                    "chessid": "1",
                    "starttime": "2025-01-01 10:00:00",
                    "blacknick": "A",
                    "blackuid": 1,
                    "blackdan": 19,
                    "whitenick": "B",
                    "whiteuid": 2,
                    "whitedan": 26,
                    "winner": 1,
                    "point": 200,
                    "reason": 1,
                    "movenum": 200,
                }
            ],
        }
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: payload)

        games = FoxGameSource().list_games(RemoteUser(source="fox", user_id="1", name="A"))

        assert len(games) == 1
        assert games[0].black_rank == "2d"
        assert games[0].white_rank == "9d"
        assert games[0].result == "B+2"
        assert games[0].move_count == 200
        assert games[0].user_color == "B"

    def test_list_games_filters_unplayed_and_duplicates(self, monkeypatch):
        payload = {
            "result": 0,
            "chesslist": [
                {"chessid": "1", "blackuid": 1, "whiteuid": 2},
                {"chessid": "1", "blackuid": 1, "whiteuid": 2},  # duplicate
                {"chessid": "2", "blackuid": 3, "whiteuid": 4},  # user not playing
                {"chessid": "3", "blackuid": 2, "whiteuid": 1},  # user is white
            ],
        }
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: payload)

        games = FoxGameSource().list_games(RemoteUser(source="fox", user_id="1", name="A"))

        assert [g.game_id for g in games] == ["1", "3"]
        assert games[0].user_color == "B"
        assert games[1].user_color == "W"

    def test_list_games_uses_fetchnum(self, monkeypatch):
        captured = {}

        def fake_get_json(url, **kwargs):
            captured["url"] = url
            return {"result": 0, "chesslist": []}

        monkeypatch.setattr(fox, "_get_json", fake_get_json)
        FoxGameSource().list_games(RemoteUser(source="fox", user_id="1", name="A"), limit=50)
        assert "fetchnum=50" in captured["url"]

    def test_list_games_defaults_to_server_limit(self, monkeypatch):
        captured = {}

        def fake_get_json(url, **kwargs):
            captured["url"] = url
            return {"result": 0, "chesslist": []}

        monkeypatch.setattr(fox, "_get_json", fake_get_json)
        FoxGameSource().list_games(RemoteUser(source="fox", user_id="1", name="A"))
        assert "fetchnum=200" in captured["url"]

    def test_list_games_caps_fetchnum_at_server_limit(self, monkeypatch):
        captured = {}

        def fake_get_json(url, **kwargs):
            captured["url"] = url
            return {"result": 0, "chesslist": []}

        monkeypatch.setattr(fox, "_get_json", fake_get_json)
        FoxGameSource().list_games(RemoteUser(source="fox", user_id="1", name="A"), limit=9999)
        assert "fetchnum=200" in captured["url"]

    @staticmethod
    def _chesslist(count):
        return [
            {"chessid": str(i), "blackuid": 1, "whiteuid": 2, "blacknick": "A", "whitenick": "B"}
            for i in range(1, count + 1)
        ]

    def test_list_games_page_first_page_is_cheap(self, monkeypatch):
        urls = []

        def fake_get_json(url, **kwargs):
            urls.append(url)
            return {"result": 0, "chesslist": self._chesslist(12)}

        monkeypatch.setattr(fox, "_get_json", fake_get_json)
        user = RemoteUser(source="fox", user_id="1", name="A")

        page = FoxGameSource().list_games_page(user, 1, 10)

        assert "fetchnum=10" in urls[-1]
        assert len(urls) == 1
        assert [g.game_id for g in page.games] == [str(i) for i in range(1, 11)]
        assert page.has_more is True
        assert page.total is None

    def test_list_games_page_full_first_page_is_maybe_more(self, monkeypatch):
        # A boundary row the user did not play is filtered out, so list_games
        # returns exactly page_size rows. That must still count as "maybe more"
        # (the Show more button must not disappear).
        chesslist = self._chesslist(10) + [
            {"chessid": "99", "blackuid": 3, "whiteuid": 4, "blacknick": "C", "whitenick": "D"}
        ]
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: {"result": 0, "chesslist": chesslist})
        user = RemoteUser(source="fox", user_id="1", name="A")

        page = FoxGameSource().list_games_page(user, 1, 10)

        assert len(page.games) == 10
        assert page.has_more is True
        assert page.total is None

    def test_list_games_page_short_first_page_is_complete(self, monkeypatch):
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: {"result": 0, "chesslist": self._chesslist(4)})
        user = RemoteUser(source="fox", user_id="1", name="A")

        page = FoxGameSource().list_games_page(user, 1, 10)

        assert len(page.games) == 4
        assert page.has_more is False
        assert page.total == 4

    def test_list_games_page_second_fetches_full_once_then_caches(self, monkeypatch):
        urls = []

        def fake_get_json(url, **kwargs):
            urls.append(url)
            return {"result": 0, "chesslist": self._chesslist(12)}

        monkeypatch.setattr(fox, "_get_json", fake_get_json)
        user = RemoteUser(source="fox", user_id="1", name="A")
        source = FoxGameSource()

        source.list_games_page(user, 1, 10)
        second = source.list_games_page(user, 2, 10)

        assert "fetchnum=200" in urls[-1]
        assert len(urls) == 2
        assert second.total == 12
        assert [g.game_id for g in second.games] == ["11", "12"]
        assert second.has_more is False

        third = source.list_games_page(user, 3, 10)  # served from cache, no request
        assert len(urls) == 2
        assert third.games == []
        assert third.total == 12

    def test_list_games_page_cache_is_per_user(self, monkeypatch):
        urls = []

        def fake_get_json(url, **kwargs):
            urls.append(url)
            return {"result": 0, "chesslist": self._chesslist(12)}

        monkeypatch.setattr(fox, "_get_json", fake_get_json)
        source = FoxGameSource()

        source.list_games_page(RemoteUser(source="fox", user_id="1", name="A"), 2, 10)
        source.list_games_page(RemoteUser(source="fox", user_id="2", name="B"), 2, 10)

        # A second user must trigger a fresh full fetch rather than reuse user 1's cache.
        assert len(urls) == 2

    def test_fetch_sgf(self, monkeypatch):
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: {"result": 0, "chess": "(;GM[1])"})
        assert FoxGameSource().fetch_sgf(RemoteGame(source="fox", game_id="1")) == "(;GM[1])"

    def test_fetch_sgf_normalizes_literal_newlines(self, monkeypatch):
        # Fox returns escaped line breaks as the literal text "\r\n"/"\n".
        raw = "(;GM[1]\\r\\nSZ[19]\\r\\nC[a\\nb])"
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: {"result": 0, "chess": raw})

        sgf = FoxGameSource().fetch_sgf(RemoteGame(source="fox", game_id="1"))

        assert sgf == "(;GM[1]\nSZ[19]\nC[a\nb])"
        assert "\\r" not in sgf and "\\n" not in sgf

    def test_fetch_sgf_error(self, monkeypatch):
        monkeypatch.setattr(fox, "_get_json", lambda url, **k: {"result": 1, "resultstr": "bad"})
        with pytest.raises(FoxError):
            FoxGameSource().fetch_sgf(RemoteGame(source="fox", game_id="1"))
