"""Tests for the isolated fork localization catalog (katrain/core/l10n.py)."""

from katrain.core.l10n import l10n
from katrain.core.lang import i18n


def test_l10n_tracks_i18n_language_switches():
    original_lang = i18n.lang
    try:
        i18n.switch_lang("en")
        assert l10n.lang == "en"
        assert l10n._("Fox Weiqi") == "Fox Weiqi"
        assert l10n._("Local machine") == "Local machine"

        i18n.switch_lang("cn")
        assert l10n.lang == "cn"
        assert l10n._("Fox Weiqi") != "Fox Weiqi"
        assert l10n._("Local machine") != "Local machine"
    finally:
        i18n.switch_lang(original_lang)
