from katrain.core.online import GamePage, OnlineGameSource, RemoteGame, RemoteUser, select_exact_match


class _FakeSource(OnlineGameSource):
    """Source whose list_games returns the newest ``limit`` games."""

    def __init__(self, games):
        self._games = games
        self.calls = []

    def list_games(self, user, limit=0):
        self.calls.append(limit)
        return self._games[:limit] if limit else self._games


def _games(count):
    return [RemoteGame(source="fake", game_id=str(i)) for i in range(1, count + 1)]


def test_default_page_refetches_a_growing_prefix():
    source = _FakeSource(_games(25))
    user = RemoteUser(source="fake", user_id="1", name="A")

    first = source.list_games_page(user, 1, 10)
    second = source.list_games_page(user, 2, 10)

    assert source.calls == [11, 21]  # 1-based page -> limit = page * page_size + 1 (probe for next page)
    assert [g.game_id for g in first.games] == [str(i) for i in range(1, 11)]
    assert [g.game_id for g in second.games] == [str(i) for i in range(11, 21)]
    assert first.has_more and second.has_more
    assert first.total is None and second.total is None  # more may remain


def test_default_page_reports_total_when_exhausted():
    source = _FakeSource(_games(12))
    user = RemoteUser(source="fake", user_id="1", name="A")

    first = source.list_games_page(user, 1, 10)
    second = source.list_games_page(user, 2, 10)

    assert first.has_more
    assert not second.has_more
    assert second.total == 12
    assert second.games == _games(12)[10:]


def test_gamepage_defaults():
    page = GamePage()
    assert page.games == [] and page.total is None and page.has_more is False


def _user(name, user_id=None):
    return RemoteUser(source="fake", user_id=user_id or name, name=name)


def test_select_exact_match_prefers_exact_name():
    candidates = [_user("foobar", "1"), _user("foo", "2")]
    assert select_exact_match("foo", candidates).user_id == "2"


def test_select_exact_match_accepts_id():
    candidates = [_user("foo", "123"), _user("foobar", "456")]
    assert select_exact_match("456", candidates).name == "foobar"


def test_select_exact_match_accepts_lone_candidate():
    assert select_exact_match("Foo", [_user("foo", "1")]).name == "foo"


def test_select_exact_match_rejects_ambiguous_prefix():
    assert select_exact_match("foo", [_user("foobar", "1"), _user("foobaz", "2")]) is None
