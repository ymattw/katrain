"""Common types for loading game records from online Go servers.

Each server is implemented as an :class:`OnlineGameSource`; the GUI renders any
source with a generic panel, so adding a new server (e.g. OGS) only requires a
new subclass here.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RemoteUser:
    """A player account on an online server."""

    source: str
    user_id: str
    name: str
    rank: Optional[str] = None
    record: Optional[str] = None


@dataclass
class RemoteGame:
    """A game record listed on an online server, before its SGF is fetched."""

    source: str
    game_id: str
    date: Optional[str] = None
    black_name: str = "Black"
    black_rank: Optional[str] = None
    white_name: str = "White"
    white_rank: Optional[str] = None
    result: Optional[str] = None
    move_count: Optional[int] = None
    user_color: Optional[str] = None  # "B"/"W" if the queried user played this game
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GamePage:
    """One page of a user's games plus the pagination state it implies."""

    games: List[RemoteGame] = field(default_factory=list)
    # Total number of games the source can provide, when known (e.g. once a
    # server without a cursor had to fetch its whole capped list).
    total: Optional[int] = None
    has_more: bool = False


def select_exact_match(query: str, candidates: List[RemoteUser]) -> Optional[RemoteUser]:
    """Pick the account that exactly matches ``query`` from several candidates.

    A server may answer a lookup with similar accounts (e.g. ``foo`` returning
    ``foo`` and ``foobar``); prefer an exact match over the first result. If
    nothing matches exactly, a lone candidate is still accepted, while several
    ambiguous ones are rejected.
    """
    query = (query or "").strip()
    for user in candidates:
        if query and query in (user.name, user.user_id):
            return user
    return candidates[0] if len(candidates) == 1 else None


class OnlineGameSource:
    """Interface implemented by each online Go server."""

    key = ""
    display_name = ""
    query_label = ""
    query_placeholder = ""

    def lookup(self, query: str) -> Optional[RemoteUser]:
        """Resolve a user-provided account name (or ID) to a single account.

        Return ``None`` when nothing matches. Implementations that may get
        several candidates should disambiguate with :func:`select_exact_match`.
        """
        raise NotImplementedError

    def list_games(self, user: RemoteUser, limit: int = 0) -> List[RemoteGame]:
        """Return the available (recent) games for a user, newest first.

        ``limit`` of 0 means the server's default/maximum.
        """
        raise NotImplementedError

    def list_games_page(self, user: RemoteUser, page: int, page_size: int) -> GamePage:
        """Return one 1-based page of a user's games, newest first.

        This is the primitive the GUI uses. The default implementation only
        assumes ``list_games(limit=n)`` returns the newest ``n`` games, so it
        re-fetches a growing prefix per page (one row extra to detect whether a
        next page exists); sources with real pagination, or with a cheaper
        strategy, should override it.
        """
        limit = page * page_size
        games = self.list_games(user, limit=limit + 1)
        start = (page - 1) * page_size
        end = start + page_size
        return GamePage(
            games=games[start:end],
            total=len(games) if len(games) <= limit else None,
            has_more=len(games) > limit,
        )

    def fetch_sgf(self, game: RemoteGame) -> str:
        """Return the SGF text for a listed game."""
        raise NotImplementedError
