"""Interactive verification popup for importing a Go board position from a clipboard screenshot."""

from __future__ import annotations

from kivy.app import App
from kivy.clock import Clock
from kivy.graphics import Color, Line, Rectangle
from kivy.graphics.texture import Texture
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import NumericProperty, ObjectProperty, StringProperty
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.image import Image as KivyImage
from kivy.uix.popup import Popup
from kivy.uix.widget import Widget
from PIL import Image

from katrain.core.constants import OUTPUT_INFO
from katrain.core.screenshot import (
    build_position_tree,
    detect_board,
    detect_board_in_box,
    drag_square_corners,
    grid_to_board_box,
)
from katrain.gui.theme import Theme

_DEFAULT_RULES = "chinese"
_DEFAULT_KOMI = 7.5
_CORNER_GRAB_RADIUS = dp(24)
_CORNER_SQUARE_HALF = dp(6)
_KV_LOADED = False


class StoneToggleButton(ButtonBehavior, KivyImage):
    """Clickable Black/White stone indicator for toggling whose turn it is."""

    player = StringProperty("B")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fit_mode = "contain"
        self.source = Theme.STONE_TEXTURE.get(self.player, Theme.STONE_TEXTURE["B"])

    def on_player(self, _instance, value):
        self.source = Theme.STONE_TEXTURE.get(value, Theme.STONE_TEXTURE["B"])


