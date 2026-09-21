# Design: loading games from Fox Weiqi (foxwq.com)

This document describes how to re-implement the Fox game-loading feature from
`origin/main` and reach the current state. It captures the final UX, the Fox
public API behavior we reverse-engineered, the architecture, and the Kivy/KaTrain
pitfalls found along the way.

## 1. Goal

Let users load game records directly from Fox Weiqi by username, from inside
KaTrain's existing **Load Game** dialog, without any Fox login or API key.

## 2. Final UX

- The Load dialog gets a top-level tab bar: **Local machine** | **Fox Weiqi**.
  The local tab is the existing `I18NFileBrowser`, unchanged (Ctrl-L still opens
  the dialog on the local tab by default).
- The `fast analysis` / `rewind` checkboxes stay shared above the tab content.
- Fox tab layout: left pane = username field + Search button + status line;
  right pane = scrollable game list.
- Username field: placeholder/label **"Fox username"**; accepts only
  `^[a-zA-Z0-9_-]+$` after trimming. The Search button is hidden/collapsed and
  disabled while the input is empty or invalid; Enter also re-validates.
- On search: resolve username → UID, fetch only the 10 most recent games (fast),
  and show them. The first **Show more** then fetches the full list (up to 200)
  in one request and reveals the next 10; every later **Show more** is local and
  instant. It hides when everything is shown. The API has no pagination, so a
  larger `fetchnum` is the only way to reach older games.
- Game rows are a table with columns: `date (left-aligned) | players | result |
  moves`. No row borders; the selected row gets a subtle background. The result
  column is coloured green (user won), red (user lost), grey (draw/unknown).
- Players are listed with the queried user first, each side tagged with a
  colour circle (`●` / `○`).
- One click selects a row; double click (or the **Load** button) fetches the SGF
  and loads it into Analysis mode.
- The button row shows **Show more** on the left and **Load** pinned to the
  right.
- The last used username is remembered.

## 3. Fox public API (no login)

All requests need a mobile User-Agent, e.g.
`Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15`.

| purpose | URL |
|---|---|
| user lookup | `https://newframe.foxwq.com/cgi/QueryUserInfoPanel?srcuid=0&username=<name>` |
| game list | `https://h5.foxwq.com/yehuDiamond/chessbook_local/YHWQFetchChessList?srcuid=0&dstuid=<uid>&type=1&lastcode=0&searchkey=&uin=<uid>&fetchnum=<n>` |
| SGF | `https://h5.foxwq.com/yehuDiamond/chessbook_local/YHWQFetchChess?chessid=<id>` |

Responses are JSON with a `result` field (`0` = ok, otherwise `resultstr`).

### User lookup

Returns `uid`, `username`, `dan`, `totalwin`, `totallost`, `totalequal`, etc.

Note: `totalwin/totallost/totalequal` are per-type counters and are **not** a
reliable "total games" number (e.g. a pro account can report `3` while the list
has hundreds). Do not use them for the list total.

### Game list

- `type=1` is used. It returns `fetchnum + 1` rows (the `+1` looks like a
  boundary/ongoing entry), so `fetchnum=100` → 101 rows.
- `fetchnum` is the **only** useful parameter and the server **caps it at 200**:
  `fetchnum=5`→6, `20`→21, `100`→101, `150`→151, `199`→200, `>=200`→200.
- **No pagination exists.** `lastcode` (numeric or a real chessid), `page`,
  `pageno`, `pagenum`, `pageindex`, `p`, `pn`, `start`, `offset`, `skip`,
  `begin`, `beginindex`, `index`, `startindex`, `next`, `nextcode`, `lasttime`,
  `before` all return the same first page, and the response's `lastcode` is
  always `0`.
- The list is a **chessbook**, so it can contain games the account only
  saved/recorded. Keep only games where the account is Black or White, and
  dedupe by `chessid`.
- Useful per-game fields: `chessid`, `starttime`, `blacknick`, `blackdan`,
  `blackuid`, `whitenick`, `whitedan`, `whiteuid`, `winner`, `point`, `reason`,
  `movenum`.

### Rank encoding

