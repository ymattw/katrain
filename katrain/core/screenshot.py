"""Clipboard screenshot capture, Go board detection, stone classification, and SGF building."""

from __future__ import annotations

import ctypes
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass
from io import BytesIO
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageGrab
from pysgf import Move

from katrain.core.game_node import GameNode

Point = Tuple[float, float]
Corners = Tuple[Point, Point, Point, Point]  # (TL, TR, BR, BL) outer intersection centers

SUPPORTED_BOARD_SIZES = (19, 13, 9)
_WORK_MAX_DIM = 480
_WARP_CELL_PX = 24
_RIDGE_LUT = [0 if i <= 6 else (40 if i > 40 else i) for i in range(256)]
_SYM_MASK_LUT = [255 if i < 32 else 0 for i in range(256)]

_MACOS_PASTEBOARD_CTX = None


@dataclass
class BoardDetectionResult:
    board_size: int
    corners: Corners  # (TL, TR, BR, BL) outer grid intersection centers in original image coords
    stones: Dict[Tuple[int, int], str]  # (col, row) with row=0 at top -> "B" or "W"
    next_player: str  # "B" or "W"
    confidence: float
    last_move: Optional[Tuple[int, int]] = None
    box_corners: Optional[Corners] = None  # outer board box (+0.5 cell margin enclosing border stones)


def _get_macos_pasteboard_ctx():
    """Lazily initialize typed Objective-C bindings for ``NSPasteboard`` on macOS."""
    global _MACOS_PASTEBOARD_CTX
    if _MACOS_PASTEBOARD_CTX is not None:
        return _MACOS_PASTEBOARD_CTX

    objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")

    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p

    # Bind distinct typed CFUNCTYPE wrappers (safe on Apple Silicon arm64 ABI).
    msg_addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    send_0 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(msg_addr)
    send_1 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(msg_addr)
    send_str = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(msg_addr)
    send_size = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)(msg_addr)
    send_bool_1 = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(msg_addr)

    cls_nsstring = objc.objc_getClass(b"NSString")
    cls_pb = objc.objc_getClass(b"NSPasteboard")
    sel_utf8 = objc.sel_registerName(b"stringWithUTF8String:")

    def nsstr(value: str):
        return send_str(cls_nsstring, sel_utf8, value.encode("utf-8"))

    _MACOS_PASTEBOARD_CTX = {
        "cls_pb": cls_pb,
        "send_0": send_0,
        "send_1": send_1,
        "send_size": send_size,
        "send_bool_1": send_bool_1,
        "sel_general": objc.sel_registerName(b"generalPasteboard"),
        "sel_types": objc.sel_registerName(b"types"),
        "sel_contains": objc.sel_registerName(b"containsObject:"),
        "sel_data_for_type": objc.sel_registerName(b"dataForType:"),
        "sel_length": objc.sel_registerName(b"length"),
        "sel_bytes": objc.sel_registerName(b"bytes"),
        "uti_file_url": nsstr("public.file-url"),
        "uti_filenames": nsstr("NSFilenamesPboardType"),
        "uti_images": (nsstr("public.png"), nsstr("public.tiff")),
    }
    return _MACOS_PASTEBOARD_CTX


def _read_macos_clipboard_image() -> Optional[Image.Image]:
    """Fast native ``NSPasteboard`` image read on macOS (~5-50 ms vs 500-3000 ms for osascript)."""
    ctx = _get_macos_pasteboard_ctx()
    pb = ctx["send_0"](ctx["cls_pb"], ctx["sel_general"])
    if not pb:
        return None
    types = ctx["send_0"](pb, ctx["sel_types"])
    if not types:
        return None

    # Ignore clipboard file paths (e.g. Finder Cmd+C on an image or .sgf file).
    if ctx["send_bool_1"](types, ctx["sel_contains"], ctx["uti_file_url"]) or ctx["send_bool_1"](
        types, ctx["sel_contains"], ctx["uti_filenames"]
    ):
        return None

    for uti in ctx["uti_images"]:
        if not ctx["send_bool_1"](types, ctx["sel_contains"], uti):
            continue
        nsdata = ctx["send_1"](pb, ctx["sel_data_for_type"], uti)
        if not nsdata:
            continue
        length = ctx["send_size"](nsdata, ctx["sel_length"])
        ptr = ctx["send_0"](nsdata, ctx["sel_bytes"])
        if ptr and length:
            raw = ctypes.string_at(ptr, length)
            return Image.open(BytesIO(raw)).convert("RGB")
    return None


def read_clipboard_image() -> Optional[Image.Image]:
    """Read raw image data from the system clipboard, ignoring file paths.

    Returns an RGB ``PIL.Image.Image`` if the clipboard holds image data, or
    ``None`` when the clipboard is empty, holds text, or holds file paths
    (such as Finder-copied files).
    """
    if sys.platform == "darwin":
        try:
            return _read_macos_clipboard_image()
        except Exception:
            pass

    try:
        grabbed = ImageGrab.grabclipboard()
    except Exception:
        grabbed = None

    if isinstance(grabbed, Image.Image):
        return grabbed.convert("RGB")
    if isinstance(grabbed, list):
        # Explicitly ignore clipboard file paths (e.g. Explorer/Finder Cmd+C).
        return None

    # Linux Wayland / X11 fallback when ImageGrab is unavailable.
    for cmd in (
        ["wl-paste", "--no-newline", "--type", "image/png"],
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
    ):
        if not shutil.which(cmd[0]):
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=2, check=False)
            if proc.returncode == 0 and proc.stdout:
                return Image.open(BytesIO(proc.stdout)).convert("RGB")
        except Exception:
            continue
    return None


