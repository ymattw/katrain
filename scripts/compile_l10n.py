"""Validate and compile fork-local translations under katrain/i18n/enhanced."""

import glob
import os
import re
import sys
from collections import defaultdict

import polib

LOCALE_DIR = "katrain/i18n/enhanced"
DOMAIN = "katrain_enhanced"
DEFAULT_LANG = "en"


def main() -> int:
    if not os.path.isdir(LOCALE_DIR):
        print(f"No enhanced locale directory found at {LOCALE_DIR}")
        return 0

    locales = sorted(d for d in os.listdir(LOCALE_DIR) if os.path.isdir(os.path.join(LOCALE_DIR, d)))
    errors = 0
    strings_to_langs = defaultdict(dict)
    strings_to_keys = defaultdict(dict)
    lang_to_strings = defaultdict(set)
    po = {}
    pofile = {}

    for lang in locales:
        pofile[lang] = os.path.join(LOCALE_DIR, lang, "LC_MESSAGES", f"{DOMAIN}.po")
        po[lang] = polib.pofile(pofile[lang])
        for entry in po[lang].translated_entries():
            strings_to_langs[entry.msgid][lang] = entry
            strings_to_keys[entry.msgid][lang] = set(re.findall(r"\{.*?\}", entry.msgstr))
            if entry.msgid in lang_to_strings[lang]:
                print(f"duplicate {entry.msgid!r} in {lang}")
                errors += 1
            else:
                lang_to_strings[lang].add(entry.msgid)

    for lang in locales:
        if lang != DEFAULT_LANG:
            for msgid in lang_to_strings[lang]:
                if (
                    DEFAULT_LANG in strings_to_keys[msgid]
                    and strings_to_keys[msgid][lang] != strings_to_keys[msgid][DEFAULT_LANG]
                ):
                    print(
                        f"{msgid!r} has inconsistent format keys for {lang}: "
                        f"{strings_to_keys[msgid][lang]} != {strings_to_keys[msgid][DEFAULT_LANG]}"
                    )
                    errors += 1
        missing = strings_to_langs.keys() - lang_to_strings[lang]
        if missing:
            print(f"Missing {len(missing)} entries in {lang}: {sorted(missing)}")
            errors += 1

        mofile = pofile[lang].replace(".po", ".mo")
        po[lang].save_as_mofile(mofile)

    for ext in ("py", "kv"):
        files = sorted(set(glob.glob(f"katrain/*.{ext}") + glob.glob(f"katrain/**/*.{ext}", recursive=True)))
        for path in files:
            with open(path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    for raw in re.findall(r"\bl10n\._\((.*?)\)", line):
                        arg = raw.strip()
                        if arg and arg[0] in ("'", '"'):
                            msgid = arg.strip("\"'")
                            if msgid and msgid not in strings_to_langs:
                                print(f"Missing l10n key {msgid!r} at {path}:{lineno}")
                                errors += 1

    print(f"Checked {len(locales)} enhanced locales ({len(strings_to_langs)} keys), errors={errors}.")
    return int(errors > 0)


if __name__ == "__main__":
    sys.exit(main())