`dan` is an integer; empirically (confirmed against SGF `BR`/`WR` tags):

- `dan >= 100` → `P{dan-99}d` (pro)
- `dan >= 18` → `{dan-17}d` (amateur)
- `dan > 0` → `{18-dan}k`

e.g. `18→1d`, `19→2d`, `20→3d`, `26→9d`, `103→P4d`.

### Result encoding

- `winner`: `1` = Black, `2` = White, `0` = draw.
- `reason`: `1` = points, `2` = timeout, `3` = resignation, `4` = resignation.
- **`point` is in centipoints**: `2275` means 22.75. Format as
  `B+22.75` / `W+R` / `B+T`.

### Komi / SGF quirks

SGFs come from `AP[foxwq]` and are nonstandard. `pysgf` already corrects the Fox
komi when it sees `AP[foxwq]` (see `pysgf/parser.py`), so no extra work is
needed for komi. Just parse the fetched SGF text with `KaTrainSGF.parse_sgf`.

Fox also encodes line breaks as the **literal text** `\r\n` / `\n` (raw bytes
`5c 72 5c 6e`), not real newlines. pysgf then fails with
`Parse Error: unexpected character at ...`. There is no existing workaround in
KaTrain or pysgf, so `FoxGameSource.fetch_sgf` normalises them before returning:

```python
sgf.replace("\\r\\n", "\n").replace("\\r", "\n").replace("\\n", "\n")
```

## 4. Architecture and files

### `katrain/core/online.py` (new)

Server-agnostic types and interface, no Kivy imports so it is unit-testable:

```python
@dataclass
class RemoteUser:
    source: str
    user_id: str
    name: str
    rank: Optional[str] = None
    record: Optional[str] = None

@dataclass
class RemoteGame:
    source: str
    game_id: str
    date: Optional[str] = None
    black_name: str = "Black"
    black_rank: Optional[str] = None
    white_name: str = "White"
    white_rank: Optional[str] = None
    result: Optional[str] = None
    move_count: Optional[int] = None
    user_color: Optional[str] = None   # "B"/"W" if the queried user played
    raw: Dict[str, Any] = field(default_factory=dict)

@dataclass
class GamePage:
    games: List[RemoteGame] = field(default_factory=list)
    total: Optional[int] = None    # known total, or None
    has_more: bool = False         # whether a following page exists

class OnlineGameSource:
    key = ""
    display_name = ""
    query_label = ""
    query_placeholder = ""

    def lookup(self, query: str) -> Optional[RemoteUser]: ...
    def list_games(self, user: RemoteUser, limit: int = 0) -> List[RemoteGame]: ...
    def list_games_page(self, user: RemoteUser, page: int, page_size: int) -> GamePage: ...
    def fetch_sgf(self, game: RemoteGame) -> str: ...
```

`lookup` returns a single user (or `None`). Sources whose server can return
several candidates by name disambiguate with the `select_exact_match(query,
candidates)` helper: it prefers an exact name/ID match, accepts a lone
candidate, and returns `None` for an ambiguous set.

`limit = 0` means "server default/maximum". `list_games_page` is the primitive
the GUI uses: it returns one 1-based page plus the pagination state. The
base-class default slices a growing prefix, so a source with real pagination
overrides it. `FoxGameSource` overrides it with the one-shot-full-fetch
strategy described below, keeping all server-specific behavior out of the GUI.

### `katrain/core/fox.py` (new)

Implements `FoxGameSource` using `urllib3` (already a dependency). Constants:

```python
# module-private
_QUERY_USER_URL = "https://newframe.foxwq.com/cgi/QueryUserInfoPanel"
_GAME_LIST_URL = "https://h5.foxwq.com/yehuDiamond/chessbook_local/YHWQFetchChessList"
_FETCH_GAME_URL = "https://h5.foxwq.com/yehuDiamond/chessbook_local/YHWQFetchChess"
_MOBILE_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"

# The public Fox recent-games endpoint (no user login required) returns at most
# 200 rows and has no pagination: lastcode/page/offset etc. are all ignored.
_MAX_GAMES = 200
```

