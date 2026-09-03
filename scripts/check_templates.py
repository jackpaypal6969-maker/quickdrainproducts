#!/usr/bin/env python
"""Parse every template with the real Jinja environment (custom filters
registered), so a deploy never ships a template that 500s on first render."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jinja2 import TemplateSyntaxError  # noqa: E402

from app.jinja_env import env  # noqa: E402


def main() -> int:
    bad = 0
    names = env.list_templates(extensions=["html", "txt"])
    for name in names:
        try:
            env.get_template(name)
        except TemplateSyntaxError as exc:
            bad += 1
            print(f"FAIL {name}:{exc.lineno}: {exc.message}")
    print(f"templates checked: {len(names)}, failures: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