def _ridge_profiles(
    gray: Image.Image,
    x0: int = 0,
    x1: Optional[int] = None,
    y0: int = 0,
    y1: Optional[int] = None,
    bright: bool = False,
) -> Tuple[List[float], List[float]]:
    """Compute 1D vertical-line (X) and horizontal-line (Y) ridge profiles in C via Pillow.

    Using a thin-ridge detector (center darker than BOTH neighbors at +/-1 or +/-2
    with similar luminance on both sides) highlights Go grid lines while strongly
    suppressing step edges from UI buttons, headers, and outer wooden board frames.
    """
    w, h = gray.size
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = w if x1 is None else min(w, x1)
    y1 = h if y1 is None else min(h, y1)

    vx = [0.0] * w
    hy = [0.0] * h
    rw = x1 - x0
    rh = y1 - y0
    if rw < 6 or rh < 6:
        return vx, hy

    roi = gray if (x0 == 0 and y0 == 0 and x1 == w and y1 == h) else gray.crop((x0, y0, x1, y1))

    # Vertical lines: compare each pixel against left/right neighbors at offset 1 and 2.
    inner_w = rw - 4
    c_v = roi.crop((2, 0, rw - 2, rh))
    l1 = roi.crop((1, 0, rw - 3, rh))
    r1 = roi.crop((3, 0, rw - 1, rh))
    l2 = roi.crop((0, 0, rw - 4, rh))
    r2 = roi.crop((4, 0, rw, rh))

    if not bright:
        d1 = ImageChops.subtract(ImageChops.darker(l1, r1), c_v)
        d2 = ImageChops.subtract(ImageChops.darker(l2, r2), c_v)
    else:
        d1 = ImageChops.subtract(c_v, ImageChops.lighter(l1, r1))
        d2 = ImageChops.subtract(c_v, ImageChops.lighter(l2, r2))

    m1 = ImageChops.difference(l1, r1).point(_SYM_MASK_LUT)
    m2 = ImageChops.difference(l2, r2).point(_SYM_MASK_LUT)
    v_map = ImageChops.lighter(ImageChops.darker(d1, m1), ImageChops.darker(d2, m2)).point(_RIDGE_LUT)
    v_proj = v_map.resize((inner_w, 1), Image.Resampling.BOX).tobytes()
    for i, val in enumerate(v_proj):
        vx[x0 + 2 + i] = float(val)

    # Horizontal lines: compare each pixel against top/bottom neighbors at offset 1 and 2.
    inner_h = rh - 4
    c_h = roi.crop((0, 2, rw, rh - 2))
    u1 = roi.crop((0, 1, rw, rh - 3))
    b1 = roi.crop((0, 3, rw, rh - 1))
    u2 = roi.crop((0, 0, rw, rh - 4))
    b2 = roi.crop((0, 4, rw, rh))

    if not bright:
        dh1 = ImageChops.subtract(ImageChops.darker(u1, b1), c_h)
        dh2 = ImageChops.subtract(ImageChops.darker(u2, b2), c_h)
    else:
        dh1 = ImageChops.subtract(c_h, ImageChops.lighter(u1, b1))
        dh2 = ImageChops.subtract(c_h, ImageChops.lighter(u2, b2))

    mh1 = ImageChops.difference(u1, b1).point(_SYM_MASK_LUT)
    mh2 = ImageChops.difference(u2, b2).point(_SYM_MASK_LUT)
    h_map = ImageChops.lighter(ImageChops.darker(dh1, mh1), ImageChops.darker(dh2, mh2)).point(_RIDGE_LUT)
    h_proj = h_map.resize((1, inner_h), Image.Resampling.BOX).tobytes()
    for i, val in enumerate(h_proj):
        hy[y0 + 2 + i] = float(val)

    return vx, hy


def _smooth_profile(profile: Sequence[float]) -> List[float]:
    n = len(profile)
    if n < 3:
        return list(profile)
    out = [0.0] * n
    out[0] = 0.67 * profile[0] + 0.33 * profile[1]
    for i in range(1, n - 1):
        out[i] = 0.25 * profile[i - 1] + 0.5 * profile[i] + 0.25 * profile[i + 1]
    out[n - 1] = 0.33 * profile[n - 2] + 0.67 * profile[n - 1]
    return out


def _dilate_max(profile: Sequence[float], radius: int) -> List[float]:
    n = len(profile)
    out = [0.0] * n
    for i in range(n):
        lo = 0 if i < radius else i - radius
        hi = n - 1 if i + radius >= n else i + radius
        best = profile[lo]
        for j in range(lo + 1, hi + 1):
            if profile[j] > best:
                best = profile[j]
        out[i] = best
    return out


