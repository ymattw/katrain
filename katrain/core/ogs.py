"""Load game records from Online Go Server (online-go.com).

Uses the public OGS REST API, which requires no login:

- ``GET /api/v1/players/?username=<name>`` resolves a name to an account.
- ``GET /api/v1/players/<id>/games`` lists a player's games (paginated).
- ``GET /api/v1/games/<id>/sgf`` returns the raw SGF text.
"""

import json
import math
import re
import urllib.parse
from typing import Any, Dict, List, Optional

import urllib3

from katrain.core.online import GamePage, OnlineGameSource, RemoteGame, RemoteUser, select_exact_match

_PLAYERS_URL = "https://online-go.com/api/v1/players/"
_GAMES_URL = "https://online-go.com/api/v1/players/{player_id}/games"
_SGF_URL = "https://online-go.com/api/v1/games/{game_id}/sgf"
_USER_AGENT = "KaTrain (+https://github.com/sanderland/katrain)"

_HTTP = urllib3.PoolManager()
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json,text/plain,*/*"}


class OgsError(Exception):
    """Raised when OGS returns an error or an unexpected response."""


def _get_bytes(url: str, timeout: float = 20) -> bytes:
    try:
        response = _HTTP.request("GET", url, headers=_HEADERS, timeout=timeout)
    except urllib3.exceptions.HTTPError as e:
        raise OgsError(str(e)) from e
    if response.status != 200:
        raise OgsError(f"OGS server returned HTTP {response.status}")
    return response.data


def _get_json(url: str, timeout: float = 20) -> Dict[str, Any]:
    try:
        return json.loads(_get_bytes(url, timeout).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise OgsError("Invalid response from OGS server") from e


def _get_text(url: str, timeout: float = 20) -> str:
    try:
        return _get_bytes(url, timeout).decode("utf-8")
    except UnicodeDecodeError as e:
        raise OgsError("Invalid response from OGS server") from e


def ogs_rank_label(ranking: Any, professional: Any = False) -> Optional[str]:
    """Convert OGS's numeric rank to a display label.

    OGS uses ``30 = 1d``, so ``r < 30`` is ``ceil(30 - r)`` kyu and otherwise
    ``floor(r - 29)`` dan. Pro accounts encode their rank as ``ranking - 36``
    (mirroring OGS's own ``rankString``).
    """
    try:
        ranking = float(ranking)
    except (TypeError, ValueError):
        return None
    if professional:
        if ranking > 900:
            ranking -= 1000
        return f"{int(ranking - 36)}p"
    if ranking < 30:
        return f"{math.ceil(30 - ranking)}k"
    return f"{math.floor(ranking - 29)}d"


def ogs_result(outcome: Any, black_lost: Any, white_lost: Any, annulled: Any = False) -> str:
    """Format an OGS game outcome as an SGF-style result (e.g. ``W+R``)."""
    if annulled or bool(black_lost) == bool(white_lost):
        return "Draw"
    color = "W" if black_lost else "B"
    outcome = str(outcome or "").strip()
    if outcome == "Resignation":
        return f"{color}+R"
    if outcome == "Timeout":
        return f"{color}+T"
    match = re.match(r"^([0-9.]+)\s+points?$", outcome)
    if match:
        return f"{color}+{match.group(1)}"
    return f"{color}+?"


class OgsGameSource(OnlineGameSource):
    key = "ogs"
    display_name = "OGS"
    query_label = "OGS username"
    query_placeholder = ""

    def lookup(self, query: str) -> Optional[RemoteUser]:
        query = (query or "").strip()
        if not query:
            raise OgsError("Please enter an OGS username")

        url = f"{_PLAYERS_URL}?username={urllib.parse.quote(query)}"
        data = _get_json(url)
        candidates = [
            RemoteUser(
                source=self.key,
                user_id=str(player.get("id")),
                name=player.get("username") or query,
                rank=ogs_rank_label(player.get("ranking"), player.get("professional")),
            )
            for player in data.get("results", [])
        ]
        return select_exact_match(query, candidates)

    def list_games(self, user: RemoteUser, limit: int = 0) -> List[RemoteGame]:
        params = {"ordering": "-id"}
        if limit:
            params["page_size"] = limit
        data = _get_json(self._games_url(user, params))
        user_id = str(user.user_id)
        return [g for g in (self._make_game(raw, user_id) for raw in data.get("results", [])) if g is not None]

    def list_games_page(self, user: RemoteUser, page: int, page_size: int) -> GamePage:
        # OGS has real pagination; ``-id`` lists the newest games first.
        params = {"ordering": "-id", "page": page, "page_size": page_size}
        data = _get_json(self._games_url(user, params))
        user_id = str(user.user_id)
        games = [g for g in (self._make_game(raw, user_id) for raw in data.get("results", [])) if g is not None]
        return GamePage(
            games=games,
            total=data.get("count"),
            has_more=data.get("next") is not None,
        )

    def fetch_sgf(self, game: RemoteGame) -> str:
        url = _SGF_URL.format(game_id=urllib.parse.quote(str(game.game_id)))
        sgf = _get_text(url)
        if not sgf.strip():
            raise OgsError("OGS returned an empty game record")
        return sgf

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _games_url(user: RemoteUser, params: Dict[str, Any]) -> str:
        base = _GAMES_URL.format(player_id=urllib.parse.quote(str(user.user_id)))
        return f"{base}?{urllib.parse.urlencode(params)}"

    def _make_game(self, raw: Dict[str, Any], user_id: str) -> Optional[RemoteGame]:
        # Ongoing games have no downloadable SGF (OGS answers 403), so skip them.
        if not raw.get("ended"):
            return None
        players = raw.get("players") or {}
        black = players.get("black") or {}
        white = players.get("white") or {}
        black_id = str(black.get("id", ""))
        white_id = str(white.get("id", ""))
        if user_id == black_id:
            user_color = "B"
        elif user_id == white_id:
            user_color = "W"
        else:
            return None
        return RemoteGame(
            source=self.key,
            game_id=str(raw.get("id", "")),
            date=(raw.get("started") or "").replace("T", " ")[:19],
            black_name=black.get("username") or "Black",
            black_rank=ogs_rank_label(black.get("ranking"), black.get("professional")),
            white_name=white.get("username") or "White",
            white_rank=ogs_rank_label(white.get("ranking"), white.get("professional")),
            result=ogs_result(raw.get("outcome"), raw.get("black_lost"), raw.get("white_lost"), raw.get("annulled")),
            move_count=None,
            user_color=user_color,
            raw=raw,
        )
