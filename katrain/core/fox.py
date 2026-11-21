"""Load game records from Fox Weiqi (foxwq.com).

Ported from ``download_by_name.py``. It uses the platform's public API, which
does not require login or authentication, and returns the recent games of a
given account (nickname or numeric UID).
"""

import json
import urllib.parse
from typing import Any, Dict, List, Optional

import urllib3

from katrain.core.online import GamePage, OnlineGameSource, RemoteGame, RemoteUser

_QUERY_USER_URL = "https://newframe.foxwq.com/cgi/QueryUserInfoPanel"
_GAME_LIST_URL = "https://h5.foxwq.com/yehuDiamond/chessbook_local/YHWQFetchChessList"
_FETCH_GAME_URL = "https://h5.foxwq.com/yehuDiamond/chessbook_local/YHWQFetchChess"
_MOBILE_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"

# The public Fox recent-games endpoint (no user login required) returns at most
# 200 rows and has no pagination: `lastcode`, `page`, `offset`, etc. are all
# ignored. So 200 is the most games we can load for an account at once.
_MAX_GAMES = 200

_HTTP = urllib3.PoolManager()
_HEADERS = {"User-Agent": _MOBILE_USER_AGENT, "Accept": "application/json,text/plain,*/*"}


class FoxError(Exception):
    """Raised when Fox returns an error or an unexpected response."""


def _get_json(url: str, timeout: float = 20) -> Dict[str, Any]:
    try:
        response = _HTTP.request("GET", url, headers=_HEADERS, timeout=timeout)
    except urllib3.exceptions.HTTPError as e:
        raise FoxError(str(e)) from e
    if response.status != 200:
        raise FoxError(f"Fox server returned HTTP {response.status}")
    try:
        return json.loads(response.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise FoxError("Invalid response from Fox server") from e


def fox_rank_label(dan: Any) -> Optional[str]:
    """Convert Fox's numeric rank encoding to a display label.

    Empirically: 18 -> 1d, 19 -> 2d, ... 26 -> 9d, 100+N -> pro Nd, 17 -> 1k, ...
    """
    try:
        dan = int(dan)
    except (TypeError, ValueError):
        return None
    if dan >= 100:
        return f"P{dan - 99}d"
    if dan >= 18:
        return f"{dan - 17}d"
    if dan > 0:
        return f"{18 - dan}k"
    return None


def parse_result(winner: Any, point: Any, reason: Any) -> str:
    """Format a Fox game result as an SGF-style string (e.g. ``W+R``).

    Fox reports the winning margin in centipoints (``2275`` means 22.75 points).
    """
    if winner == 0:
        return "Draw"
    color = "B" if winner == 1 else "W"
    if reason == 1:
        try:
            point = float(point) / 100.0
        except (TypeError, ValueError):
            point = 0
        return f"{color}+{point:g}" if point > 0 else f"{color}+?"
    if reason == 2:
        return f"{color}+T"
    return f"{color}+R"


class FoxGameSource(OnlineGameSource):
    key = "fox"
    display_name = "Fox Weiqi"
    query_label = "Fox username"
    query_placeholder = ""

    def __init__(self):
        super().__init__()
        # The endpoint has no cursor, so once a page beyond the first is asked
        # for we fetch the whole capped list and serve later pages from here.
        self._page_cache_user: Optional[str] = None
        self._page_cache: List[RemoteGame] = []

    def lookup(self, query: str) -> Optional[RemoteUser]:
        query = (query or "").strip()
        if not query:
            raise FoxError("Please enter a Fox username")

        url = f"{_QUERY_USER_URL}?srcuid=0&username={urllib.parse.quote(query)}"
        data = _get_json(url)
        if data.get("result") != 0:
            raise FoxError(data.get("resultstr") or "User not found")
        uid = str(data.get("uid", "")).strip()
        if not uid:
            raise FoxError("No UID found for this username")
        return RemoteUser(
            source=self.key,
            user_id=uid,
            name=data.get("username") or query,
            rank=fox_rank_label(data.get("dan")),
            record=(f"{data.get('totalwin', 0)}W {data.get('totallost', 0)}L {data.get('totalequal', 0)}D"),
        )

    def list_games(self, user: RemoteUser, limit: int = 0) -> List[RemoteGame]:
        limit = max(1, min(limit or _MAX_GAMES, _MAX_GAMES))
        uid = urllib.parse.quote(str(user.user_id))
        # Fox ignores pagination parameters (lastcode/page/offset all do nothing) and
        # only supports a `fetchnum` row limit, capped at `max_fetch` by the server.
        url = f"{_GAME_LIST_URL}?srcuid=0&dstuid={uid}&type=1&lastcode=0&searchkey=&uin={uid}&fetchnum={limit}"
        data = _get_json(url)
        if data.get("result") != 0:
            raise FoxError(data.get("resultstr") or "Failed to fetch game list")

        # The chessbook endpoint can include games the account only saved/recorded,
        # so keep only games where the queried account actually played, and dedupe.
        user_id = str(user.user_id)
        games = []
        seen = set()
        for g in data.get("chesslist", []):
            game_id = str(g.get("chessid", ""))
            if not game_id or game_id in seen:
                continue
            if user_id == str(g.get("blackuid", "")):
                user_color = "B"
            elif user_id == str(g.get("whiteuid", "")):
                user_color = "W"
            else:
                continue
            seen.add(game_id)
            games.append(
                RemoteGame(
                    source=self.key,
                    game_id=game_id,
                    date=g.get("starttime"),
                    black_name=g.get("blacknick") or "Black",
                    black_rank=fox_rank_label(g.get("blackdan")),
                    white_name=g.get("whitenick") or "White",
                    white_rank=fox_rank_label(g.get("whitedan")),
                    result=parse_result(g.get("winner"), g.get("point"), g.get("reason")),
                    move_count=g.get("movenum"),
                    user_color=user_color,
                    raw=g,
                )
            )
        return games

    def list_games_page(self, user: RemoteUser, page: int, page_size: int) -> GamePage:
        """Paginate a user's games without a server cursor.

        Page 1 is a cheap ``fetchnum=page_size`` request. Because there is no
        pagination on this endpoint, page 2 fetches the whole (capped) list in
        one request and caches it; every later page is a slice of that cache.
        """
        if page <= 1:
            games = self.list_games(user, limit=page_size)[:page_size]
            # A full page is ambiguous: the server's extra boundary row can be
            # filtered out (saved games, duplicates), so a full page only means
            # "maybe more". Short of a full page there is definitely nothing.
            complete = len(games) < page_size
            return GamePage(
                games=games,
                total=len(games) if complete else None,
                has_more=not complete,
            )

        if self._page_cache_user != user.user_id:
            self._page_cache = self.list_games(user)
            self._page_cache_user = user.user_id
        start = (page - 1) * page_size
        return GamePage(
            games=self._page_cache[start : start + page_size],
            total=len(self._page_cache),
            has_more=start + page_size < len(self._page_cache),
        )

    def fetch_sgf(self, game: RemoteGame) -> str:
        url = f"{_FETCH_GAME_URL}?chessid={urllib.parse.quote(str(game.game_id))}"
        data = _get_json(url)
        if data.get("result") != 0:
            raise FoxError(data.get("resultstr") or "Failed to download game record")
        sgf = data.get("chess", "")
        if not sgf:
            raise FoxError("Fox returned an empty game record")
        # Fox encodes line breaks as the literal text "\r\n"/"\n" rather than real
        # newlines, which the SGF parser rejects; turn them back into newlines.
        return sgf.replace("\\r\\n", "\n").replace("\\r", "\n").replace("\\n", "\n")
