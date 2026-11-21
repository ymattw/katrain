# Import Go Board Position from Clipboard Screenshot

Design document for loading a Go board state directly from a screenshot in the
system clipboard (`Cmd+V` / `Ctrl+V`), so the user can immediately analyze the
position or play variations without replaying from move 0.

## 1. Summary

- Pressing `Cmd+V` / `Ctrl+V` checks whether the clipboard holds SGF text or an
  `http(s)://` URL first; if not, it checks for **raw image data** in the
  clipboard (explicitly ignoring file-path lists such as macOS Finder copies).
- When a screenshot image is found, KaTrain opens `ScreenshotImportModal` with:
  - The screenshot with a dimmed outer scrim, a square outer border expanded by
    `0.5` cell beyond the outer grid lines (so 1st-line and corner stones sit
    fully inside the bright region), and 4 small square corner handles (`TL`,
    `TR`, `BR`, `BL`) that keep the box strictly square while dragging and
    automatically re-detect the board on release.
  - A **Next to play** row with a clickable Black/White stone indicator
    (auto-detected from last-move marker or stone count parity), an aligned
    **Komi** input (default `7.5`), **Cancel**, and **Load**.
- Clicking **Load** creates a single root `GameNode` under **Chinese
  rules** (`RU[chinese]`) with `SZ`, `KM`, `AB`, `AW`, and `PL` (**no `AE` clear
  placements**) and loads it into Analysis mode so KataGo evaluates the position
  in a single query. Because Chinese area scoring does not depend on historical
  prisoner counts, static mid-game positions score accurately without needing
  capture history.
- Uses **Pillow only** (`pillow` in `pyproject.toml`) with **zero changes to
  `spec/KaTrain.spec`** and **zero changes to upstream `.po`/`.mo` files**.

## 2. Architecture

### 2.1 Core recognition & SGF builder (`katrain/core/screenshot.py`)

1. **`read_clipboard_image()`**
   - On macOS, reads `public.png` / `public.tiff` directly from `NSPasteboard`
     via typed `ctypes` calls (avoiding Pillow's slow `osascript` + hex-dump
     subprocess) and returns `None` when `public.file-url` or
     `NSFilenamesPboardType` is present.
   - On Windows, uses `PIL.ImageGrab.grabclipboard()`, accepting **only**
     `PIL.Image.Image` instances (returning `None` for `list` file paths).
   - Falls back to `wl-paste` / `xclip` PNG reads on Linux.

2. **`detect_board(image, board_size=None)`**
   - Downscales grayscale copy to max dimension `480 px`.
   - Computes 1D vertical-line (`vx`) and horizontal-line (`hy`) ridge profiles
     in C via Pillow (`ImageChops` + `resize(..., BOX)`) using a thin-ridge
     filter (`center` darker than both `±1`/`±2` neighbors with similar
     luminance on both sides, `abs(side1 - side2) < 32`).
   - Estimates candidate cell spacings via 1D autocorrelation, normalizes
     against a robust interior-line peak (so thick outer board borders or UI
     divider bars do not crush interior lines), and runs a center-constrained
     comb-filter search across `N ∈ {19, 13, 9}` (requiring the grid to contain
     the image center, early-exiting on high-confidence `19×19` matches,
     requiring near-square cells `abs(x_step - y_step) / max <= 0.04`,
     penalizing between-line midpoint ridges to reject `2×`/`3×` step
     harmonics, and penalizing multi-step grid continuations outside `0..N-1`).
   - Runs a second pass restricted to the detected board ROI and refines
     `(x0, x_step)` / `(y0, y_step)` to sub-pixel precision via weighted line
     centroids and ordinary least squares.

3. **`warp_board()` & `classify_stones()`**
   - Perspective-warps the 4-corner quad (with a `0.5`-cell outer margin so
     1st-line and corner stones are not clipped) into a canonical `N * 24 px`
     square image via `PIL.Image.transform(..., PERSPECTIVE, BILINEAR)` using a
     pure-Python 8×8 Gaussian elimination solver.
   - Estimates the board wood color from the median RGB of all `(N-1)×(N-1)`
     mid-cell points (halfway diagonally between 4 intersections).
   - Classifies each intersection using pixels in an outer ring (`0.18..0.37`
     cell radius), which avoids star points (hoshi) at the center as well as
     printed move numbers and last-move markers.
   - Detects a standout **last-move marker** (`_detect_last_move`: vivid colored
     accent such as Fox's red/blue triangle or OGS's blue marker, or a unique
     inner circle/ring, triangle, or move number within `0.33 * cell` after
     subtracting per-color median baseline) to infer `next_player` accurately
     even when captures have equalized stone counts; falls back to stone-count
     parity (`"B"` if `#B <= #W` else `"W"`).

4. **`detect_ko_capture()`, `build_position_tree()` & `build_position_sgf()`**
   - Maps image `(col, row)` (top-left origin) to `pysgf` `Move((col, N - 1 - row))`
     SGF coordinates (`aa` = top-left) and constructs a root `GameNode` with
     `AB`, `AW`, `PL`, `SZ`, `KM`, `RU`, and no `AE`.
   - When `last_move` matches the exact geometry of an active single-stone ko
     capture (and agrees with `next_player`), reconstructs a 2-node standard
     SGF tree (pre-capture root with the captured opponent stone restored +
     child capture move) and selects the child node on load so both KaTrain and
     KataGo automatically enforce the ko rule.

### 2.2 Verification modal (`katrain/gui/widgets/screenshot_popup.py`)

- `ScreenshotBoardCanvas`: displays the screenshot `Texture` (letterboxed,
  capped at 1200 px for instant OpenGL upload), a translucent dark scrim fading
  the area outside the expanded (`+0.5` cell) board square, a square outer
  border, and 4 small square corner handles that keep the bounding box strictly
  square while dragging (`drag_square_corners`) and trigger `detect_board_in_box`
  automatically on release.
- `StoneToggleButton`: clickable KaTrain Black/White stone (`B_stone.png` /
  `W_stone.png`) placed inline next to `"Next to play"`.
- `ScreenshotImportPopup` & `ScreenshotImportModal`: self-contained `Builder.load_string`
  KV rules using `l10n._(...)` from `katrain/core/l10n.py`, avoiding edits to
  `katrain/popups.kv`, `katrain/gui/popups.py`, and upstream i18n files.

### 2.3 Upstream wiring (`katrain/__main__.py`)

- `KaTrainGui.load_sgf_from_clipboard()` checks `read_clipboard_image()` first
  (sub-millisecond no-op for text clipboards on macOS; avoids spawning `pbpaste`
  when pasting screenshots) and opens `ScreenshotImportModal` via
  `_open_screenshot_popup(image)`, falling back to the existing SGF/URL paste
  path otherwise.

## 3. Verification

- `tests/test_screenshot.py`:
  - Clipboard path-rejection & image-acceptance tests.
  - Synthetic 19×19 board with mobile UI chrome, 1st-line edge stones, hoshi
    dots, and a numbered/marked stone.
  - Synthetic 13×13 and 9×9 board-size inference.
  - Neutral grey board theme stone classification.
  - `build_position_tree` / `build_position_sgf` coordinate mapping and zero
    `clear_placements` (`AE`) assertion.
- Verified end-to-end against a real 1706×2106 Fox mobile screenshot (`19×19`,
  57 Black + 57 White stones, including move `115` with digits/marker and
  1st-line edge stones at `T11` and `M1`).
