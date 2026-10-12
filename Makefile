# MetaMonitor build and install.
#
# Per-user install (default): make install
# System/packaging install:  make install PREFIX=/usr DESTDIR=debian/metamonitor
#
# SPDX-License-Identifier: Unlicense

PREFIX ?= $(HOME)/.local
DESTDIR ?=
BINDIR = $(PREFIX)/bin
LIBDIR = $(PREFIX)/share/metamonitor
APPDIR = $(PREFIX)/share/applications
MANDIR = $(PREFIX)/share/man/man1
METADIR = $(PREFIX)/share/metainfo
ICONDIR = $(PREFIX)/share/icons/hicolor
EXTUUID = metamonitor@yannchemin.github.io
EXTDIR = $(PREFIX)/share/gnome-shell/extensions/$(EXTUUID)
APP_ID = io.github.yannchemin.MetaMonitor
SHIMDIR = $(PREFIX)/lib/metamonitor
PYTHON ?= python3
CC ?= cc
CFLAGS ?= -O2 -g
CFLAGS += -Wall -Wextra -fPIC
SHIM = grass/libmmgrass.so

.PHONY: all test shell-test run install install-files hooks uninstall deb clean

all: $(SHIM)

$(SHIM): grass/mmgrass.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -shared -o $@ $< -ldl -lpthread

test: $(SHIM)
	$(PYTHON) -m pytest -q tests

# The top-bar extension in a throwaway headless gnome-shell (private D-Bus,
# temporary XDG dirs, no input method). Screenshot and results in OUT.
shell-test:
	tests/shell/run_nested_shell.sh $(OUT)

run:
	./bin/metamonitor

# A per-user install also turns on the shell and GRASS hooks (as the app does
# on first start); a staged/packaging install (DESTDIR set) only copies files.
install: install-files
ifeq ($(DESTDIR),)
	$(MAKE) hooks
endif

hooks:
	$(DESTDIR)$(BINDIR)/mmctl bash enable
	$(DESTDIR)$(BINDIR)/mmctl grass enable

install-files: $(SHIM)
	install -d $(DESTDIR)$(SHIMDIR)
	install -m 644 $(SHIM) $(DESTDIR)$(SHIMDIR)/
	install -d $(DESTDIR)$(LIBDIR)/metamonitor/collectors $(DESTDIR)$(LIBDIR)/metamonitor/ui \
		$(DESTDIR)$(LIBDIR)/bin $(DESTDIR)$(BINDIR) $(DESTDIR)$(APPDIR) \
		$(DESTDIR)$(MANDIR) $(DESTDIR)$(METADIR)
	install -m 644 metamonitor/*.py $(DESTDIR)$(LIBDIR)/metamonitor/
	install -m 644 metamonitor/collectors/*.py $(DESTDIR)$(LIBDIR)/metamonitor/collectors/
	install -m 644 metamonitor/ui/*.py metamonitor/ui/style.css $(DESTDIR)$(LIBDIR)/metamonitor/ui/
	install -m 755 bin/metamonitor bin/mmctl bin/mmrun $(DESTDIR)$(LIBDIR)/bin/
	for b in metamonitor mmctl mmrun; do \
		ln -sf $(LIBDIR)/bin/$$b $(DESTDIR)$(BINDIR)/$$b; done
	sed 's|@BINDIR@/||g' data/$(APP_ID).desktop > $(DESTDIR)$(APPDIR)/$(APP_ID).desktop
	install -m 644 data/$(APP_ID).metainfo.xml $(DESTDIR)$(METADIR)/
	install -m 644 data/metamonitor.1 data/mmctl.1 data/mmrun.1 $(DESTDIR)$(MANDIR)/
	install -d $(DESTDIR)$(LIBDIR)/data/fonts $(DESTDIR)$(LIBDIR)/data/icons/symbolic \
		$(DESTDIR)$(ICONDIR)/scalable/apps $(DESTDIR)$(ICONDIR)/symbolic/apps $(DESTDIR)$(EXTDIR)/icons
	install -m 644 data/fonts/*.ttf data/fonts/OFL-*.txt $(DESTDIR)$(LIBDIR)/data/fonts/
	install -m 644 data/icons/symbolic/*.svg $(DESTDIR)$(LIBDIR)/data/icons/symbolic/
	install -m 644 data/icons/hicolor/scalable/apps/$(APP_ID).svg $(DESTDIR)$(ICONDIR)/scalable/apps/
	install -m 644 data/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg $(DESTDIR)$(ICONDIR)/symbolic/apps/
	install -m 644 gnome-shell/$(EXTUUID)/metadata.json gnome-shell/$(EXTUUID)/extension.js \
		gnome-shell/$(EXTUUID)/stylesheet.css $(DESTDIR)$(EXTDIR)/
	install -m 644 gnome-shell/$(EXTUUID)/icons/*.svg $(DESTDIR)$(EXTDIR)/icons/
ifeq ($(DESTDIR),)
	-gtk-update-icon-cache -q -t $(ICONDIR) 2>/dev/null
endif

uninstall:
	-$(BINDIR)/mmctl bash disable
	-$(BINDIR)/mmctl grass disable
	rm -rf $(DESTDIR)$(EXTDIR)
	rm -f $(DESTDIR)$(ICONDIR)/scalable/apps/$(APP_ID).svg $(DESTDIR)$(ICONDIR)/symbolic/apps/$(APP_ID)-symbolic.svg
	rm -rf $(DESTDIR)$(LIBDIR) $(DESTDIR)$(SHIMDIR)
	rm -f $(DESTDIR)$(BINDIR)/metamonitor $(DESTDIR)$(BINDIR)/mmctl $(DESTDIR)$(BINDIR)/mmrun
	rm -f $(DESTDIR)$(APPDIR)/$(APP_ID).desktop $(DESTDIR)$(METADIR)/$(APP_ID).metainfo.xml
	rm -f $(DESTDIR)$(MANDIR)/metamonitor.1 $(DESTDIR)$(MANDIR)/mmctl.1 $(DESTDIR)$(MANDIR)/mmrun.1

# Binary package into dist/ (dpkg-buildpackage writes to the parent directory).
# "make clean" leaves dist/ alone.
deb:
	dpkg-buildpackage -us -uc -b
	mkdir -p dist
	mv ../metamonitor_*.deb ../metamonitor-dbgsym_*.*deb ../metamonitor_*.buildinfo ../metamonitor_*.changes dist/

clean:
	rm -f $(SHIM)
	find . -name __pycache__ -prune -exec rm -rf {} +
