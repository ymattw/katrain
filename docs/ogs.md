# Design: loading games from OGS (online-go.com)

This document describes the OGS game-loading feature, added on top of the
generic `OnlineGameSource`/`OnlineGamePanel` architecture introduced for Fox
(see `docs/fox.md`). Unlike Fox, OGS has real pagination, so the panel needed no
changes beyond a new tab.

## 1. Goal

Let users load their game records from OGS by username, from inside KaTrain's
**Load Game** dialog, without any OGS login or API key.

## 2. Final UX

- The Load dialog tab bar gains a third tab: **Local machine** | **Fox Weiqi** |
  **OGS**.
- The OGS tab uses the same `OnlineGamePanel`: username field + Search + status
  on the left, scrollable game list on the right.
- Search shows the first 10 games; **Show more** fetches the next 10 (OSG is
  truly paginated). The row total is known immediately from the API `count`.
- Rows, selection, result colouring, double-click/Load, and the remembered
  username (`general/ogs_username`) all behave exactly like Fox.

## 3. OGS public API (no login)

No special User-Agent is required; a descriptive one and
`Accept: application/json,text/plain,*/*` are sent.

| purpose | URL |
|---|---|
| user lookup | `GET https://online-go.com/api/v1/players/?username=<name>` |
| game list | `GET https://online-go.com/api/v1/players/<id>/games?ordering=-id&page=<n>&page_size=<m>` |
| SGF | `GET https://online-go.com/api/v1/games/<id>/sgf` |

### User lookup

Returns a paginated envelope with a `results` array of player objects:
`id`, `username`, `ranking`, `professional`. Several similar accounts can be
returned for a prefix query, so `lookup` disambiguates with
`select_exact_match(query, candidates)` from `katrain/core/online.py`.

### Game list

- The default ordering is **oldest first**; `ordering=-id` gives newest first,
  which is what the panel expects.
- Real pagination: `count` is the total number of games, `next` is the URL of
  the following page (or `null`). `page`/`page_size` are honoured.
- Per-game fields used: `id`, `started`, `ended`, `outcome`, `black_lost`,
  `white_lost`, `annulled`, `players.black`/`players.white` (each with `id`,
  `username`, `ranking`, `professional`).
- There is no move count in the list response, so the `moves` column is empty
  for OGS.

### Rank encoding

OGS's `ranking` is on a scale where `30 = 1d`. Mirroring OGS's own
`rankString`:

- `ranking < 30` → `ceil(30 - ranking)` kyu (`18.97 → 12k`, `29.1 → 1k`)
- otherwise → `floor(ranking - 29)` dan (`30 → 1d`, `38 → 9d`)
- professional accounts → `ranking - 36` p (and `ranking - 1000 - 36` when
  `ranking > 900`)

The list uses the player's **current** `ranking` for display only. The
container SGF carries the authoritative rank at the time of the game (`BR`/`WR`),
which KaTrain parses when the game is loaded — so no per-game historical data is
fetched or computed.

### Result encoding

`outcome` is a human string and `black_lost`/`white_lost` say who lost:

- `Resignation` → `X+R`; `Timeout` → `X+T`; `"<n> points"` → `X+<n>`.
- `annulled`, or both/neither side lost, → `Draw`.

### Ongoing games

OGS answers **HTTP 403** for the SGF of a game that has not ended (`ended` is
`null`). Such games are filtered out of the list so a row can always be loaded.

## 4. Architecture and files

### `katrain/core/ogs.py` (new)

`OgsGameSource(OnlineGameSource)` using `urllib3`:

- `_get_bytes`/`_get_json`/`_get_text` — GET with UA/Accept, raise `OgsError`.
- `ogs_rank_label(ranking, professional=False)` — conversion above.
- `ogs_result(outcome, black_lost, white_lost, annulled=False)` — result above.
- `lookup(query)` — `select_exact_match` over the API results.
- `list_games(user, limit=0)` — first page, newest first (interface
  completeness; the GUI uses `list_games_page`).
