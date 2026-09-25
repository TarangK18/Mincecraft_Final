#!/usr/bin/env python3
"""Turn DOKI-Recipes.xlsx into the recipes.json the station reads.

  python3 xlsx_to_recipes.py DOKI-Recipes.xlsx [recipes.json]

Meat recipes: the workbook holds grams per 1 kg of meat; recipes.json holds a
percentage of the base weight. 73 g/kg is 7.3 %.

Fixed batches (C3 = "Fixed batch"): the workbook holds grams per batch, and so
does recipes.json, marked "batch": "fixed". No conversion — the numbers are
the batch.

Refuses to write a recipe that still has an unweighable ingredient or an
untouched example row, so a sheet nobody has finished cannot quietly reach the
floor. Existing bases, scales, tolerances and the PIN are left alone.
"""

import argparse
import json
import os
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
FIRST_ROW, LAST_ROW = 9, 48
SKIP = {"Instructions", "Settings", "Summary", "Ingredients"}
EXAMPLE = ("Salt", 7.5)      # the grey row seeded into each blank sheet


def read_sheet(ws):
    """(product_dict, [problems]) for one recipe sheet."""
    problems = []
    # Row 3 says what the weights are measured against. A sheet from before
    # row 3 existed has nothing there, and was always per kg of meat.
    fixed = str(ws["C3"].value or "").strip().lower().startswith("fixed")
    product_id = (ws["C5"].value or "").strip()
    bases = [b.strip() for b in str(ws["F5"].value or "").split(",")
             if b.strip() and b.strip() != "—"]
    if not product_id:
        problems.append("no Product ID in C5")
    if not bases and not fixed:
        problems.append("no bases listed in F5")

    meat = None if fixed else ((ws["C7"].value or "").strip() or None)
    flour = "" if fixed else (ws["C6"].value or "").strip()
    water = "" if fixed else (ws["F6"].value or "").strip()
    if bool(flour) != bool(water):
        problems.append("names a flour or a water ingredient but not both — "
                        "water cannot be derived from the daily ratio")

    ingredients, seen = [], set()
    for r in range(FIRST_ROW, LAST_ROW + 1):
        name = ws.cell(row=r, column=2).value
        if name is None or not str(name).strip():
            continue
        name = str(name).strip()
        grams = ws.cell(row=r, column=3).value
        where = ws.cell(row=r, column=6).value or ""

        if grams is None:
            problems.append(f"row {r}: '{name}' has no weight")
            continue
        try:
            grams = float(grams)
        except (TypeError, ValueError):
            problems.append(f"row {r}: '{name}' weight {grams!r} is not a number")
            continue
        if grams < 0:
            problems.append(f"row {r}: '{name}' has a negative weight")
            continue
        if name.lower() in seen:
            problems.append(f"row {r}: '{name}' appears twice")
            continue
        seen.add(name.lower())
        if grams == 0:
            # Listed for reference, not weighed — the station shows it under
            # "listed but not weighed". Checked before the Weigh-on column,
            # which in a workbook saved before this fix still says
            # "NEITHER — zero" and used to get the whole recipe refused.
            ingredients.append([name, 0.0])
            continue
        if str(where).startswith("NEITHER"):
            problems.append(f"row {r}: '{name}' — {where}")
            continue
        # g per 1 kg of meat -> % of meat is /10; a fixed batch is already grams.
        qty = grams if fixed else round(grams / 10.0, 4)
        ingredients.append([name, qty])

    if not any(q > 0 for _, q in ingredients):
        problems.append("no ingredients filled in")
    elif len(ingredients) == 1 and ingredients[0][0] == EXAMPLE[0] \
            and abs(ingredients[0][1] - EXAMPLE[1] / 10.0) < 1e-9:
        problems.append("still holds only the grey example row")

    names = {n for n, _ in ingredients}
    for role, ing in (("flour", flour), ("water", water)):
        if ing and ing not in names:
            problems.append(f"{role} ingredient '{ing}' is not in the "
                            f"ingredient list")

    product = {"id": product_id, "name": ws.title, "meat": meat,
               "ingredients": ingredients}
    if fixed:
        product["batch"] = "fixed"
    if flour and water:
        product["flour_ingredient"] = flour
        product["water_ingredient"] = water
    return product, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", nargs="?",
                    default=os.path.join(HERE, "DOKI-Recipes.xlsx"))
    ap.add_argument("recipes", nargs="?",
                    default=os.path.join(HERE, "recipes.json"))
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args(argv)

    wb = openpyxl.load_workbook(args.workbook, data_only=True)
    with open(args.recipes, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    ready, skipped = [], []
    for title in wb.sheetnames:
        if title in SKIP:
            continue
        product, problems = read_sheet(wb[title])
        if problems:
            skipped.append((title, problems))
        else:
            ready.append(product)

    for title, problems in skipped:
        print(f"skipped {title}:")
        for p in problems:
            print(f"    {p}")

    if not ready:
        print("\nNothing to write — no sheet is complete.")
        return 1

    by_id = {p["id"]: p for p in cfg.get("products", [])}
    added, updated = [], []
    for p in ready:
        (updated if p["id"] in by_id else added).append(p["name"])
        by_id[p["id"]] = p
    cfg["products"] = list(by_id.values())

    print(f"\nready: {len(ready)} recipe(s)")
    for p in ready:
        total = sum(q for _, q in p["ingredients"])
        if p.get("batch") == "fixed":
            print(f"    {p['name']:<20} {len(p['ingredients']):>2} ingredients, "
                  f"fixed batch of {total:,.0f} g, no meat")
            continue
        gate = (f"  water = ratio x {p['flour_ingredient']}"
                if p.get("flour_ingredient") else "  not water-gated")
        print(f"    {p['name']:<20} {len(p['ingredients']):>2} ingredients, "
              f"{total:.3f} % of base ({total * 10:.2f} g per kg of meat)"
              f"{gate}")
    if added:
        print(f"  added:   {', '.join(added)}")
    if updated:
        print(f"  updated: {', '.join(updated)}")

    if args.dry_run:
        print("\n--dry-run: recipes.json not written")
        return 0

    with open(args.recipes, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\nwrote {args.recipes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
