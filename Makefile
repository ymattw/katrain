# KaTrain build orchestration for macOS, Linux and Windows.
#
# The Makefile is the single entry point for building and running the app from
# this fork, and mirrors the jobs in .github/workflows/test_and_build.yaml.
#
# Requirements:
#   - uv: https://docs.astral.sh/uv/
#   - a POSIX shell; on Windows run make from Git Bash or MSYS2
#   - macOS app builds additionally need the Xcode command line tools and the
#     Homebrew packages installed by `make deps`
#
# Usage:
#   make deps       install/verify build dependencies
#   make sync       create/update the uv virtualenv
#   make test       run the test suite and i18n check
#   make run        run KaTrain from source
#   make katago     build the engine (macOS) or verify the bundled binary
#   make app        freeze the app with PyInstaller
#   make package    create a .dmg (macOS) / .zip (Windows) / .tar.gz (Linux)
#   make build      sync + katago + app + package
#   make clean      remove build artifacts
#
# Variables:
#   FORCE=1         force a KataGo rebuild even if the binary exists
#   KATAGO_SRC=...  KataGo source checkout (default: ../KataGo)

SHELL := /bin/bash
.DEFAULT_GOAL := help

REPO := $(CURDIR)

# --- platform detection -----------------------------------------------------
ifeq ($(OS),Windows_NT)
  PLATFORM := windows
  BACKEND := OPENCL
  ARCH := x86_64
else
  UNAME_S := $(shell uname -s)
  UNAME_M := $(shell uname -m)
  ifeq ($(UNAME_S),Darwin)
    PLATFORM := macos
    ifeq ($(UNAME_M),arm64)
      BACKEND := METAL
      ARCH := arm64
    else
      BACKEND := OPENCL
      ARCH := x86_64
    endif
  else
    PLATFORM := linux
    BACKEND := OPENCL
    ARCH := $(UNAME_M)
  endif
endif

