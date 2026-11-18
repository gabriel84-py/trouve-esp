"""Regenere panneau/web_ui.h a partir de web/app.html et web/login.html.

Les pages sont compressees en gzip (le navigateur les decompresse) :
~4 fois moins de flash et un affichage plus rapide sur le Wi-Fi de l'ESP32.

Usage (depuis le dossier chambre/) :  python3 outils/generer_web_ui.py
"""
import gzip
from pathlib import Path

root = Path(__file__).resolve().parent.parent


def c_array(name, path):
    raw = path.read_bytes()
    gz = gzip.compress(raw, compresslevel=9, mtime=0)
    lines = []
    for i in range(0, len(gz), 20):
        lines.append("  " + ",".join("0x%02x" % b for b in gz[i:i + 20]) + ",")
    body = "\n".join(lines)
    return (f"// {path.name} : {len(raw)} octets, {len(gz)} octets en gzip\n"
            f"static const uint8_t {name}[] PROGMEM = {{\n{body}\n}};\n"
            f"static const size_t {name}_LEN = {len(gz)};\n"), len(raw), len(gz)


out = ["// Fichier genere a partir de web/app.html et web/login.html (gzip)",
       "// Modifie les fichiers HTML puis regenere ; ne pas editer a la main.",
       "#pragma once", "#include <Arduino.h>", ""]
for name, file in (("LOGIN_HTML_GZ", "login.html"), ("APP_HTML_GZ", "app.html")):
    txt, a, b = c_array(name, root / "web" / file)
    out.append(txt)
    print(f"{file:11s} {a:6d} -> {b:6d} octets")
(root / "panneau" / "web_ui.h").write_text("\n".join(out), encoding="ascii")
print("web_ui.h regenere")
