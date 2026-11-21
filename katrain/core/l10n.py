"""Fork-local translation catalog, isolated from upstream katrain.po/mo files."""

import gettext
import os
import sys

from kivy._event import Observable

from katrain.core.lang import DEFAULT_LANGUAGE, i18n
from katrain.core.utils import find_package_resource

ENHANCED_DOMAIN = "katrain_enhanced"


class EnhancedLang(Observable):
    """Observable gettext wrapper synchronized with KaTrain's main ``i18n``."""

    def __init__(self, base_lang=i18n):
        super().__init__()
        self._base = base_lang
        self.observers = []
        self.lang = None
        self.font_name = base_lang.font_name
        self._catalog = gettext.NullTranslations()
        self._load(base_lang)
        base_lang.callbacks.append(self._load)

    def _(self, text):
        return self._catalog.gettext(text)

    def set_widget_font(self, widget):
        widget.font_name = self.font_name

    def fbind(self, name, func, *args):
        if name == "_":
            widget, _prop, *_ = args[0]
            self.observers.append((widget, func, args))
            try:
                self.set_widget_font(widget)
            except Exception as e:
                print(e)
        else:
            return super().fbind(name, func, *args)

    def funbind(self, name, func, *args):
        if name == "_":
            widget, *_ = args[0]
            key = (widget, func, args)
            if key in self.observers:
                self.observers.remove(key)
        else:
            return super().funbind(name, func, *args)

    def _load(self, base_lang):
        self.lang = base_lang.lang
        self.font_name = base_lang.font_name
        i18n_dir, _ = os.path.split(find_package_resource("katrain/i18n/__init__.py"))
        locale_dir = os.path.join(i18n_dir, "enhanced")
        self._catalog = gettext.translation(
            ENHANCED_DOMAIN,
            locale_dir,
            languages=[self.lang, DEFAULT_LANGUAGE],
            fallback=True,
        )
        for widget, func, args in self.observers:
            try:
                func(args[0], None, None)
                self.set_widget_font(widget)
            except ReferenceError:
                pass
            except Exception as e:
                print("Error in switching enhanced language", e, file=sys.stderr)


l10n = EnhancedLang()
