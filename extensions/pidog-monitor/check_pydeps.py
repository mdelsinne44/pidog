#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vérifie les dépendances Python du secours IA (monitor_ai.py).

Sortie : une ligne par dépendance — OK / ABSENTE — puis un résumé.

* stdlib : la seule chose réellement requise pour les deux chemins
  (cloud requests + local llama-cli) ;
* requests : requis uniquement pour le chemin *cloud* ;
* transformers : requis uniquement pour le repli *transformers* (config.json),
  jamais pour le chemin GGUF/llama-cli.

Lancement :  python3 check_pydeps.py
"""

from importlib import util
import sys

PACKAGES = [
    ("requests", "path cloud (appels OpenRouter/Gemini/Groq)"),
    ("transformers", "repli offline via config.json (pas GGUF)"),
    ("torch", "support de transformers (pas GGUF)"),
    ("tokenizers", "tokenizer transformers (pas GGUF)"),
]

STDLIB = ["json", "glob", "os", "re", "shutil", "subprocess", "sys",
          "argparse", "tempfile", "time"]

missing = []


def check_stdlib():
    print("== stdlib (toujours requise) ==")
    ok = all(hasattr(sys, mod) or __import__(mod) and True for mod in STDLIB)
    # __import__ réel, plus fiable
    for mod in STDLIB:
        try:
            __import__(mod)
            print(f"  ok    {mod}")
        except ImportError:
            print(f"  ABSENT {mod}")
            missing.append(mod)


def check_packages():
    print("== paquets tiers ==")
    for name, why in PACKAGES:
        spec = util.find_spec(name.split(".")[0])
        if spec is None:
            print(f"  ABSENT {name}  ({why})")
            missing.append(name)
        else:
            try:
                mod = __import__(name)
                version = getattr(mod, "__version__", "?")
                print(f"  ok    {name} {version}  ({why})")
            except Exception as exc:
                print(f"  ?     {name} import car cassé ({exc})")
                missing.append(name)


if __name__ == "__main__":
    check_stdlib()
    check_packages()
    print()
    if missing:
        print(f"MANQUANT(S) : {', '.join(missing)}")
        sys.exit(1)
    print("Toutes les dépendances sont présentes.")
    sys.exit(0)