def _autocorr_steps(profile: Sequence[float], min_step: float, max_step: float, top_k: int = 5) -> List[float]:
    """Return top candidate periodic spacings (in pixels) via 1D autocorrelation."""
    n = len(profile)
    lo = max(4, int(math.floor(min_step)))
    hi = min(n // 2, int(math.ceil(max_step)))
    if hi <= lo:
        return []

    mean = sum(profile) / max(1, n)
    centered = [max(0.0, v - mean * 0.5) for v in profile]
    scores: List[Tuple[float, int]] = []
    for lag in range(lo, hi + 1):
        acc = 0.0
        limit = n - lag
        for i in range(limit):
            acc += centered[i] * centered[i + lag]
        scores.append((acc / max(1, limit), lag))

    peaks: List[Tuple[float, float]] = []
    for idx, (val, lag) in enumerate(scores):
        prev_v = scores[idx - 1][0] if idx > 0 else -1.0
        next_v = scores[idx + 1][0] if idx + 1 < len(scores) else -1.0
        if val >= prev_v and val >= next_v and val > 0:
            if 0 < idx < len(scores) - 1:
                denom = prev_v - 2 * val + next_v
                delta = 0.5 * (prev_v - next_v) / denom if abs(denom) > 1e-9 else 0.0
                delta = max(-0.5, min(0.5, delta))
            else:
                delta = 0.0
            peaks.append((val, lag + delta))

    peaks.sort(key=lambda item: item[0], reverse=True)
    return [step for _score, step in peaks[:top_k]]


def _comb_score(
    p1: Sequence[float],
    n: int,
    start: float,
    step: float,
    board_size: int,
    size_weight: float,
) -> Tuple[float, int]:
    inside = 0.0
    hits = 0
    pos = start
    for _ in range(board_size):
        idx = int(pos + 0.5)
        if 0 <= idx < n:
            val = p1[idx]
            inside += val
            if val >= 0.18:
                hits += 1
        pos += step
    mean_in = inside / board_size

    # Penalize missing outer border lines so combs cannot dangle into empty margins.
    i0 = int(start + 0.5)
    ilast = int(start + (board_size - 1) * step + 0.5)
    end0 = p1[i0] if 0 <= i0 < n else 0.0
    end1 = p1[ilast] if 0 <= ilast < n else 0.0
    end_pen = (0.12 if end0 < 0.12 else 0.0) + (0.12 if end1 < 0.12 else 0.0)

    # Penalize between-line ridges so 2x/3x step harmonics (e.g. 13x13 skipping every
    # other line of a 19x19 board) and uniform noise collapse toward zero.
    mid_pen = 0.0
    if step >= 7.0:
        m2 = 0.0
        pos = start + 0.5 * step
        for _ in range(board_size - 1):
            idx = int(pos + 0.5)
            if 0 <= idx < n:
                m2 += p1[idx]
            pos += step
        mid_pen = m2 / (board_size - 1)
    if step >= 12.0:
        m3 = 0.0
        s1 = step / 3.0
        s2 = 2.0 * s1
        pos = start
        for _ in range(board_size - 1):
            ia = int(pos + s1 + 0.5)
            ib = int(pos + s2 + 0.5)
            if 0 <= ia < n:
                m3 += p1[ia]
            if 0 <= ib < n:
                m3 += p1[ib]
            pos += step
        m3 /= 2 * (board_size - 1)
        if m3 > mid_pen:
            mid_pen = m3

    eff_in = mean_in - 0.85 * mid_pen
    if eff_in <= 0.0:
        return 0.0, hits

    im1 = int(start - step + 0.5)
    im2 = int(start - 2.0 * step + 0.5)
    ip1 = int(start + board_size * step + 0.5)
    ip2 = int(start + (board_size + 1) * step + 0.5)
    p_m1 = p1[im1] if 0 <= im1 < n else 0.0
    p_m2 = p1[im2] if 0 <= im2 < n else 0.0
    p_p1 = p1[ip1] if 0 <= ip1 < n else 0.0
    p_p2 = p1[ip2] if 0 <= ip2 < n else 0.0

    continuation = (p_m1 if p_m1 < p_m2 else p_m2) + (p_p1 if p_p1 < p_p2 else p_p2)
    edge_spill = 0.18 * (p_m1 + p_p1)
    score = (eff_in * eff_in) * size_weight - 0.65 * continuation - edge_spill - end_pen
    return score, hits


def _best_comb_1d(
    profile: Sequence[float],
    board_size: int,
    candidate_steps: Sequence[float],
) -> Tuple[float, float, float]:
    """Find ``(score, start, step)`` maximizing regular grid-line alignment for ``board_size``."""
    n = len(profile)
    sm = _smooth_profile(profile)
    max_val = max(sm) if sm else 0.0
    if max_val < 1.5:
        return 0.0, 0.0, 0.0

    # Normalize against a robust interior-line peak so thick outer board borders or
    # strong UI divider bars do not crush the interior grid lines.
    peaks = [sm[i] for i in range(1, n - 1) if sm[i] > 0 and sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1]]
    peaks.sort(reverse=True)
    ref_val = peaks[min(len(peaks) - 1, max(2, board_size // 3))] if peaks else max_val
    ref_val = max(ref_val, max_val * 0.25)

    norm = [1.0 if v >= ref_val else v / ref_val for v in sm]
    p1 = _dilate_max(norm, 1)
    min_hits = max(4, int(math.ceil(board_size * 0.72)))
    size_weight = math.sqrt(board_size / 19.0)

    # Deduplicate candidate steps within 0.5 px.
    steps: List[float] = []
    for s in candidate_steps:
        if s < 5.0 or (board_size - 1) * s >= n - 2:
            continue
        if all(abs(s - existing) > 0.5 for existing in steps):
            steps.append(s)

    # Coarse scan (step=2 px on start, constrained so the grid contains the image center),
    # then local fine scan around the best hit.
    center_lo = n * 0.48
    center_hi = n * 0.52
    coarse_best = (0.0, 0.0, 0.0)
    for step in steps:
        span = (board_size - 1) * step
        min_start = max(1, int(math.floor(center_lo - span)))
        max_start = min(int(n - 2 - span), int(math.ceil(center_hi)))
        for start_i in range(min_start, max_start + 1, 2):
            score, hits = _comb_score(p1, n, float(start_i), step, board_size, size_weight)
            if hits >= min_hits and score > coarse_best[0]:
                coarse_best = (score, float(start_i), step)

    if coarse_best[0] <= 0:
        return coarse_best

    best = coarse_best
    _, c_start, c_step = coarse_best
    for ds in (-0.4, -0.2, 0.0, 0.2, 0.4):
        step = c_step + ds
        span = (board_size - 1) * step
        if step < 5.0 or span >= n - 2:
            continue
        lo_s = max(1, int(c_start) - 3)
        hi_s = min(int(n - 2 - span), int(c_start) + 3)
        for start_i in range(lo_s, hi_s + 1):
            score, hits = _comb_score(p1, n, float(start_i), step, board_size, size_weight)
            if hits >= min_hits and score > best[0]:
                best = (score, float(start_i), step)

    # Sub-pixel refinement via weighted local maxima + OLS linear fit.
    _score, start, step = best
    xs_idx: List[float] = []
    xs_pos: List[float] = []
    radius = max(2, int(round(step * 0.22)))
    for k in range(board_size):
        pred = start + k * step
        c = int(round(pred))
        lo = max(0, c - radius)
        hi = min(n - 1, c + radius)
        peak_i = max(range(lo, hi + 1), key=lambda i: sm[i])
        if norm[peak_i] >= 0.15:
            num = 0.0
            den = 0.0
            for j in range(max(0, peak_i - 1), min(n, peak_i + 2)):
                num += j * sm[j]
                den += sm[j]
            refined = num / den if den > 0 else float(peak_i)
            xs_idx.append(float(k))
            xs_pos.append(refined)

    if len(xs_idx) >= max(4, board_size // 2):
        m = len(xs_idx)
        sx = sum(xs_idx)
        sy = sum(xs_pos)
        sxx = sum(x * x for x in xs_idx)
        sxy = sum(x * y for x, y in zip(xs_idx, xs_pos))
        denom = m * sxx - sx * sx
        if abs(denom) > 1e-9:
            b = (m * sxy - sx * sy) / denom
            a = (sy - b * sx) / m
            if 5.0 <= b and 0 <= a and a + (board_size - 1) * b <= n - 1:
                start, step = a, b
                refined_score, _hits = _comb_score(p1, n, start, step, board_size, size_weight)
                best = (refined_score, start, step)
    return best


def _fit_grid_for_profiles(
    vx: Sequence[float],
    hy: Sequence[float],
    sizes: Sequence[int],
) -> Tuple[float, int, float, float, float, float]:
    """Return ``(score, board_size, x0, x_step, y0, y_step)``."""
    w, h = len(vx), len(hy)
    best = (0.0, sizes[0], 0.0, 0.0, 0.0, 0.0)

    for size in sizes:
        min_step = max(6.0, min(w, h) * 0.22 / (size - 1))
        max_step = min(w, h) * 0.98 / (size - 1)
        x_steps = _autocorr_steps(vx, min_step, max_step, top_k=5)
        y_steps = _autocorr_steps(hy, min_step, max_step, top_k=5)
        shared = list(x_steps) + list(y_steps)
        shared += [s * 0.5 for s in shared if s * 0.5 >= min_step]
        if not shared:
            continue

        sx, x0, x_step = _best_comb_1d(vx, size, shared)
        if sx <= 0:
            continue
        y_cands = [x_step] + [s for s in shared if abs(s - x_step) / x_step <= 0.04]
        sy, y0, y_step = _best_comb_1d(hy, size, y_cands)
        if sy <= 0 or abs(x_step - y_step) / max(x_step, y_step) > 0.04:
            continue

        combined = 0.5 * (sx + sy) + 0.01 * (size / 19.0)
        if combined > best[0]:
            best = (combined, size, x0, x_step, y0, y_step)
        # Early exit: a high-confidence 19x19 lock cannot be beaten by a 13x13/9x9 subset.
        if size == 19 and combined >= 0.50:
            break

    return best


def _default_corners(w: int, h: int) -> Corners:
    side = min(w, h) * 0.82
    x0 = (w - side) * 0.5
    y0 = (h - side) * 0.5
    x1 = x0 + side
    y1 = y0 + side
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _solve_perspective_coeffs(dst_pts: Sequence[Point], src_pts: Sequence[Point]) -> Tuple[float, ...]:
    """Compute the 8 PIL ``PERSPECTIVE`` coefficients mapping ``dst -> src``."""
    matrix: List[List[float]] = []
    for (u, v), (x, y) in zip(dst_pts, src_pts):
        matrix.append([u, v, 1.0, 0.0, 0.0, 0.0, -x * u, -x * v, x])
        matrix.append([0.0, 0.0, 0.0, u, v, 1.0, -y * u, -y * v, y])

    n = 8
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(matrix[r][col]))
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        piv_val = matrix[col][col]
        if abs(piv_val) < 1e-12:
            return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
        for j in range(col, n + 1):
            matrix[col][j] /= piv_val
        for r in range(n):
            if r != col:
                factor = matrix[r][col]
                for j in range(col, n + 1):
                    matrix[r][j] -= factor * matrix[col][j]

    return tuple(matrix[i][n] for i in range(n))


def warp_board(
    image: Image.Image,
    corners: Corners,
    board_size: int,
    cell_px: int = _WARP_CELL_PX,
) -> Image.Image:
    """Perspective-warp the board defined by ``corners`` into a square ``N*cell_px`` image."""
    out_size = board_size * cell_px
    lo = 0.5 * cell_px
    hi = (board_size - 0.5) * cell_px
    dst_pts: Sequence[Point] = ((lo, lo), (hi, lo), (hi, hi), (lo, hi))
    coeffs = _solve_perspective_coeffs(dst_pts, corners)
    rgb_im = image if image.mode == "RGB" else image.convert("RGB")
    return rgb_im.transform(
        (out_size, out_size),
        Image.Transform.PERSPECTIVE,
        coeffs,
        resample=Image.Resampling.BILINEAR,
    )


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def _detect_last_move(
    rgb: bytes,
    w: int,
    h: int,
    stones: Dict[Tuple[int, int], str],
    cell_px: int,
) -> Optional[Tuple[int, int]]:
    """Detect a single standout last-move marker (colored accent, inner circle, triangle, or number).

    When captures occur on the board, stone counts alone can misidentify whose turn it
    is; identifying the last-played stone's marker resolves both the turn and active
    ko captures reliably.
    """
    if len(stones) < 2:
        return None

    half = max(4, cell_px // 2)
    # 0.33 * cell covers inner circles/rings, triangles, and numbers while staying inside the stone.
    inner_r_sq = (0.33 * cell_px) ** 2
    accent_scores: List[Tuple[int, Tuple[int, int]]] = []
    raw_inner: Dict[Tuple[int, int], float] = {}

    for pos, color in stones.items():
        col, row = pos
        cx = int(round((col + 0.5) * cell_px))
        cy = int(round((row + 0.5) * cell_px))
        accent = 0
        anom = 0
        inner_n = 0

        y_lo = max(0, cy - half)
        y_hi = min(h - 1, cy + half)
        x_lo = max(0, cx - half)
        x_hi = min(w - 1, cx + half)
        for yy in range(y_lo, y_hi + 1):
            dy = yy - cy
            dy2 = dy * dy
            row_off = yy * w
            for xx in range(x_lo, x_hi + 1):
                off = (row_off + xx) * 3
                r = rgb[off]
                g = rgb[off + 1]
                b = rgb[off + 2]
                # Vivid non-wood marker colors (red triangle/dot, blue/cyan/green ring).
                if (
                    (r > 155 and r - g > 70 and r - b > 70)
                    or (b > 135 and b - r > 55)
                    or (g > 135 and g - r > 45 and g - b > 45)
                ):
                    accent += 1
                dx = xx - cx
                if dx * dx + dy2 <= inner_r_sq:
                    inner_n += 1
                    luma = 0.299 * r + 0.587 * g + 0.114 * b
                    if (color == "B" and luma > 130.0) or (color == "W" and luma < 120.0):
                        anom += 1

        accent_scores.append((accent, pos))
        raw_inner[pos] = anom / max(1, inner_n)

    # 1. Prefer a unique vivid accent marker (e.g. Fox red/blue triangle, OGS blue dot).
    accent_scores.sort(key=lambda item: item[0], reverse=True)
    top_acc, top_acc_pos = accent_scores[0]
    sec_acc = accent_scores[1][0]
    if top_acc >= 3 and top_acc >= max(sec_acc * 2.5, sec_acc + 3):
        return top_acc_pos

    # 2. Subtract per-color median baseline so glossy 3D stone highlights cancel out, then
    # look for a single stone with a standout inner circle, triangle, or move number.
    baselines: Dict[str, float] = {}
    for color in ("B", "W"):
        vals = [raw_inner[p] for p, c in stones.items() if c == color]
        baselines[color] = _median(vals) if len(vals) >= 3 else 0.0

    inner_scores = sorted(
        ((max(0.0, raw_inner[p] - baselines[c]), p) for p, c in stones.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    top_c, top_c_pos = inner_scores[0]
    sec_c = inner_scores[1][0]
    plain_ratio = sum(1 for val, _p in inner_scores[1:] if val < 0.03) / max(1, len(inner_scores) - 1)
    if top_c >= 0.05 and plain_ratio >= 0.85 and top_c >= max(sec_c * 2.0, sec_c + 0.035):
        return top_c_pos

    return None


def classify_stones(
    image: Image.Image,
    corners: Corners,
    board_size: int,
    cell_px: int = _WARP_CELL_PX,
) -> Tuple[Dict[Tuple[int, int], str], str, Optional[Tuple[int, int]]]:
    """Classify every intersection as Empty, ``"B"``, or ``"W"`` and infer ``next_player``."""
    warped = warp_board(image, corners, board_size, cell_px=cell_px)
    w, h = warped.size
    rgb = warped.tobytes()

    # 1. Estimate board wood color from mid-cell centers (diagonally between 4 intersections).
    rs: List[float] = []
    gs: List[float] = []
    bs: List[float] = []
    for row in range(board_size - 1):
        cy = (row + 1) * cell_px
        for col in range(board_size - 1):
            cx = (col + 1) * cell_px
            sr = sg = sb = 0
            cnt = 0
            for dy in (-1, 0, 1):
                yy = cy + dy
                if 0 <= yy < h:
                    base = yy * w
                    for dx in (-1, 0, 1):
                        xx = cx + dx
                        if 0 <= xx < w:
                            off = (base + xx) * 3
                            sr += rgb[off]
                            sg += rgb[off + 1]
                            sb += rgb[off + 2]
                            cnt += 1
            if cnt:
                rs.append(sr / cnt)
                gs.append(sg / cnt)
                bs.append(sb / cnt)

    br = _median(rs)
    bg = _median(gs)
    bb = _median(bs)
    board_luma = 0.299 * br + 0.587 * bg + 0.114 * bb
    board_chroma = max(br, bg, bb) - min(br, bg, bb)

    # Outer-ring offsets (radius 0.18..0.37 of cell) avoid hoshi dots, move numbers, and markers.
    r_in_sq = (0.18 * cell_px) ** 2
    r_out_sq = (0.37 * cell_px) ** 2
    r_max = int(math.ceil(0.37 * cell_px))
    ring_offsets = [
        (dx, dy)
        for dy in range(-r_max, r_max + 1)
        for dx in range(-r_max, r_max + 1)
        if r_in_sq <= dx * dx + dy * dy <= r_out_sq
    ]
    n_ring = max(1, len(ring_offsets))

    dark_luma_thresh = min(105.0, board_luma - 55.0)
    if board_luma < 95.0:
        dark_luma_thresh = max(35.0, board_luma - 28.0)

    is_warm_board = board_chroma >= 28.0
    white_chroma_cap = max(32.0, board_chroma * 0.58)
    white_luma_floor = max(145.0, board_luma - 28.0) if is_warm_board else min(235.0, board_luma + 28.0)
    white_dist_sq_floor = (34.0 * 34.0) if is_warm_board else (28.0 * 28.0)
    wood_dist_sq_cap = 34.0 * 34.0
    mean_dist_sq_floor = 32.0 * 32.0

    stones: Dict[Tuple[int, int], str] = {}
    black_count = 0
    white_count = 0

    for row in range(board_size):
        cy = int(round((row + 0.5) * cell_px))
        for col in range(board_size):
            cx = int(round((col + 0.5) * cell_px))
            dark_px = 0
            white_px = 0
            wood_px = 0
            luma_sum = 0.0
            dist_sq_sum = 0.0
            valid = 0

            for dx, dy in ring_offsets:
                xx = cx + dx
                yy = cy + dy
                if 0 <= xx < w and 0 <= yy < h:
                    off = (yy * w + xx) * 3
                    r = rgb[off]
                    g = rgb[off + 1]
                    b = rgb[off + 2]
                    luma = 0.299 * r + 0.587 * g + 0.114 * b
                    dr = r - br
                    dg = g - bg
                    db = b - bb
                    dist_sq = dr * dr + dg * dg + db * db
                    mx = r if r >= g else g
                    if b > mx:
                        mx = b
                    mn = r if r <= g else g
                    if b < mn:
                        mn = b
                    chroma = mx - mn

                    luma_sum += luma
                    dist_sq_sum += dist_sq
                    valid += 1

                    if dist_sq < wood_dist_sq_cap:
                        wood_px += 1
                    if luma < dark_luma_thresh and chroma < 55:
                        dark_px += 1
                    elif luma >= white_luma_floor and dist_sq >= white_dist_sq_floor and chroma <= white_chroma_cap:
                        white_px += 1

            denom = valid or n_ring
            frac_dark = dark_px / denom
            frac_white = white_px / denom
            frac_wood = wood_px / denom
            mean_luma = luma_sum / denom
            mean_dist_sq = dist_sq_sum / denom

            if frac_dark >= 0.42 and frac_dark > frac_white and mean_luma < board_luma - 35.0:
                stones[(col, row)] = "B"
                black_count += 1
            elif frac_white >= 0.42 and frac_white > frac_dark and frac_wood < 0.45 and mean_dist_sq >= mean_dist_sq_floor:
                stones[(col, row)] = "W"
                white_count += 1

    last_move = _detect_last_move(rgb, w, h, stones, cell_px)
    if last_move is not None:
        next_player = "W" if stones[last_move] == "B" else "B"
    else:
        next_player = "B" if black_count <= white_count else "W"
    return stones, next_player, last_move


def grid_to_board_box(corners: Corners, board_size: int, img_w: int, img_h: int) -> Corners:
    """Expand outer grid-line ``corners`` by 0.5 cell on each side so border stones sit inside the box."""
    tl, tr, br, bl = corners
    grid_w = tr[0] - tl[0]
    grid_h = bl[1] - tl[1]
    side = 0.5 * (grid_w + grid_h)
    half_cell = 0.5 * side / max(1, board_size - 1)
    cx = 0.5 * (tl[0] + tr[0])
    cy = 0.5 * (tl[1] + bl[1])
    half_box = 0.5 * side + half_cell
    max_half = min(cx, cy, (img_w - 1) - cx, (img_h - 1) - cy)
    half_box = max(1.0, min(half_box, max_half))
    x0, x1 = cx - half_box, cx + half_box
    y0, y1 = cy - half_box, cy + half_box
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def board_box_to_grid(box_corners: Corners, board_size: int) -> Corners:
    """Inverse of :func:`grid_to_board_box`: inset by 0.5 cell from the outer board box."""
    tl, tr, br, bl = box_corners
    box_w = tr[0] - tl[0]
    box_h = bl[1] - tl[1]
    inset_x = 0.5 * box_w / max(1, board_size)
    inset_y = 0.5 * box_h / max(1, board_size)
    x0, x1 = tl[0] + inset_x, tr[0] - inset_x
    y0, y1 = tl[1] + inset_y, bl[1] - inset_y
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def detect_board_in_box(
    image: Image.Image,
    box_corners: Corners,
    fallback_board_size: int = 19,
) -> BoardDetectionResult:
    """Re-detect the board grid inside a user-dragged square ``box_corners``.

    Falls back to classifying directly from ``board_box_to_grid(box_corners)``
    when the cropped region has low grid-detection confidence.
    """
    orig_w, orig_h = image.size
    tl, tr, br, bl = box_corners
    bw = max(1.0, tr[0] - tl[0])
    bh = max(1.0, bl[1] - tl[1])
    pad = max(4.0, 0.04 * max(bw, bh))
    cx0 = max(0, int(math.floor(tl[0] - pad)))
    cy0 = max(0, int(math.floor(tl[1] - pad)))
    cx1 = min(orig_w, int(math.ceil(br[0] + pad)))
    cy1 = min(orig_h, int(math.ceil(br[1] + pad)))

    if cx1 - cx0 >= 48 and cy1 - cy0 >= 48:
        cropped = image.crop((cx0, cy0, cx1, cy1))
        sub = detect_board(cropped)
        if sub.confidence >= 0.30:
            shifted_grid: Corners = tuple((pt[0] + cx0, pt[1] + cy0) for pt in sub.corners)  # type: ignore[assignment]
            shifted_box = grid_to_board_box(shifted_grid, sub.board_size, orig_w, orig_h)
            stones, next_player, last_move = classify_stones(image, shifted_grid, sub.board_size)
            return BoardDetectionResult(
                board_size=sub.board_size,
                corners=shifted_grid,
                stones=stones,
                next_player=next_player,
                confidence=sub.confidence,
                last_move=last_move,
                box_corners=shifted_box,
            )

    grid = board_box_to_grid(box_corners, fallback_board_size)
    stones, next_player, last_move = classify_stones(image, grid, fallback_board_size)
    return BoardDetectionResult(
        board_size=fallback_board_size,
        corners=grid,
        stones=stones,
        next_player=next_player,
        confidence=0.0,
        last_move=last_move,
        box_corners=box_corners,
    )


def drag_square_corners(
    corners: Corners,
    corner_index: int,
    target: Point,
    img_w: int,
    img_h: int,
    min_side: float = 32.0,
) -> Corners:
    """Resize an axis-aligned square box by dragging ``corner_index`` while anchoring the opposite corner."""
    opp_x, opp_y = corners[(corner_index + 2) % 4]
    tx = max(0.0, min(float(img_w - 1), target[0]))
    ty = max(0.0, min(float(img_h - 1), target[1]))

    # Direction signs from the anchored opposite corner toward the dragged corner:
    # 0=TL (-,-), 1=TR (+,-), 2=BR (+,+), 3=BL (-,+)
    sx = -1.0 if corner_index in (0, 3) else 1.0
    sy = -1.0 if corner_index in (0, 1) else 1.0

    # Max square side that stays inside image bounds along both axes.
    max_x = opp_x if sx < 0 else (img_w - 1 - opp_x)
    max_y = opp_y if sy < 0 else (img_h - 1 - opp_y)
    max_side = max(1.0, min(max_x, max_y))

    dx = (tx - opp_x) * sx
    dy = (ty - opp_y) * sy
    desired = 0.5 * (max(0.0, dx) + max(0.0, dy))
    side = max(min(min_side, max_side), min(max_side, desired))

    drag_x = opp_x + sx * side
    drag_y = opp_y + sy * side
    x0, x1 = (drag_x, opp_x) if sx < 0 else (opp_x, drag_x)
    y0, y1 = (drag_y, opp_y) if sy < 0 else (opp_y, drag_y)
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def detect_board(image: Image.Image, board_size: Optional[int] = None) -> BoardDetectionResult:
    """Auto-detect the Go board grid and classify stones from a screenshot image."""
    orig_w, orig_h = image.size
    scale = min(1.0, _WORK_MAX_DIM / max(orig_w, orig_h))
    work_w = max(32, int(round(orig_w * scale)))
    work_h = max(32, int(round(orig_h * scale)))

    gray = image.convert("L").resize((work_w, work_h), Image.Resampling.BILINEAR)
    sizes = (board_size,) if board_size in SUPPORTED_BOARD_SIZES else SUPPORTED_BOARD_SIZES

    # Pass 1: full-image ridge profiles with center-constrained grid search.
    vx_d, hy_d = _ridge_profiles(gray, bright=False)
    best_d = _fit_grid_for_profiles(vx_d, hy_d, sizes)

    best = best_d
    use_bright = False
    if best_d[0] < 0.35:
        vx_b, hy_b = _ridge_profiles(gray, bright=True)
        best_b = _fit_grid_for_profiles(vx_b, hy_b, sizes)
        if best_b[0] > best_d[0]:
            best = best_b
            use_bright = True

    score, det_size, x0, x_step, y0, y_step = best

    if score < 0.20 or x_step <= 0 or y_step <= 0:
        fallback_size = board_size or 19
        corners = _default_corners(orig_w, orig_h)
        stones, next_player, last_move = classify_stones(image, corners, fallback_size)
        return BoardDetectionResult(
            board_size=fallback_size,
            corners=corners,
            stones=stones,
            next_player=next_player,
            confidence=0.0,
            last_move=last_move,
            box_corners=grid_to_board_box(corners, fallback_size, orig_w, orig_h),
        )

    # Pass 2: refine grid strictly inside the detected board ROI to suppress UI chrome.
    rx0 = max(0, int(math.floor(x0 - x_step * 0.5)))
    rx1 = min(work_w, int(math.ceil(x0 + (det_size - 0.5) * x_step)))
    ry0 = max(0, int(math.floor(y0 - y_step * 0.5)))
    ry1 = min(work_h, int(math.ceil(y0 + (det_size - 0.5) * y_step)))
    vx2, hy2 = _ridge_profiles(gray, rx0, rx1, ry0, ry1, bright=use_bright)
    refined = _fit_grid_for_profiles(vx2, hy2, (det_size,))
    if refined[0] >= score * 0.85 and refined[3] > 0 and refined[5] > 0:
        score, det_size, x0, x_step, y0, y_step = refined

    # Keep the detected box an exact square centered on the fitted grid.
    inv = 1.0 / scale
    cx = (x0 + 0.5 * (det_size - 1) * x_step) * inv
    cy = (y0 + 0.5 * (det_size - 1) * y_step) * inv
    half_side = 0.5 * (det_size - 1) * (0.5 * (x_step + y_step)) * inv
    lx = max(0.0, cx - half_side)
    rx = min(float(orig_w - 1), cx + half_side)
    ty = max(0.0, cy - half_side)
    by = min(float(orig_h - 1), cy + half_side)
    corners: Corners = ((lx, ty), (rx, ty), (rx, by), (lx, by))

    stones, next_player, last_move = classify_stones(image, corners, det_size)
    return BoardDetectionResult(
        board_size=det_size,
        corners=corners,
        stones=stones,
        next_player=next_player,
        confidence=min(1.0, score),
        last_move=last_move,
        box_corners=grid_to_board_box(corners, det_size, orig_w, orig_h),
    )


def detect_ko_capture(
    stones: Dict[Tuple[int, int], str],
    last_move: Optional[Tuple[int, int]],
    board_size: int,
) -> Optional[Tuple[int, int]]:
    """Return the ko-captured point if ``last_move`` has the exact geometry of a single-stone ko capture."""
    if last_move is None or last_move not in stones:
        return None
    color = stones[last_move]
    opp = "W" if color == "B" else "B"

    def neighbors(pt: Tuple[int, int]) -> List[Tuple[int, int]]:
        c, r = pt
        return [
            (c + dc, r + dr)
            for dc, dr in ((-1, 0), (1, 0), (0, -1), (0, 1))
            if 0 <= c + dc < board_size and 0 <= r + dr < board_size
        ]

    # 1. last_move must be an isolated stone with exactly 1 empty neighbor (the captured point)
    #    and all other orthogonal neighbors occupied by the opponent.
    lm_nbs = neighbors(last_move)
    empty_nbs = [nb for nb in lm_nbs if nb not in stones]
    if len(empty_nbs) != 1:
        return None
    if any(stones.get(nb) == color for nb in lm_nbs):
        return None

    ko_pt = empty_nbs[0]
    # 2. Every orthogonal neighbor of ko_pt other than last_move must be friendly (`color`),
    #    meaning an opponent stone at ko_pt had last_move as its sole liberty before capture.
    ko_other_nbs = [nb for nb in neighbors(ko_pt) if nb != last_move]
    if not ko_other_nbs or any(stones.get(nb) != color for nb in ko_other_nbs):
        return None

    # 3. Verify that placing the pre-capture opponent stone at ko_pt (without last_move)
    #    leaves every adjacent friendly chain with at least one liberty (legal pre-capture board).
    pre_stones = {p: c for p, c in stones.items() if p != last_move}
    pre_stones[ko_pt] = opp
    for start in ko_other_nbs:
        visited = {start}
        stack = [start]
        has_liberty = False
        while stack and not has_liberty:
            cur = stack.pop()
            for nb in neighbors(cur):
                if nb not in pre_stones:
                    has_liberty = True
                    break
                if pre_stones[nb] == color and nb not in visited:
                    visited.add(nb)
                    stack.append(nb)
        if not has_liberty:
            return None

    return ko_pt


def build_position_sgf(
    board_size: int,
    stones: Dict[Tuple[int, int], str],
    next_player: str = "B",
    komi: float = 6.5,
    rules: str = "japanese",
    last_move: Optional[Tuple[int, int]] = None,
) -> str:
    """Format a detected board position as a standard SGF string (no ``AE``)."""
    root = build_position_tree(
        board_size, stones, next_player=next_player, komi=komi, rules=rules, last_move=last_move
    )
    return root.sgf()


def build_position_tree(
    board_size: int,
    stones: Dict[Tuple[int, int], str],
    next_player: str = "B",
    komi: float = 6.5,
    rules: str = "japanese",
    last_move: Optional[Tuple[int, int]] = None,
) -> GameNode:
    """Build a ``GameNode`` tree with ``AB``/``AW``/``PL`` and no ``AE``.

    When ``last_move`` matches an active ko capture (and agrees with ``next_player``),
    emits a 2-node standard SGF tree (pre-capture root + capture move child) so both
    KaTrain and KataGo automatically enforce the ko ban.
    """
    player_next = "W" if next_player == "W" else "B"
    ko_pt = (
        detect_ko_capture(stones, last_move, board_size)
        if last_move is not None and stones.get(last_move) != player_next
        else None
    )

    if ko_pt is not None and last_move is not None:
        last_color = stones[last_move]
        opp_color = "W" if last_color == "B" else "B"
        root_stones = {pos: col for pos, col in stones.items() if pos != last_move}
        root_stones[ko_pt] = opp_color
        root_player = last_color
    else:
        root_stones = stones
        root_player = player_next

    ab: List[str] = []
    aw: List[str] = []
    for (col, row), color in sorted(root_stones.items()):
        if not (0 <= col < board_size and 0 <= row < board_size):
            continue
        sgf_coord = Move((col, board_size - 1 - row), player=color).sgf((board_size, board_size))
        if color == "B":
            ab.append(sgf_coord)
        elif color == "W":
            aw.append(sgf_coord)

    props: Dict[str, object] = {
        "FF": 4,
        "GM": 1,
        "SZ": board_size,
        "KM": komi,
        "RU": rules,
        "PL": root_player,
    }
    if ab:
        props["AB"] = ab
    if aw:
        props["AW"] = aw
    root = GameNode(properties=props)

    if ko_pt is not None and last_move is not None:
        lm_col, lm_row = last_move
        lm_move = Move((lm_col, board_size - 1 - lm_row), player=stones[last_move])
        GameNode(parent=root, move=lm_move)

    return root