class ScreenshotBoardCanvas(Widget):
    """Displays a screenshot with a faded outer scrim and square-locked 4-corner handles."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._image = None
        self._texture = None
        self._corners = None  # [(x, y), ...] outer board box in original image coords (TL, TR, BR, BL)
        self._img_rect = (0.0, 0.0, 1.0, 1.0)
        self._dragging_corner = None
        self._on_drag_finished = None
        self._redraw_trigger = Clock.create_trigger(self._redraw, 0)
        self.bind(pos=self._redraw_trigger, size=self._redraw_trigger)

    def set_data(self, image, corners, on_drag_finished=None):
        self._image = image
        self._corners = [tuple(pt) for pt in corners]
        if on_drag_finished is not None:
            self._on_drag_finished = on_drag_finished

        rgb = image if image.mode == "RGB" else image.convert("RGB")
        # Cap OpenGL preview texture size for instant modal display on Retina screenshots.
        max_tex = 1200
        if max(rgb.size) > max_tex:
            scale = max_tex / max(rgb.size)
            rgb = rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))), Image.Resampling.BILINEAR)
        tex = Texture.create(size=rgb.size, colorfmt="rgb")
        tex.blit_buffer(rgb.tobytes(), colorfmt="rgb", bufferfmt="ubyte")
        tex.flip_vertical()
        self._texture = tex
        self._redraw_trigger()

    def update_overlay(self, corners):
        self._corners = [tuple(pt) for pt in corners]
        self._redraw_trigger()

    def _compute_img_rect(self):
        if self._image is None or self.width <= 1 or self.height <= 1:
            return (self.x, self.y, max(1.0, self.width), max(1.0, self.height))
        iw, ih = self._image.size
        pad = dp(12)
        avail_w = max(1.0, self.width - 2 * pad)
        avail_h = max(1.0, self.height - 2 * pad)
        scale = min(avail_w / iw, avail_h / ih)
        dw = iw * scale
        dh = ih * scale
        ox = self.x + (self.width - dw) * 0.5
        oy = self.y + (self.height - dh) * 0.5
        return (ox, oy, dw, dh)

    def _img_to_canvas(self, ix, iy):
        ox, oy, dw, dh = self._img_rect
        iw, ih = self._image.size
        cx = ox + (ix / iw) * dw
        cy = oy + (1.0 - iy / ih) * dh
        return cx, cy

    def _canvas_to_img(self, cx, cy):
        ox, oy, dw, dh = self._img_rect
        iw, ih = self._image.size
        ix = ((cx - ox) / dw) * iw
        iy = (1.0 - (cy - oy) / dh) * ih
        return max(0.0, min(float(iw - 1), ix)), max(0.0, min(float(ih - 1), iy))

    def _redraw(self, *_args):
        self.canvas.clear()
        if self._texture is None or self._image is None or not self._corners:
            return

        self._img_rect = self._compute_img_rect()
        ox, oy, dw, dh = self._img_rect
        canvas_corners = [self._img_to_canvas(*pt) for pt in self._corners]

        with self.canvas:
            # 1. Screenshot image
            Color(1, 1, 1, 1)
            Rectangle(texture=self._texture, pos=(ox, oy), size=(dw, dh))

            # 2. Dim the screenshot area outside the detected board square
            ix0, iy0, ix1, iy1 = ox, oy, ox + dw, oy + dh
            bx0 = max(ix0, min(ix1, min(pt[0] for pt in canvas_corners)))
            bx1 = max(ix0, min(ix1, max(pt[0] for pt in canvas_corners)))
            by0 = max(iy0, min(iy1, min(pt[1] for pt in canvas_corners)))
            by1 = max(iy0, min(iy1, max(pt[1] for pt in canvas_corners)))
            Color(0, 0, 0, 0.5)
            if iy1 > by1:  # top
                Rectangle(pos=(ix0, by1), size=(ix1 - ix0, iy1 - by1))
            if by0 > iy0:  # bottom
                Rectangle(pos=(ix0, iy0), size=(ix1 - ix0, by0 - iy0))
            if bx0 > ix0 and by1 > by0:  # left
                Rectangle(pos=(ix0, by0), size=(bx0 - ix0, by1 - by0))
            if ix1 > bx1 and by1 > by0:  # right
                Rectangle(pos=(bx1, by0), size=(ix1 - bx1, by1 - by0))

            # 3. Outer square border connecting the 4 corners
            Color(0.15, 0.9, 0.35, 0.85)
            pts = [coord for pt in canvas_corners for coord in pt]
            Line(points=pts, close=True, width=1.4)

            # 4. Small square draggable corner handles
            for idx, (hx, hy) in enumerate(canvas_corners):
                is_active = idx == self._dragging_corner
                half = _CORNER_SQUARE_HALF * (1.25 if is_active else 1.0)
                Color(0.05, 0.05, 0.05, 0.95)
                Rectangle(pos=(hx - half - 1.5, hy - half - 1.5), size=(2 * (half + 1.5), 2 * (half + 1.5)))
                Color(1.0, 0.85, 0.15, 0.95) if is_active else Color(0.2, 0.95, 0.4, 0.95)
                Rectangle(pos=(hx - half, hy - half), size=(2 * half, 2 * half))

    def on_touch_down(self, touch):
        if self._image is None or not self._corners or not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)
        best_idx = None
        best_dist = _CORNER_GRAB_RADIUS
        for idx, pt in enumerate(self._corners):
            hx, hy = self._img_to_canvas(*pt)
            dist = ((touch.x - hx) ** 2 + (touch.y - hy) ** 2) ** 0.5
            if dist <= best_dist:
                best_dist = dist
                best_idx = idx
        if best_idx is not None:
            self._dragging_corner = best_idx
            touch.grab(self)
            self._redraw_trigger()
            return True
        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        if touch.grab_current is self and self._dragging_corner is not None:
            iw, ih = self._image.size
            target = self._canvas_to_img(touch.x, touch.y)
            self._corners = list(drag_square_corners(tuple(self._corners), self._dragging_corner, target, iw, ih))
            self._redraw_trigger()
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            self._dragging_corner = None
            self._redraw_trigger()
            if self._on_drag_finished:
                self._on_drag_finished(tuple(self._corners))
            return True
        return super().on_touch_up(touch)


class ScreenshotImportPopup(BoxLayout):
    """Modal content for verifying and loading a Go board position from a screenshot."""

    katrain = ObjectProperty(None)
    popup = ObjectProperty(None, allownone=True)
    board_size = NumericProperty(19)
    next_player = StringProperty("B")

    def __init__(self, katrain, image, **kwargs):
        _ensure_kv_loaded()
        super().__init__(**kwargs)
        self.katrain = katrain
        self._image = image
        self._box_corners = None
        self._stones = {}
        self._last_move = None
        self._turn_overridden = False
        self.ids.komi_input.text = str(_DEFAULT_KOMI)
        self._run_detection()

    def _run_detection(self):
        res = detect_board(self._image)
        iw, ih = self._image.size
        self.board_size = res.board_size
        self._box_corners = res.box_corners or grid_to_board_box(res.corners, res.board_size, iw, ih)
        self._stones = res.stones
        self._last_move = res.last_move
        self.next_player = res.next_player
        self._turn_overridden = False
        self.ids.board_canvas.set_data(
            self._image,
            self._box_corners,
            on_drag_finished=self._on_box_drag_finished,
        )

    def _on_box_drag_finished(self, box_corners):
        res = detect_board_in_box(self._image, box_corners, fallback_board_size=int(self.board_size))
        self.board_size = res.board_size
        self._box_corners = res.box_corners or box_corners
        self._stones = res.stones
        self._last_move = res.last_move
        if not self._turn_overridden:
            self.next_player = res.next_player
        self.ids.board_canvas.update_overlay(self._box_corners)

    def toggle_next_player(self):
        self._turn_overridden = True
        self.next_player = "W" if self.next_player == "B" else "B"

    def _parse_komi(self) -> float:
        try:
            return float(self.ids.komi_input.text.strip())
        except (ValueError, AttributeError):
            return _DEFAULT_KOMI

    def load_position(self):
        root = build_position_tree(
            board_size=int(self.board_size),
            stones=self._stones,
            next_player=self.next_player,
            komi=self._parse_komi(),
            rules=_DEFAULT_RULES,
            last_move=self._last_move,
        )
        if self.popup is not None:
            self.popup.dismiss()
        self.katrain._do_new_game(move_tree=root, analyze_fast=False, sgf_filename="")
        if root.children and self.katrain.game is not None:
            self.katrain.game.set_current_node(root.children[0])
            self.katrain.update_state()
        self.katrain.log(
            f"Imported {self.board_size}x{self.board_size} board position from clipboard screenshot "
            f"({len(self._stones)} stones, next={self.next_player}, rules={_DEFAULT_RULES}).",
            OUTPUT_INFO,
        )

    def cancel(self):
        if self.popup is not None:
            self.popup.dismiss()

    def on_submit(self):
        self.load_position()


class ScreenshotImportModal(Popup):
    """Popup window hosting :class:`ScreenshotImportPopup`."""

    def __init__(self, katrain, image, **kwargs):
        _ensure_kv_loaded()
        content = ScreenshotImportPopup(katrain, image)
        app = App.get_running_app()
        w = min(app.gui.width, dp(1100)) if app and getattr(app, "gui", None) else dp(1100)
        h = min(app.gui.height, dp(780)) if app and getattr(app, "gui", None) else dp(780)
        super().__init__(content=content, size=(w, h), **kwargs)
        content.popup = self
        if app and getattr(app, "gui", None):
            self.bind(on_dismiss=Clock.schedule_once(lambda _dt: app.gui.update_state(), 0.2))


_SCREENSHOT_KV = """
#:import Theme katrain.gui.theme.Theme
#:import i18n katrain.core.lang.i18n
#:import l10n katrain.core.l10n.l10n
#:import kivy_version kivy.__version__
#:set CP_SPACING Theme.CP_SPACING
#:set CP_PADDING Theme.CP_PADDING