Key functions:

- `_get_json(url, timeout=20)` — `urllib3.PoolManager` GET with UA/Accept headers,
  raises `FoxError` on HTTP/JSON errors.
- `fox_rank_label(dan)` — rank mapping above.
- `parse_result(winner, point, reason)` — result string, `point/100`, `T`/`R`/`Draw`.
- `FoxGameSource.lookup(query)` — URL-encode the name, `result==0`, return the
  `RemoteUser` (or `None`).
- `FoxGameSource.list_games(user, limit=0)`:
  ```python
  limit = max(1, min(limit or _MAX_GAMES, _MAX_GAMES))
  # build URL with fetchnum=limit
  # keep only games whose blackuid/whiteuid == user_id, dedupe by chessid
  ```
- `FoxGameSource.list_games_page(user, page, page_size)` — pagination without a
  server cursor: page 1 is `list_games(user, limit=page_size)`; page 2 fetches
  the whole capped list once (cached per `user_id`); later pages are slices of
  that cache. Returns a `GamePage` (`total` known from page 2 on).
- `FoxGameSource.fetch_sgf(game)` — GET by `chessid`, return `data["chess"]`.

`key = "fox"`, `display_name = "Fox Weiqi"`, `query_label = "Fox username"`.

### `katrain/gui/widgets/onlinegames.py` (new)

`OnlineGamePanel(BoxLayout)` + `RemoteGameRow(ToggleButtonBehavior, BoxLayout)`.
Registered in `katrain/gui/widgets/__init__.py` (`OnlineGamePanel`,
`RemoteGameRow`).

Panel properties: `source`, `katrain`, `load_popup`, `selected_game`,
`has_more`, `busy`, `status_text`, `status_error`, `query_valid`.

Constants: `PAGE_SIZE = 10`, `QUERY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")`.

Behavior:
- `search()` bumps a request generation counter, validates the query, saves the
  username to config, resets the list, then runs the lookup on a worker thread.
- `_do_search` (worker): `lookup` → `list_games_page(user, 1, PAGE_SIZE)`,
  schedules `_search_done(seq, user, page)` on the Kivy main thread via
  `Clock.schedule_once`.
- `_search_done`: keep the resolved user, then `_render_page(1, page)`.
- `show_more()`: `_load_page(self._page + 1)`, i.e. ask the source for exactly
  one specific page.
- `_load_page`/`_do_load_page`: worker calls
  `source.list_games_page(user, page, PAGE_SIZE)`; `_page_loaded` appends the
  rows via `_render_page`. The panel never knows how the source fulfils a page.
- `_render_page`: append rows, update `_page`, `_shown`, `_total`, `has_more`.
- Status text depends on whether the source reported a total:
  `"Showing {} games"` until then, `"Showing {} of {} games"` after, and
  `"Loading more games..."` while a page is being fetched. Because fetching a
  page runs while `busy` is set, a search cannot overlap it; `_request_seq` is a
  defensive stale-response guard.
- `load_selected()` / double-click: worker calls `fetch_sgf`, then
  `_load_done(sgf)` dismisses the popup and calls
  `katrain.load_sgf_content(sgf, fast=..., rewind=...)`.
- `_update_buttons`: recompute `query_valid`, toggle Search visibility/disabled,
  Load disabled (nothing selected), Show more disabled (`not has_more`).
- `_make_row`: builds the table row via `RemoteGameRow` and `_matchup`/`_row_color`.

Row helpers:
- `_matchup(game)` — user first, `●`/`○` before the user / after the opponent.
  Note: `●`(U+25CF)/`○`(U+25CB) are used instead of `⚫`(U+26AB)/`⚪`(U+26AA)
  because the latter are not in KaTrain's CJK font and render as boxes (verified
  by rendering). The circles are drawn in the label colour, so the fill/hollow
  choice is derived from `Theme.TEXT_COLOR`: on the default dark/white-text theme
  a filled `●` reads as a white stone and a hollow `○` as black (and vice versa
  for a custom light theme).
- `_row_color(game)` — green if `result` starts with `user_color`, red if it
  starts with the other colour, grey for draw/unknown.

