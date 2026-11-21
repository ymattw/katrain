# KaTrain Enhanced

This is a personal fork of [sanderland/katrain](https://github.com/sanderland/katrain).
Ongoing work lives on the `enhanced` branch, organized as isolated modules and
minimal upstream hooks so it rebases cleanly onto new upstream releases.

Current upstream base: **v1.20.0**

## Features

- **Local build & packaging (`Makefile`)**: single entry point (`make deps`,
  `make run`, `make test`, `make build`) for macOS, Linux, and Windows.
- **Node deletion with <kbd>Backspace</kbd> / <kbd>Del</kbd>**
  ([design doc](docs/backspace-del.md)): works without <kbd>Ctrl</kbd> and
  fixes deletion on macOS (<kbd>Ctrl+Del</kbd> still works).
- **Undo for deleted nodes and pruned branches** ([design doc](docs/undo.md)):
  <kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Cmd</kbd>+<kbd>Z</kbd> restores the most
  recent deleted node or pruned branch, invalidating automatically if a new
  branch is played at the same position.
- **Load games from Fox Weiqi (foxwq.com)** ([design doc](docs/fox.md)):
  search recent public games by username directly from the Load dialog, with
  result-colored rows and colored stone indicators.
- **Load games from OGS (online-go.com)** ([design doc](docs/ogs.md)):
  search and paginate public games by username from the Load dialog.
- **Automatic KataGo thread tuning** ([design doc](docs/auto-threads.md)):
  derives `numAnalysisThreads` and `numSearchThreads` from CPU count so the
  desktop stays responsive during full-game analysis.
- **Lazy hover PV refinement & bulk analysis scheduler**
  ([design doc](docs/hover-pv.md)): hovering a shallow candidate move pauses
  and drains background full-game analysis so KataGo deepens its variation
  promptly, then resumes without discarding completed nodes.

## Building from this fork

Requirements: [uv](https://docs.astral.sh/uv/), and a POSIX shell (Git Bash on
Windows). On macOS, Xcode command line tools and Homebrew are also required.

```bash
git clone https://github.com/ymattw/katrain.git
cd katrain
git checkout enhanced

make deps       # install/verify build dependencies
make test       # run pytest, upstream i18n check, and fork l10n check
make run        # run KaTrain from source
make build      # engine + app + native distributable (dist/ and package/)
```