# --- tools and metadata -----------------------------------------------------
UV ?= uv
JOBS ?= $(shell nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

# Version comes from katrain/core/constants.py (what CI's get-version action reads).
VERSION := $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' katrain/core/constants.py)

KATAGO_SRC ?= $(REPO)/../KataGo
ifeq ($(PLATFORM),windows)
  KATAGO_BIN := katrain/KataGo/katago.exe
else ifeq ($(PLATFORM),linux)
  KATAGO_BIN := katrain/KataGo/katago
else
  KATAGO_BIN := katrain/KataGo/katago-osx
endif

DIST := dist
PKG := package

ifeq ($(PLATFORM),macos)
  APP_OUT := $(DIST)/KaTrain.app
else ifeq ($(PLATFORM),windows)
  APP_OUT := $(DIST)/KaTrain.exe
else
  APP_OUT := $(DIST)/KaTrain
endif

# The macOS build removes the Linux/Windows engine binaries before freezing so
# they are not bundled. These are tracked files, moved aside during the build.
NON_MACOS_KATAGO := katrain/KataGo/katago katrain/KataGo/katago.exe katrain/KataGo/*.dll

.PHONY: help deps sync test run katago katago-build app package build clean restore-katago

help: ## Show this help
	@echo "KaTrain $(VERSION) -- platform: $(PLATFORM), arch: $(ARCH), KataGo backend: $(BACKEND)"
	@echo
	@echo "Targets:"
	@sed -n 's/^\([a-zA-Z_-]*\):.*## \(.*\)/  \1  \2/p' $(MAKEFILE_LIST)

deps: ## Install/verify build dependencies
	@if [ "$(PLATFORM)" = "macos" ]; then \
		missing=0; \
		for pkg in cmake ninja libzip protobuf abseil; do \
			if brew list --versions $$pkg >/dev/null 2>&1; then echo "ok:       $$pkg"; \
			else echo "install:  $$pkg"; brew install $$pkg || missing=1; fi; \
		done; \
		[ $$missing -eq 0 ] || exit 1; \
	elif [ "$(PLATFORM)" = "linux" ]; then \
		echo "Install system dependencies (Debian/Ubuntu):"; \
		echo "  sudo apt-get install python3-dev build-essential git ffmpeg libsdl2-dev \\"; \
		echo "    libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev libportmidi-dev \\"; \
		echo "    libswscale-dev libavformat-dev libavcodec-dev zlib1g-dev libzip-dev \\"; \
		echo "    libgstreamer1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \\"; \
		echo "    libpulse-dev pkg-config libgl-dev opencl-headers ocl-icd-opencl-dev"; \
	else \
		echo "Windows: no extra system packages needed; a KataGo binary is bundled."; \
	fi
	@command -v $(UV) >/dev/null 2>&1 || { echo "Missing uv: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
	@echo "deps ok"

sync: ## Create/update the uv virtualenv
	$(UV) sync --group dev

test: sync ## Run the test suite and i18n check
	$(UV) run pytest tests
	$(UV) run python i18n.py -todo

run: sync katago ## Run KaTrain from source
	$(UV) run katrain

katago: ## Build the engine (macOS) or verify the bundled binary
	@if [ "$(PLATFORM)" = "macos" ]; then \
		if [ "$(FORCE)" = "1" ] || [ ! -x "$(KATAGO_BIN)" ]; then $(MAKE) katago-build; \
		else echo "KataGo already built at $(KATAGO_BIN) (use FORCE=1 to rebuild)"; fi; \
	elif [ -e "$(KATAGO_BIN)" ]; then \
		echo "Using bundled KataGo: $(KATAGO_BIN)"; \
	else \
		echo "No KataGo binary found at $(KATAGO_BIN)"; exit 1; \
	fi

katago-build: ## Force a clean KataGo build (macOS)
	@echo ">> Building KataGo ($(BACKEND)) from $(KATAGO_SRC)"
	@if [ ! -d "$(KATAGO_SRC)/.git" ]; then \
		echo ">> Cloning KataGo (stable) into $(KATAGO_SRC)"; \
		git clone --depth 1 --branch stable https://github.com/lightvector/KataGo.git "$(KATAGO_SRC)"; \
	fi
	cd "$(KATAGO_SRC)/cpp" && cmake -G Ninja . -DUSE_BACKEND=$(BACKEND) -DBUILD_DISTRIBUTED=1
	cd "$(KATAGO_SRC)/cpp" && cmake --build . -j$(JOBS)
	cp "$(KATAGO_SRC)/cpp/katago" "$(KATAGO_BIN)"
	@echo ">> Built: $$($(KATAGO_BIN) version | head -1)"

app: sync katago ## Freeze the app with PyInstaller
	@echo ">> Freezing KaTrain $(VERSION) for $(PLATFORM)"
ifeq ($(PLATFORM),macos)
	@set -e; \
	backup=$$(mktemp -d); \
	files=$$(ls $(NON_MACOS_KATAGO) 2>/dev/null || true); \
	restore() { for f in $$files; do [ -e "$$backup/$$(basename $$f)" ] && mv "$$backup/$$(basename $$f)" "$$f"; done; rm -rf "$$backup"; }; \
	trap restore EXIT INT TERM; \
	for f in $$files; do mv "$$f" "$$backup/"; done; \
	KATRAIN_VERSION=$(VERSION) KIVY_HEADLESS=1 KIVY_NO_WINDOW=1 KIVY_GL_BACKEND=mock SDL_VIDEODRIVER=dummy \
		$(UV) run pyinstaller spec/KaTrain.spec --clean --noconfirm
else
	KATRAIN_VERSION=$(VERSION) $(UV) run pyinstaller spec/KaTrain.spec --clean --noconfirm
endif

package: app ## Create a native distributable (dmg / zip / tar.gz)
	@mkdir -p $(PKG)
ifeq ($(PLATFORM),macos)
	codesign --force --deep --sign - $(APP_OUT)
	rm -rf dmg_temp && mkdir -p dmg_temp
	cp -R $(APP_OUT) dmg_temp/
	ln -s /Applications dmg_temp/Applications
	hdiutil create -volname "KaTrain $(VERSION)" -srcfolder dmg_temp -ov -format UDZO $(PKG)/KaTrain-$(VERSION)-$(ARCH).dmg
	rm -rf dmg_temp
	@echo ">> Created $(PKG)/KaTrain-$(VERSION)-$(ARCH).dmg"
else ifeq ($(PLATFORM),windows)
	$(UV) run python -c "import os,shutil; os.path.exists('$(DIST)/DebugKaTrain/KaTrain.exe') and shutil.copy2('$(DIST)/DebugKaTrain/KaTrain.exe','$(DIST)/KaTrain/debugkatrain.exe'); os.path.exists('$(DIST)/KaTrain.exe') and shutil.copy2('$(DIST)/KaTrain.exe','$(PKG)/KaTrain.exe'); os.path.isdir('$(DIST)/KaTrain') and shutil.make_archive('$(PKG)/KaTrain','zip','$(DIST)','KaTrain')"
	@echo ">> Created $(PKG)/KaTrain.exe and $(PKG)/KaTrain.zip"
else
	tar -C $(DIST) -czf $(PKG)/KaTrain-$(VERSION)-linux-$(ARCH).tar.gz KaTrain
	@echo ">> Created $(PKG)/KaTrain-$(VERSION)-linux-$(ARCH).tar.gz"
endif

build: sync katago app package ## Full build: sync + katago + app + package
	@echo ">> Build complete for $(PLATFORM)."

clean: ## Remove build artifacts
	rm -rf build $(DIST) $(PKG) dmg_temp osx_app

restore-katago: ## Restore tracked engine binaries removed by a macOS build
	git checkout -- katrain/KataGo
