#!/usr/bin/env python
"""Parse every template with the real Jinja environment (custom filters
registered), so a deploy never ships a template that 500s on first render."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import re  # noqa: E402

from jinja2 import TemplateSyntaxError, meta  # noqa: E402

from app.jinja_env import env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    bad = 0
    names = env.list_templates(extensions=["html", "txt"])
    known = set(names)
    for name in names:
        try:
            env.get_template(name)
            source = env.loader.get_source(env, name)[0]
            for ref in meta.find_referenced_templates(env.parse(source)):
                if ref is not None and ref not in known:
                    bad += 1
                    print(f"FAIL {name}: references missing template {ref}")
        except TemplateSyntaxError as exc:
            bad += 1
            print(f"FAIL {name}:{exc.lineno}: {exc.message}")
    # every template a route renders must exist too
    referenced = set()
    for py in (ROOT / "app").rglob("*.py"):
        referenced.update(re.findall(r'render\(\s*request,\s*"([^"]+\.html)"', py.read_text(encoding="utf-8")))
        referenced.update(re.findall(r'arender\(\s*request,\s*"([^"]+\.html)"', py.read_text(encoding="utf-8")))
        referenced.update("emails/" + t + ".html" for t in re.findall(r'emails\.send\([^"]*"[^"]*",\s*"([a-z_]+)"', py.read_text(encoding="utf-8")))
    for ref in sorted(referenced):
        if ref not in known:
            bad += 1
            print(f"FAIL route references missing template {ref}")
    print(f"templates checked: {len(names)} (+{len(referenced)} route references), failures: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