Status: keep the i18n key + args in Python and build `status_text` atomically:
```python
def _set_status(self, key, *args, error=False):
    self._status_key = key
    self._status_args = args
    self.status_error = error
    self._apply_status()

def _apply_status(self):
    if not self._status_key:
        self.status_text = ""
    elif self._status_args:
        self.status_text = i18n._(self._status_key).format(*self._status_args)
    else:
        self.status_text = i18n._(self._status_key)
```
Register `i18n.callbacks.append(self._on_language_change)` in `__init__` so the
status retranslates on language switch.

### `katrain/gui/popups.py`

`LoadSGFPopup` gains `current_source = StringProperty("local")`, `select_source`,
creates the `OnlineGamePanel` and adds it to the Fox `Screen`, and routes
`on_submit` to the active tab.

### `katrain/popups.kv`

- `<LoadSourceTab@SizedToggleButton>` styled like `PlayAnalyzeButton`.
- `<RemoteGameRow>`: horizontal `BoxLayout` with four `Label`s, no outline, a
  selection `canvas.before` rectangle.
- `<OnlineGamePanel>`: left/right split; left column uses
  `size_hint_y: None; height: self.minimum_height; pos_hint: {'top': 1}`; the
  bottom row holds Show more + a flexible `Widget` spacer + Load.
- `<LoadSGFPopup>`: tab bar + shared fast/rewind + `ScreenManager` with `local`
  (the existing `I18NFileBrowser`) and `fox` screens.

### `katrain/__main__.py`

- `load_sgf_content(sgf_content, fast=False, rewind=True)` — parse SGF text and
  call `_do_new_game(..., sgf_filename="")`. Empty filename marks it as a loaded
  game (human players) while Ctrl-S still opens Save As.
- Set `popup_contents.popup = self.fileselect_popup` so the panel can dismiss it.

### Other

- Remembered username lives in `general/fox_username`, but it is **not** added to
  the packaged `katrain/config.json` template: there is no meaningful per-user
  default, and `OnlineGamePanel._save_username` creates the key in the user's
  `~/.katrain/config.json` on the first search. Reads go through
  `config("general/fox_username", "")`, so a missing key is harmless.
- `katrain/gui/theme.py`: `ONLINE_GAME_WIN_COLOR` (green),
  `ONLINE_GAME_LOSS_COLOR` (red), `ONLINE_GAME_DRAW_COLOR` (grey).
- i18n: add the new strings to `en` and translate the active locales.

## 5. Data flow

```
username ─► FoxGameSource.lookup ─► RemoteUser
          └► list_games_page(user, page=1, 10) ─► GamePage(10 rows)
                 └► OnlineGamePanel._render_page ─► rows (10)
                        └► Show more
                        │      └► list_games_page(user, page=2, 10)
                        │             └► inside FoxGameSource: list_games(limit=200)
                        │                  cached; GamePage(total=N, 10 rows)
                        │             └► _render_page ─► rows (10)
                        │      └► Show more (page 3+) ─► cached slice
                        └► select ─► FoxGameSource.fetch_sgf ─► SGF text
                               └► KaTrain.load_sgf_content ─► _do_new_game
```

Network work always happens on a daemon thread; results/callbacks are marshalled
back with `Clock.schedule_once`.

## 6. Pitfalls (do not reintroduce)

1. **Kivy vertical `BoxLayout` packs fixed-height children at the bottom.**
   The left column needs `size_hint_y: None; height: self.minimum_height;
   pos_hint: {'top': 1}` to sit under the tab bar. A trailing `Widget:
   size_hint_y: 1` also works but was replaced by `pos_hint` for clarity.
2. **`TextInput.padding_y` and `_adjust_viewport`.** If
   `height - padding_top - padding_bottom - line_height < 0`, Kivy sets a
   nonzero `scroll_y` that toggles as you type, so the text visibly jumps. Use
   `padding_y: max(0, (self.height - self.line_height) / 2)` (the real line
   height, not `font_size`).
3. **Plain `Label`/`TextInput` use Kivy's default Roboto, which has no CJK
   glyphs.** Set `font_name: i18n.font_name` on the row labels and the username
   field.