<ScreenshotImportModal>:
    background_color: Theme.BACKGROUND_COLOR
    background: self.background if kivy_version < '2.0' else ''
    title: l10n._("Import board from screenshot")
    title_size: sp(Theme.DESC_FONT_SIZE)
    title_font: i18n.font_name
    size_hint: None, None

<ScreenshotImportPopup>:
    orientation: 'horizontal'
    padding: CP_PADDING
    spacing: CP_SPACING
    ScreenshotBoardCanvas:
        id: board_canvas
        size_hint_x: 0.72
    BoxLayout:
        orientation: 'vertical'
        size_hint_x: 0.28
        padding: [0, dp(8), 0, 0]
        spacing: CP_SPACING
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            spacing: dp(12)
            BoxLayout:
                size_hint_y: None
                height: dp(36)
                spacing: dp(6)
                DescriptionLabel:
                    id: next_label
                    text: l10n._("Next to play")
                    size_hint_x: 0.52
                    halign: 'left'
                AnchorLayout:
                    size_hint_x: 0.48
                    anchor_x: 'left'
                    anchor_y: 'center'
                    StoneToggleButton:
                        id: turn_stone
                        player: root.next_player
                        size_hint: None, None
                        size: dp(32), dp(32)
                        on_release: root.toggle_next_player()
            BoxLayout:
                size_hint_y: None
                height: dp(36)
                spacing: dp(6)
                DescriptionLabel:
                    id: komi_label
                    text: l10n._("Komi")
                    size_hint_x: 0.52
                    halign: 'left'
                TextInput:
                    id: komi_input
                    multiline: False
                    size_hint_x: 0.48
                    font_name: i18n.font_name
                    padding: [dp(6), max(0, (self.height - self.line_height) / 2)]
        Widget:
            size_hint_y: 1
        BoxLayout:
            size_hint_y: None
            height: dp(40)
            spacing: CP_SPACING
            AutoSizedRoundedRectangleButton:
                text: l10n._("Cancel")
                size_hint_y: 1
                padding_x: 15
                on_release: root.cancel()
            Widget:
                size_hint_x: 1
            AutoSizedRoundedRectangleButton:
                text: l10n._("Load")
                size_hint_y: 1
                padding_x: 15
                on_release: root.load_position()
"""


def _ensure_kv_loaded():
    global _KV_LOADED
    if not _KV_LOADED:
        Builder.load_string(_SCREENSHOT_KV)
        _KV_LOADED = True
