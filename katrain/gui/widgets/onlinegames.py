"""Panel for loading games from an online Go server (e.g. Fox)."""

import re
import threading

from kivy.clock import Clock
from kivy.properties import BooleanProperty, ListProperty, ObjectProperty, StringProperty
from kivy.uix.behaviors import ToggleButtonBehavior
from kivy.uix.boxlayout import BoxLayout

from katrain.core.constants import OUTPUT_ERROR
from katrain.core.lang import i18n
from katrain.gui.theme import Theme

_PAGE_SIZE = 10  # games fetched/rendered per page
_QUERY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class RemoteGameRow(ToggleButtonBehavior, BoxLayout):
    """A selectable remote game, rendered as aligned table columns.

    A single click selects the row (toggle state); a double click emits
    ``on_double_tap`` so the panel can load it.
    """

    __events__ = ("on_double_tap",)

    game = ObjectProperty(None, allownone=True)
    row_color = ListProperty([1, 1, 1, 1])
    date_text = StringProperty("")
    players_text = StringProperty("")
    result_text = StringProperty("")
    move_count = ObjectProperty(None, allownone=True)

    def on_touch_down(self, touch):
        if touch.is_double_tap and self.collide_point(*touch.pos):
            self.dispatch("on_double_tap", touch)
            return True
        return super().on_touch_down(touch)

    def on_double_tap(self, touch):
        pass