4. **Dynamic status text must be atomic.** Assigning `status_key` and
   `status_args` as separate Kivy properties makes the KV binding evaluate with a
   stale args tuple (`IndexError: Replacement index ... out of range`). Build the
   text in Python and retranslate via `i18n.callbacks`.
5. **`fetchnum`/pagination.** Only `fetchnum` works and the server caps it at
   200. Do not try `lastcode`/`page`/etc. Pagination is therefore implemented in
   `FoxGameSource.list_games_page` (page 1 cheap, page 2 fetches the full capped
   list once and caches it); the GUI only ever requests page N and must not grow
   Fox-specific fetch logic.
6. **Filter and dedupe.** The chessbook can contain games the user did not play.
   Keep only games where the user is Black or White, dedupe by `chessid`, so the
   "of N" total is the user's real game count.
7. **Retranslation.** The static KV strings retranslate automatically because
   KaTrain registers `i18n._` observers. Any text set from Python (status,
   `moves` column) must either be a KV `i18n._` expression or be refreshed from
   an `i18n.callbacks` callback.
8. **A full first page only means "maybe more".** The server's `fetchnum+1`
   boundary row can be filtered out (saved games, duplicate `chessid`), so page 1
   may contain exactly `PAGE_SIZE` games even when hundreds remain. Treat a full
   page as `has_more=True`; only a short page is definitely the end. Concluding
   "no more" from `len(games) == PAGE_SIZE` hides the **Show more** button.

## 7. i18n strings

New strings: `Local machine`, `Fox Weiqi`, `Fox username`, `Search`, `Show more`,
`Load`, `Loading game...`, `{} moves`, `Please enter a username`, `Searching...`,
`No matching user found`, `This account has no public games`, `Showing {} games`,
`Showing {} of {} games`, `Loading more games...`. (`Found {} games` is still in
`en` but no longer used by the paged panel.)

Workflow: add English entries to
`katrain/i18n/locales/en/LC_MESSAGES/katrain.po`, run `python i18n.py` to
propagate/compile, then fill real translations for every active locale and clear
the `TODO` comments. `i18n.py` must exit `0`.

## 8. Tests

`tests/test_fox.py` (no network, `_get_json` monkeypatched):

- `fox_rank_label` for dan/kyu/pro and invalid input.
- `parse_result` centipoints (`350→B+3.5`, `2275→B+22.75`, `25→W+0.25`), resign,
  timeout, draw.
- `lookup` by name; numeric input is treated as a nickname (ID support was
  dropped); error path raises `FoxError`.
- `list_games`: mapping, `user_color`, filtering of unplayed games and duplicate
  `chessid`s, and that `fetchnum` is sent (`50`), defaults to `200`, and is
  clamped (`9999`→`200`).
- `list_games_page`: page 1 is a cheap `fetchnum=page_size`; page 2 fetches
  `fetchnum=200` once and caches; page 3+ is served from cache without a request;
  the cache is keyed by `user_id`.
- `fetch_sgf` success/error.

`tests/test_online.py` covers the base `OnlineGameSource.list_games_page`
default (growing-prefix refetch, `has_more`, and `total` once exhausted).

## 9. Verification checklist

- `ruff check` / `ruff format --check` clean.
- `pytest tests` passes.
- `python i18n.py` exits 0.
- `katrain/gui.kv` and `katrain/popups.kv` parse; `OnlineGamePanel` instantiates.
- Switch language `en → de → cn` and confirm the status and `{} moves` column
  retranslate.
- Reveal all rows and confirm the count ends at `Showing N of N` (not N-10).
- Live check: `list_games` returns `fetchnum+1` rows and at most 200.

## 10. Out of scope / future

- No OGS or other server yet; add another `OnlineGameSource` and a tab.
- No way past 200 games on this endpoint. If Fox exposes a real cursor, only
  `FoxGameSource.list_games_page` needs to change; the GUI stays page-based.
- The rank kyu branch was extrapolated; the SGF carries authoritative
  `BR`/`WR` labels if needed.
