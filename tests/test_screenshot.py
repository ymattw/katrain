"""Tests for clipboard screenshot Go board detection, classification, and SGF building."""

import pytest
from PIL import Image, ImageDraw
from pysgf import Move

from katrain.core import screenshot
from katrain.core.base_katrain import KaTrainBase
from katrain.core.game import BaseGame, IllegalMoveException
from katrain.core.screenshot import (
    board_box_to_grid,
    build_position_sgf,
    build_position_tree,
    classify_stones,
    detect_board,
    detect_board_in_box,
    detect_ko_capture,
    drag_square_corners,
    grid_to_board_box,
    read_clipboard_image,
)


def _render_synthetic_board(
    board_size=19,
    stones=None,
    cell=36,
    margin=40,
    top_chrome=0,
    bottom_chrome=0,
    wood_rgb=(228, 190, 125),
    line_rgb=(45, 30, 15),
    draw_hoshi=True,
    numbered_stone=None,
    marker_style="number",
):
    """Render a clean synthetic Go board screenshot (with optional UI chrome)."""
    stones = stones or {}
    board_w = (board_size - 1) * cell + 2 * margin
    board_h = board_w
    w = board_w + 40
    h = board_h + top_chrome + bottom_chrome
    bx = 20
    by = top_chrome

    im = Image.new("RGB", (w, h), (235, 240, 248))
    draw = ImageDraw.Draw(im)

    # Optional UI chrome (header & dark buttons) like mobile Go apps.
    if top_chrome:
        draw.rectangle([0, 0, w, top_chrome - 6], fill=(248, 250, 255))
        draw.rectangle([w // 3, 12, 2 * w // 3, top_chrome - 18], outline=(60, 80, 120), width=2)
    if bottom_chrome:
        btn_y = by + board_h + 24
        draw.rectangle([w // 4, btn_y, 3 * w // 4, min(h - 10, btn_y + 44)], fill=(25, 45, 80))

    # Wood board rectangle
    draw.rectangle([bx, by, bx + board_w, by + board_h], fill=wood_rgb, outline=(120, 90, 50), width=2)

    gx0 = bx + margin
    gy0 = by + margin
    gx1 = gx0 + (board_size - 1) * cell
    gy1 = gy0 + (board_size - 1) * cell

    for i in range(board_size):
        x = gx0 + i * cell
        y = gy0 + i * cell
        lw = 2 if i in (0, board_size - 1) else 1
        draw.line([(x, gy0), (x, gy1)], fill=line_rgb, width=lw)
        draw.line([(gx0, y), (gx1, y)], fill=line_rgb, width=lw)

    # Star points (hoshi)
    if draw_hoshi:
        hoshi_coords = {
            19: [(3, 3), (3, 9), (3, 15), (9, 3), (9, 9), (9, 15), (15, 3), (15, 9), (15, 15)],
            13: [(3, 3), (3, 9), (6, 6), (9, 3), (9, 9)],
            9: [(2, 2), (2, 6), (4, 4), (6, 2), (6, 6)],
        }.get(board_size, [])
        for c, r in hoshi_coords:
            cx, cy = gx0 + c * cell, gy0 + r * cell
            draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=line_rgb)

    sr = int(cell * 0.45)
    for (c, r), color in stones.items():
        cx, cy = gx0 + c * cell, gy0 + r * cell
        if color == "B":
            draw.ellipse([cx - sr, cy - sr, cx + sr, cy + sr], fill=(22, 22, 24), outline=(10, 10, 10))
        else:
            draw.ellipse([cx - sr, cy - sr, cx + sr, cy + sr], fill=(242, 242, 242), outline=(170, 170, 170))
        if numbered_stone == (c, r):
            mark_col = (240, 240, 240) if color == "B" else (25, 25, 25)
            if marker_style == "circle":
                rr = int(round(cell * 0.28))
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=mark_col, width=2)
            elif marker_style == "triangle":
                tr = int(round(cell * 0.25))
                draw.polygon([(cx, cy - tr), (cx - tr, cy + tr), (cx + tr, cy + tr)], outline=mark_col, width=2)
            else:
                draw.rectangle([cx - 5, cy - 4, cx + 5, cy + 4], fill=mark_col)

    return im, ((float(gx0), float(gy0)), (float(gx1), float(gy0)), (float(gx1), float(gy1)), (float(gx0), float(gy1)))


def test_read_clipboard_image_ignores_file_paths(monkeypatch):
    monkeypatch.setattr(screenshot.sys, "platform", "win32")
    monkeypatch.setattr(screenshot.ImageGrab, "grabclipboard", lambda: ["/Users/test/board.jpg"])
    assert read_clipboard_image() is None

    sample = Image.new("RGBA", (16, 16), (10, 20, 30, 255))
    monkeypatch.setattr(screenshot.ImageGrab, "grabclipboard", lambda: sample)
    out = read_clipboard_image()
    assert isinstance(out, Image.Image)
    assert out.mode == "RGB"
    assert out.size == (16, 16)


def test_read_clipboard_image_macos_fast_path(monkeypatch):
    monkeypatch.setattr(screenshot.sys, "platform", "darwin")
    monkeypatch.setattr(screenshot, "_read_macos_clipboard_image", lambda: None)
    assert read_clipboard_image() is None

    sample = Image.new("RGB", (32, 32), (10, 20, 30))
    monkeypatch.setattr(screenshot, "_read_macos_clipboard_image", lambda: sample)
    out = read_clipboard_image()
    assert out is sample


def test_detect_board_19x19_with_chrome_edge_stones_and_marker():
    stones = {
        (0, 0): "B",
        (18, 8): "W",  # right-edge 1st line
        (12, 18): "W",  # bottom-edge 1st line
        (3, 3): "B",
        (15, 3): "W",
        (3, 15): "W",
        (15, 15): "B",
        (6, 6): "B",  # move with number/marker in center
        (9, 9): "W",
        (10, 9): "B",
    }
    im, _expected_corners = _render_synthetic_board(
        board_size=19,
        stones=stones,
        cell=36,
        top_chrome=110,
        bottom_chrome=150,
        numbered_stone=(6, 6),
    )
    res = detect_board(im)
    assert res.board_size == 19
    assert res.stones == stones
    # Stone (6, 6) is Black with a center marker -> White to play despite 5B == 5W.
    assert res.last_move == (6, 6)
    assert res.next_player == "W"


def test_detect_board_13x13_and_9x9():
    stones_13 = {(2, 2): "B", (10, 2): "W", (6, 6): "B", (10, 10): "W", (3, 9): "B"}
    im13, _ = _render_synthetic_board(board_size=13, stones=stones_13, cell=44)
    res13 = detect_board(im13)
    assert res13.board_size == 13
    assert res13.stones == stones_13
    assert res13.last_move is None
    assert res13.next_player == "W"  # 3 black, 2 white -> W to play

    stones_9 = {(2, 2): "B", (6, 6): "W", (4, 4): "B"}
    im9, _ = _render_synthetic_board(board_size=9, stones=stones_9, cell=56)
    res9 = detect_board(im9)
    assert res9.board_size == 9
    assert res9.stones == stones_9
    assert res9.next_player == "W"


def test_classify_stones_neutral_grey_board_theme():
    stones = {(3, 3): "B", (9, 9): "W", (15, 15): "B"}
    im, corners = _render_synthetic_board(
        board_size=19,
        stones=stones,
        cell=34,
        wood_rgb=(150, 155, 152),
        line_rgb=(70, 72, 70),
    )
    detected, next_player, last_move = classify_stones(im, corners, 19)
    assert detected == stones
    assert last_move is None
    assert next_player == "W"


def test_drag_square_corners_preserves_square_and_clamps_to_image():
    corners = ((100.0, 100.0), (500.0, 100.0), (500.0, 500.0), (100.0, 500.0))
    # Drag BR (index 2) asymmetrically; TL (100, 100) stays anchored and box stays square.
    updated = drag_square_corners(corners, 2, (700.0, 550.0), img_w=1000, img_h=800)
    tl, tr, br, bl = updated
    assert tl == (100.0, 100.0)
    assert tr[1] == tl[1] and bl[0] == tl[0] and br[0] == tr[0] and br[1] == bl[1]
    assert abs((tr[0] - tl[0]) - (bl[1] - tl[1])) < 1e-6

    # Drag TL (index 0) past image origin; BR stays anchored and square stays in bounds.
    clamped = drag_square_corners(corners, 0, (-200.0, -50.0), img_w=1000, img_h=800)
    tl2, tr2, br2, bl2 = clamped
    assert br2 == (500.0, 500.0)
    assert tl2[0] >= 0.0 and tl2[1] >= 0.0
    assert abs((tr2[0] - tl2[0]) - (bl2[1] - tl2[1])) < 1e-6


def test_grid_box_roundtrip_and_redetect_in_box():
    stones = {(0, 0): "B", (18, 8): "W", (12, 18): "W", (6, 6): "B", (9, 9): "W"}
    im, grid_corners = _render_synthetic_board(board_size=19, stones=stones, cell=36, top_chrome=80, bottom_chrome=80)
    box = grid_to_board_box(grid_corners, 19, im.width, im.height)
    # Expanded box encloses the outer grid lines by ~0.5 cell on every side.
    assert box[0][0] < grid_corners[0][0] and box[1][0] > grid_corners[1][0]
    assert box[0][1] < grid_corners[0][1] and box[2][1] > grid_corners[2][1]

    back = board_box_to_grid(box, 19)
    for (x1, y1), (x2, y2) in zip(grid_corners, back):
        assert abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6

    # Re-detecting inside a slightly loose dragged box snaps cleanly to the board.
    loose_box = (
        (box[0][0] - 10, box[0][1] - 10),
        (box[1][0] + 10, box[1][1] - 10),
        (box[2][0] + 10, box[2][1] + 10),
        (box[3][0] - 10, box[3][1] + 10),
    )
    redetected = detect_board_in_box(im, loose_box)
    assert redetected.board_size == 19
    assert redetected.stones == stones


def test_detect_board_ignores_periodic_sidebar_chrome():
    stones = {(3, 3): "B", (15, 3): "W", (3, 15): "W", (15, 15): "B", (9, 9): "B"}
    base, _ = _render_synthetic_board(board_size=19, stones=stones, cell=32, top_chrome=60, bottom_chrome=60)
    # Pad with wide left/right sidebars containing periodic grid-like table lines.
    side_w = 240
    wide = Image.new("RGB", (base.width + 2 * side_w, base.height), (235, 238, 244))
    wide.paste(base, (side_w, 0))
    draw = ImageDraw.Draw(wide)
    for x in range(16, side_w - 16, 20):
        draw.line([(x, 20), (x, wide.height - 20)], fill=(180, 185, 195), width=1)
        rx = base.width + side_w + x
        draw.line([(rx, 20), (rx, wide.height - 20)], fill=(180, 185, 195), width=1)
    res = detect_board(wide)
    assert res.board_size == 19
    assert res.stones == stones


def test_detect_board_full_window_with_margins_and_thick_border_avoids_harmonic():
    """Full-window screenshot with wide margins, dark frame, and bottom controls must lock onto 19x19."""
    stones = {
        (3, 3): "B",
        (15, 3): "W",
        (3, 15): "W",
        (15, 15): "B",
        (8, 16): "W",  # last move marker -> Black to play
        (9, 9): "B",
    }
    board_im, _ = _render_synthetic_board(
        board_size=19,
        stones=stones,
        cell=26,
        margin=20,
        numbered_stone=(8, 16),
    )
    # Place the board inside a full-window screenshot with wide asymmetric margins,
    # black outer framing, top header bar, and bottom buttons/slider bar.
    win_w = board_im.width + 360
    win_h = board_im.height + 420
    full = Image.new("RGB", (win_w, win_h), (0, 0, 0))
    draw = ImageDraw.Draw(full)
    draw.rectangle([24, 16, win_w - 24, win_h - 16], fill=(242, 246, 252))
    # Header divider and bottom button / scrubber bars
    draw.line([(24, 140), (win_w - 24, 140)], fill=(120, 125, 135), width=2)
    bx = 180
    by = 160
    full.paste(board_im, (bx, by))
    btn_top = by + board_im.height + 50
    draw.rectangle([bx + 80, btn_top, bx + board_im.width - 80, btn_top + 48], fill=(30, 50, 88))
    draw.rectangle([bx + 80, btn_top + 64, bx + board_im.width - 80, btn_top + 112], fill=(30, 50, 88))
    draw.line([(bx, win_h - 60), (bx + board_im.width, win_h - 60)], fill=(40, 40, 45), width=3)

    res = detect_board(full)
    assert res.board_size == 19
    assert res.confidence >= 0.35
    assert res.stones == stones
    assert res.last_move == (8, 16)
    assert res.next_player == "B"


@pytest.mark.parametrize("marker_style", ["circle", "triangle", "number"])
@pytest.mark.parametrize("last_color", ["B", "W"])
def test_detect_last_move_markers_drive_next_player(marker_style, last_color):
    """Inner circle, triangle, and number markers determine next_player even when stone counts disagree."""
    # Deliberately unbalanced stone counts (5 B, 3 W) so stone parity would guess "W",
    # verifying that when last_color == "W" the marker overrides stone parity to "B".
    stones = {
        (3, 3): "B",
        (15, 3): "B",
        (3, 15): "B",
        (15, 15): "B",
        (9, 9): "B",
        (6, 6): "W",
        (12, 6): "W",
        (12, 12): "W",
    }
    target = (9, 9) if last_color == "B" else (6, 6)
    im, _ = _render_synthetic_board(
        board_size=19,
        stones=stones,
        cell=36,
        numbered_stone=target,
        marker_style=marker_style,
    )
    res = detect_board(im)
    assert res.stones == stones
    assert res.last_move == target
    assert res.next_player == ("W" if last_color == "B" else "B")


def test_ko_capture_detected_and_enforced_in_base_game():
    """Active ko capture marked by an inner circle produces a 2-node SGF that enforces ko in KaTrain."""
    # Black just captured a White stone at (5, 4) by playing (4, 4).
    stones = {
        (4, 4): "B",  # last move (in atari, sole liberty is (5, 4))
        (5, 3): "B",
        (5, 5): "B",
        (6, 4): "B",
        (3, 4): "W",
        (4, 3): "W",
        (4, 5): "W",
    }
    im, _ = _render_synthetic_board(
        board_size=9,
        stones=stones,
        cell=48,
        numbered_stone=(4, 4),
        marker_style="circle",
    )
    res = detect_board(im)
    assert res.board_size == 9
    assert res.stones == stones
    assert res.last_move == (4, 4)
    assert res.next_player == "W"
    assert detect_ko_capture(res.stones, res.last_move, res.board_size) == (5, 4)

    root = build_position_tree(
        res.board_size,
        res.stones,
        next_player=res.next_player,
        komi=7.5,
        rules="chinese",
        last_move=res.last_move,
    )
    assert len(root.children) == 1
    assert "AE[" not in root.sgf()

    katrain = KaTrainBase(force_package_config=True, debug_level=0)
    game = BaseGame(katrain, move_tree=root)
    game.set_current_node(root.children[0])

    # Immediate White recapture at (5, 4) -> SGF row = 9 - 1 - 4 = 4 must be rejected as Ko.
    with pytest.raises(IllegalMoveException, match="Ko"):
        game.play(Move((5, 4), player="W"))

    # Playing elsewhere (a ko threat) is legal.
    game.play(Move((0, 0), player="W"))

    # If the user manually overrides next_player to match last_move's color, keep a 1-node root.
    overridden = build_position_tree(
        res.board_size,
        res.stones,
        next_player="B",
        komi=7.5,
        rules="chinese",
        last_move=res.last_move,
    )
    assert overridden.children == []
    assert overridden.get_property("PL") == "B"


def test_build_position_tree_has_no_clear_placements_and_maps_coordinates():
    stones = {(0, 0): "B", (18, 18): "W", (3, 15): "B"}
    root = build_position_tree(19, stones, next_player="W", komi=7.5, rules="chinese")
    assert root.clear_placements == []
    assert root.get_property("SZ") == 19
    assert root.get_property("KM") == 7.5
    assert root.get_property("RU") == "chinese"
    assert root.get_property("PL") == "W"

    sgf = build_position_sgf(19, stones, next_player="W", komi=7.5, rules="chinese")
    assert "AB[aa][dp]" in sgf
    assert "AW[ss]" in sgf
    assert "PL[W]" in sgf
    assert "AE[" not in sgf