class OnlineGamePanel(BoxLayout):
    """Query an :class:`OnlineGameSource` and load one of its games.

    This panel is server-agnostic: it only ever asks the source for one specific
    page via ``source.list_games_page(user, page, _PAGE_SIZE)`` and appends the
    returned rows. All knowledge of *how* to page (a real cursor, or Fox's
    "no cursor, so fetch everything on page 2 and cache it") lives in the
    source. The panel just tracks which page is shown and whether another
    exists.

    All network calls run on worker threads; results are handed back to the
    Kivy main thread with ``Clock.schedule_once``. ``busy`` serialises requests,
    so a stale response can never land after a new search (see ``_request_seq``
    for the defensive check).
    """

    source = ObjectProperty(None)
    katrain = ObjectProperty(None)
    load_popup = ObjectProperty(None, allownone=True)
    selected_game = ObjectProperty(None, allownone=True)
    has_more = BooleanProperty(False)
    busy = BooleanProperty(False)
    # Status shown under the query box. The text is built atomically in Python
    # from an i18n key + args, and refreshed on a language switch via `i18n.callbacks`.
    status_text = StringProperty("")
    status_error = BooleanProperty(False)
    query_valid = BooleanProperty(False)

    def __init__(self, source, katrain, load_popup=None, **kwargs):
        super().__init__(**kwargs)
        self.source = source
        self.katrain = katrain
        self.load_popup = load_popup
        self._user = None
        self._page = 0  # last successfully rendered page
        self._shown = 0  # rows rendered so far
        self._total = None  # total games, once the source knows it
        # Incremented on every search; callbacks from older requests are dropped.
        self._request_seq = 0
        self._status_key = ""
        self._status_args = ()
        i18n.callbacks.append(self._on_language_change)
        self.bind(selected_game=self._update_buttons, has_more=self._update_buttons, busy=self._update_buttons)
        self.ids.query.bind(text=self._update_buttons)
        Clock.schedule_once(self._restore_last_username, 0)

    @staticmethod
    def query_is_valid(text):
        return bool(_QUERY_RE.match((text or "").strip()))

    def _restore_last_username(self, *_args):
        last = self.katrain.config(f"general/{self.source.key}_username", "")
        if last:
            self.ids.query.text = last
        self._update_buttons()

    # -- user actions -----------------------------------------------------

    def search(self):
        if self.busy:
            return
        query = self.ids.query.text.strip()
        if not self.query_is_valid(query):
            self._set_status("Please enter a username", error=True)
            return
        self._request_seq += 1
        seq = self._request_seq
        self._save_username(query)
        self._reset_results()
        self._set_status("Searching...")
        self.busy = True
        self._run(self._do_search, query, seq)

    def show_more(self):
        if self.busy:
            return
        self._load_page(self._page + 1)

    def load_selected(self):
        if self.busy or self.selected_game is None:
            return
        self.busy = True
        self._set_status("Loading game...")
        self._run(self._do_load, self.selected_game)

    def on_submit(self):
        if self._page:
            self.load_selected()
        else:
            self.search()

    # -- worker-thread side ----------------------------------------------

    def _do_search(self, query, seq):
        try:
            user = self.source.lookup(query)
            if user is None:
                raise Exception("No matching user found")
            page = self.source.list_games_page(user, 1, _PAGE_SIZE)
        except Exception as e:
            self.katrain.log(f"{self.source.display_name} search failed: {e}", OUTPUT_ERROR)
            Clock.schedule_once(lambda _dt, error=e: self._search_failed(seq, error), 0)
            return
        Clock.schedule_once(lambda _dt: self._search_done(seq, user, page), 0)

    def _do_load_page(self, page, seq):
        try:
            result = self.source.list_games_page(self._user, page, _PAGE_SIZE)
        except Exception as e:
            self.katrain.log(f"{self.source.display_name} page {page} failed: {e}", OUTPUT_ERROR)
            Clock.schedule_once(lambda _dt, error=e: self._page_failed(seq, error), 0)
            return
        Clock.schedule_once(lambda _dt: self._page_loaded(seq, page, result), 0)

    def _do_load(self, game):
        try:
            sgf = self.source.fetch_sgf(game)
        except Exception as e:
            self.katrain.log(f"{self.source.display_name} download failed: {e}", OUTPUT_ERROR)
            Clock.schedule_once(lambda _dt, error=e: self._load_failed(error), 0)
            return
        Clock.schedule_once(lambda _dt: self._load_done(sgf), 0)

    # -- main-thread callbacks -------------------------------------------

    def _search_done(self, seq, user, page):
        if seq != self._request_seq:
            return
        self.busy = False
        self._user = user
        if not page.games:
            self._total = page.total
            self._set_status("This account has no public games")
            self._update_buttons()
            return
        self._render_page(1, page)

    def _search_failed(self, seq, error):
        if seq != self._request_seq:
            return
        self.busy = False
        self._set_status(str(error), error=True)

    def _load_page(self, page):
        if self._user is None:
            return
        self.busy = True
        self._set_status("Loading more games...")
        self._run(self._do_load_page, page, self._request_seq)

    def _page_loaded(self, seq, page, result):
        if seq != self._request_seq:
            return
        self.busy = False
        self._render_page(page, result)

    def _page_failed(self, seq, error):
        if seq != self._request_seq:
            return
        self.busy = False
        # Keep the rows already shown; Show more stays available as a retry.
        self._set_status(str(error), error=True)
        self._update_buttons()

    def _load_failed(self, error):
        self.busy = False
        self._set_status(str(error), error=True)

    def _load_done(self, sgf):
        self.busy = False
        fast = bool(self.load_popup.fast.active) if self.load_popup else False
        rewind = bool(self.load_popup.rewind.active) if self.load_popup else True
        if self.load_popup is not None:
            self.load_popup.update_config(False)
            self.katrain.save_config("general")
            if self.load_popup.popup is not None:
                self.load_popup.popup.dismiss()
        self.katrain.load_sgf_content(sgf, fast=fast, rewind=rewind)

    # -- helpers ----------------------------------------------------------

    def _render_page(self, page, result):
        for game in result.games:
            self.ids.game_list.add_widget(self._make_row(game))
        self._page = page
        self._shown += len(result.games)
        self._total = result.total
        self.has_more = result.has_more
        self._update_list_status()
        self._update_buttons()

    def _reset_results(self):
        self._user = None
        self._page = 0
        self._shown = 0
        self._total = None
        self.selected_game = None
        self.has_more = False
        self._clear_rows()

    def _update_list_status(self):
        if self._total is None:
            self._set_status("Showing {} games", self._shown)
        else:
            self._set_status("Showing {} of {} games", self._shown, self._total)

    @staticmethod
    def _player_label(name, rank):
        return f"{name}({rank})" if rank else name

    @classmethod
    def _matchup(cls, game):
        # The circles are drawn in the label colour, so fill/hollow must follow the
        # theme: with light text a filled circle (●) reads as a white stone and a
        # hollow one (○) as black; with dark text it is the other way round.
        # (U+26AB/U+26AA are not in KaTrain's CJK font, so these are used instead.)
        light_text = sum(Theme.TEXT_COLOR[:3]) / 3 > 0.5
        black_dot, white_dot = ("\u25cb", "\u25cf") if light_text else ("\u25cf", "\u25cb")
        black = (black_dot, game.black_name, game.black_rank)
        white = (white_dot, game.white_name, game.white_rank)
        # Always list the queried user first.
        first, second = (white, black) if game.user_color == "W" else (black, white)
        return (
            f"{first[0]} {cls._player_label(first[1], first[2])}"
            f" vs {cls._player_label(second[1], second[2])} {second[0]}"
        )

    @staticmethod
    def _row_color(game):
        if not game.user_color or not game.result or game.result == "Draw":
            return Theme.ONLINE_GAME_DRAW_COLOR
        return Theme.ONLINE_GAME_WIN_COLOR if game.result.startswith(game.user_color) else Theme.ONLINE_GAME_LOSS_COLOR

    def _make_row(self, game):
        matchup = self._matchup(game)
        row = RemoteGameRow(
            game=game,
            group="remote_game",
            date_text=(game.date or "")[:16],
            players_text=matchup,
            result_text=game.result or "",
            move_count=game.move_count,
            row_color=self._row_color(game),
        )
        row.bind(state=self._on_row_state, on_double_tap=self._on_row_double_tap)
        return row

    def _on_row_state(self, row, state):
        if state == "down":
            self.selected_game = row.game
        elif self.selected_game is row.game:
            self.selected_game = None

    def _on_row_double_tap(self, row, _touch):
        self.selected_game = row.game
        self.load_selected()

    def _clear_rows(self):
        self.ids.game_list.clear_widgets()

    def _save_username(self, query):
        general = self.katrain._config.setdefault("general", {})
        key = f"{self.source.key}_username"
        if general.get(key) != query:
            general[key] = query
            self.katrain.save_config("general")

    def _update_buttons(self, *_args):
        self.query_valid = self.query_is_valid(self.ids.query.text)
        self.ids.search_button.disabled = self.busy or not self.query_valid
        self.ids.load_button.disabled = self.busy or self.selected_game is None
        self.ids.show_more_button.disabled = self.busy or not self.has_more

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

    def _on_language_change(self, _lang):
        self._apply_status()

    def _run(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()
