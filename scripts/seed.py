#!/usr/bin/env python
"""Seed the catalog with Quick Shot. Idempotent: re-running updates label facts
and never touches prices or stock that an admin has already changed.

Everything below about the product comes from the label photo:
  DRAIN MAINTAINER / QUICK SHOT / NATURAL DRAIN ENZYME /
  DOSED FOR MONTHLY USE ON ANY DRAIN / NET CONTENTS 4 FL OZ (118 mL)

Prices are placeholders (the intake block left them blank) and are flagged as
such in the admin until edited.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect, migrate, one, set_setting, transaction  # noqa: E402

LABEL_CLAIMS = [
    "Drain maintainer",
    "Natural drain enzyme",
    "Dosed for monthly use on any drain",
    "Net contents 4 fl oz (118 mL)",
]

SPECS = [
    ("Product type", "Drain maintainer (label)"),
    ("Formulation", "Natural drain enzyme (label)"),
    ("Net contents", "4 fl oz (118 mL) per bottle (label)"),
    ("Dosing", "Dosed for monthly use on any drain — one bottle is one monthly dose (label)"),
    ("Active ingredients", "Not printed on the front label. Added from the SDS when uploaded."),
    ("Shipping class", "Ordinary parcel. Enzymatic per the label; confirmed against the safety data sheet once it is on file."),
    ("Sold by", "Quick Drain, Long Island, NY"),
]

FAQS = [
    ("Is one bottle one dose?", "Yes. The label reads “dosed for monthly use on any drain” and the bottle holds 4 fl oz (118 mL). Use one bottle per drain, once a month."),
    ("What is in it?", "The label describes it as a natural drain enzyme and does not list ingredients. When the safety data sheet is on file we publish it here unchanged; until then we do not list ingredients we cannot show you."),
    ("Will it clear a drain that is already backed up?", "It is a maintainer, not an emergency product. If water is standing or backing up right now, that needs a diagnosis — a camera inspection tells you whether it is grease, roots, scale or a broken line. Book that with Quick Drain and use Quick Shot afterwards to keep the line clean."),
    ("Is it safe for septic systems and cesspools?", "The label says “any drain” and does not make a specific septic claim. Until the SDS is published here we will not claim more than the label does. If you are on a cesspool, ask us before use."),
    ("How does it ship?", "As an ordinary parcel at a flat rate, free from the threshold shown in the cart. Enzymatic products are not classed as corrosives for shipping; the safety data sheet, once published here, is the document that confirms the classification."),
    ("How do the packs work?", "A 3-pack is three monthly doses for one drain, or one month for three drains. A 6-pack is six. The coverage table above does the arithmetic for you."),
]

POSTS = [
    {
        "slug": "clearing-vs-cleaning-and-where-a-monthly-dose-fits",
        "title": "Clearing a drain is not cleaning it — and where a monthly dose fits",
        "excerpt": "Punching a hole through a clog and restoring the pipe wall are different jobs. A monthly dose belongs after the second one.",
        "body": (
            "Quick Drain's service pages separate two jobs: **drain clearing** punches a hole through a blockage, while **drain cleaning** — high pressure water jetting — restores the full pipe wall and original diameter.\n\n"
            "That distinction matters for a maintenance product. A monthly enzyme dose is a way to keep a *clean* line clean. It is not a substitute for jetting a line that is already coated, and it will not fix roots, offsets, bellies or collapsed pipe.\n\n"
            "## The honest sequence\n\n"
            "1. If a drain is backing up, get it diagnosed. A camera inspection with 512 Hz locating shows what is actually in the line.\n"
            "2. If the line is dirty, have it jetted wall-to-wall.\n"
            "3. Then start the monthly dose as the label directs.\n\n"
            "[Read about camera inspections and jetting on quickdrainny.com](https://www.quickdrainny.com/)"
        ),
        "seo_description": "Opening a clog and cleaning the pipe wall are different jobs. Where a monthly enzyme dose fits in that sequence.",
    },
    {
        "slug": "long-island-conditions-cast-iron-groundwater-and-roots",
        "title": "Long Island conditions: cast iron, high groundwater and roots",
        "excerpt": "Why the same drain behaves differently in Suffolk County than it does in a newer subdivision inland.",
        "body": (
            "Quick Drain's field work is shaped by local conditions the parent site calls out directly: high groundwater, older cast iron, Orangeburg pipe, sandy soil, roots and heavy rainfall.\n\n"
            "Each one changes what maintenance can and cannot do:\n\n"
            "- **Older cast iron** scales and tuberculates from the inside. Descaling is a mechanical job; once a line is clean, the label's monthly dose is the maintenance step.\n"
            "- **Roots** are a structural problem. No bottle removes a root mass. Root cutting and a camera follow-up are the fix.\n"
            "- **High groundwater and cesspools** raise the stakes on what goes down the drain. Ask before using any additive on a cesspool.\n\n"
            "If you are buying a home on Long Island, the parent site's sewer and septic inspection pages explain what to check before closing.\n\n"
            "[See Quick Drain's inspection services](https://www.quickdrainny.com/)"
        ),
        "seo_description": "How Long Island's cast iron, groundwater and root conditions change what drain maintenance can do.",
    },
]


def main() -> None:
    migrate()
    conn = connect()
    try:
        with transaction(conn):
            coll = one(conn, "SELECT id FROM collections WHERE slug = 'drain-maintenance'")
            if not coll:
                conn.execute("INSERT INTO collections(slug, name, description, sort) VALUES ('drain-maintenance', 'Drain maintenance', 'Products that keep a clean line clean between service visits.', 0)")
                coll = one(conn, "SELECT id FROM collections WHERE slug = 'drain-maintenance'")

            product = one(conn, "SELECT id FROM products WHERE slug = 'quick-shot'")
            label_fields = {
                "name": "Quick Shot",
                "tagline": "A natural drain enzyme, dosed for monthly use on any drain.",
                "description": (
                    "Quick Shot is the maintenance step between service visits. The label reads “dosed for monthly use on any drain”, 4 fl oz per bottle. We read that as one bottle, one drain, one month — and this page will not say more than the label does.\n\n"
                    "It comes from Quick Drain, the Long Island sewer and drain company that diagnoses before it quotes. The same posture applies here: if your drain is backing up today, that is a diagnostic visit, not a bottle."
                ),
                "formulation_type": "enzymatic",
                "hazmat": 0,
                "active_ingredients": "",
                "net_volume_oz": 4.0,
                "net_volume_ml": 118.0,
                "dose_text": "Dosed for monthly use on any drain",
                "dose_interval_days": 30,
                "drains_per_unit": 1,
                "directions": "",
                "safe_for": "",
                "not_safe_for": "",
                "label_claims": json.dumps(LABEL_CLAIMS),
                "weight_oz": 5.5,
                "collection_id": coll["id"],
                "is_featured": 1,
                "seo_title": "Quick Shot — natural drain enzyme, monthly dose | Quick Drain Products",
                "seo_description": "Quick Shot is a natural drain enzyme dosed for monthly use on any drain. 4 fl oz per bottle. Ships as an ordinary parcel. From Quick Drain, Long Island.",
            }
            if product:
                sets = ", ".join(f"{k} = ?" for k in label_fields)
                conn.execute(f"UPDATE products SET {sets}, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?", (*label_fields.values(), product["id"]))
                pid = product["id"]
            else:
                cols = ", ".join(["slug", *label_fields])
                marks = ", ".join("?" for _ in range(len(label_fields) + 1))
                conn.execute(f"INSERT INTO products ({cols}) VALUES ({marks})", ("quick-shot", *label_fields.values()))
                pid = one(conn, "SELECT id FROM products WHERE slug = 'quick-shot'")["id"]

            variants = [
                ("QS-1", "Single bottle", 1, 1600, None, 120, 0),
                ("QS-3", "3-pack", 3, 4200, 4800, 60, 1),
                ("QS-6", "6-pack", 6, 7800, 9600, 40, 2),
            ]
            for sku, name, units, price, compare, stock, sort in variants:
                if not one(conn, "SELECT id FROM variants WHERE sku = ?", (sku,)):
                    conn.execute("INSERT INTO variants(product_id, sku, name, units_per_pack, price_cents, compare_at_cents, stock, sort) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (pid, sku, name, units, price, compare, stock, sort))
                    conn.execute("INSERT INTO inventory_movements(variant_id, delta, reason, note) VALUES ((SELECT id FROM variants WHERE sku = ?), ?, 'restock', 'seed')", (sku, stock))

            conn.execute("DELETE FROM product_specs WHERE product_id = ?", (pid,))
            for i, (label, value) in enumerate(SPECS):
                conn.execute("INSERT INTO product_specs(product_id, label, value, sort) VALUES (?, ?, ?, ?)", (pid, label, value, i))
            conn.execute("DELETE FROM product_faqs WHERE product_id = ?", (pid,))
            for i, (q, a) in enumerate(FAQS):
                conn.execute("INSERT INTO product_faqs(product_id, question, answer, sort) VALUES (?, ?, ?, ?)", (pid, q, a, i))

            images = [
                ("quick-shot-hero", "Quick Shot 4 fl oz bottle on a dark surface", 1200, 1500, "hero", 0),
                ("quick-shot-label", "Quick Shot label: natural drain enzyme, dosed for monthly use on any drain", 1200, 1500, "gallery", 1),
                ("quick-shot-counter", "Quick Shot bottle beside a kitchen sink", 1200, 1500, "gallery", 2),
            ]
            for base, alt, w, h, kind, sort in images:
                if not one(conn, "SELECT id FROM product_images WHERE product_id = ? AND base = ?", (pid, base)):
                    conn.execute("INSERT INTO product_images(product_id, base, source, alt, width, height, kind, sort) VALUES (?, ?, 'static', ?, ?, ?, ?, ?)", (pid, base, alt, w, h, kind, sort))

            for post in POSTS:
                if not one(conn, "SELECT id FROM posts WHERE slug = ?", (post["slug"],)):
                    conn.execute(
                        "INSERT INTO posts(slug, title, excerpt, body, status, published_at, seo_description) VALUES (?, ?, ?, ?, 'published', strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?)",
                        (post["slug"], post["title"], post["excerpt"], post["body"], post["seo_description"]),
                    )

            for slug, title in (("terms", "Terms of sale"), ("privacy", "Privacy policy"), ("refunds", "Refunds & returns"), ("shipping", "Shipping policy"), ("accessibility", "Accessibility statement")):
                conn.execute("INSERT OR IGNORE INTO pages(slug, title, body) VALUES (?, ?, '')", (slug, title))

            if not one(conn, "SELECT 1 FROM settings WHERE key = 'prices_are_placeholders'"):
                set_setting(conn, "prices_are_placeholders", "1")
        print("seeded: product quick-shot with", one(conn, "SELECT COUNT(*) AS n FROM variants")["n"], "variants")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