- `list_games_page(user, page, page_size)` — requests
  `ordering=-id&page=&page_size=`, maps rows with `_make_game`, and returns
  `GamePage(games, total=count, has_more=next is not None)`. `_make_game`
  returns `None` for ongoing games and games the user did not play.
- `fetch_sgf(game)` — raw SGF text from `/games/<id>/sgf`.

`key = "ogs"`, `display_name = "OGS"`, `query_label = "OGS username"`.

No newline normalisation is needed: OGS returns ordinary SGF, unlike Fox.

### `katrain/gui/widgets/onlinegames.py`

Unchanged: the panel is server-agnostic and already paged.

### `katrain/gui/popups.py` / `katrain/popups.kv`

- `LoadSGFPopup.__init__` builds a panel per source and stores them in
  `self.online_panels` (`{"fox": ..., "ogs": ...}`), adding each to its
  `{name}_screen`; `on_submit` routes to the active panel.
- KV gains an `OGS` `LoadSourceTab` and an `ogs` `Screen`.
- `LoadSourceTab.size_hint_x` becomes `1` (was `0.5, 1`) so N tabs share the row
  equally.

### i18n

New strings: `OGS` and `OGS username`, translated for all active locales.

## 5. Data flow

```
username ─► OgsGameSource.lookup ─► RemoteUser
          └► list_games_page(user, page, 10)
                 └► GET players/<id>/games?ordering=-id&page=<n>&page_size=10
                        └► GamePage(rows, total=count, has_more=bool(next))
                               └► OnlineGamePanel._render_page
                        └► select ─► fetch_sgf ─► GET games/<id>/sgf
                               └► KaTrain.load_sgf_content ─► _do_new_game
```

All network work happens on a daemon thread and is marshalled back with
`Clock.schedule_once` by the shared panel.

## 6. Pitfalls

1. **Default ordering is oldest first.** Always pass `ordering=-id` to get the
   newest games first; without it the panel would page from 2014.
2. **Ongoing games 403 their SGF.** Filter out rows whose `ended` is null.
3. **Prefix matches.** `players/?username=foo` may return `foo` and `foobar`;
   use `select_exact_match` rather than taking `results[0]`.
4. **No move count** in the list response; leave `move_count=None`.
5. **Tab width.** `LoadSourceTab` used a hard-coded `size_hint: 0.5, 1`; use
   `size_hint_x: 1` so the layout works with two or more tabs.

## 7. Tests

`tests/test_ogs.py` (no network, `_get_json`/`_get_text` monkeypatched):

- `ogs_rank_label` kyu/dan/pro/invalid.
- `ogs_result` resignation/timeout/points/annulled/unknown, including the
  `black_lost`/`white_lost` → colour direction.
- `lookup` exact match, ambiguous prefix (`None`), no results.
- `list_games_page` URL (`ordering=-id`, `page`, `page_size`), field mapping,
  `user_color`, `total`/`has_more`, and that ongoing/other-player games are
  skipped.
- `fetch_sgf` success and empty-record error.

`tests/test_online.py` additionally covers `select_exact_match` itself.

## 8. Verification checklist

- `ruff check` / `ruff format --check` clean.
- `pytest tests` passes.
- `python i18n.py` exits 0.
- `katrain/popups.kv` and `katrain/gui.kv` parse.
- Live check: `lookup("anoek")` → uid 1; `list_games_page` returns
  `total=583`, `has_more=True`, and `fetch_sgf` returns SGF text.

## 9. Out of scope / future

- The list total (`count`) includes ongoing games, which are hidden; the
  "of N" count can therefore be a little larger than the number of loadable
  games.
- Adding another server only needs a new `OnlineGameSource`, a tab/screen, and
  a panel entry in `LoadSGFPopup.online_panels`.
