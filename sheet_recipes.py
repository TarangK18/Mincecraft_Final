"""Recipes from the owner's Google Sheet -> the station's recipes dict.

THIS VERSION: v1.0 (2026-10-27) — new; used by updater.py v1.1.
CHANGES (newest first)
  2026-10-27 v1.0  First version: one tab per recipe (the owner asked for a
                   tab per recipe instead of one Products/Ingredients table).

No network here: updater.py downloads the sheet (as .xlsx, one request, so
all tabs are from the same moment) and hands the bytes to read_sheet(). This
module only reads and checks them, so it can be tested with a file built in
the test itself.

One tab per recipe. The tab's name is the product name the station shows;
tab order is the order on the station. Each recipe tab:

    A          B
  1 ID         teriyaki_jerky      (leave as is; empty on a new recipe)
  2 Type       per kg meat         (or: fixed batch)
  3 Meat       Buffalo             (optional)
  4 Papain     yes                 (yes / no; per kg meat only)
  5 Active     yes                 (no = hidden from the station)
  6
  7 Ingredient Grams               (header row)
  8 soy sauce  46.4                (one row per ingredient, weighing order)
    ...

  Type "per kg meat" -> Grams per 1 kg of meat (stored as % of meat)
       "fixed batch" -> Grams for one batch, no meat
  Grams a number >= 0; 0 = listed but not weighed

Tabs named "How to use" or starting with "_" (e.g. "_Template") are not
recipes and are ignored. A recipe tab with a mistake is reported and
updater.py keeps that product's previous recipe; the other recipes are used.
A copied tab that still carries another recipe's ID is a mistake in the copy
only. The sheet is rejected as a whole only if it is not a spreadsheet at all
(not shared) or has no recipe tabs.
"""

import hashlib
import io
import json
import re
import unicodedata

PER_MEAT, FIXED = "per kg meat", "fixed batch"
LABELS = ("id", "type", "meat", "papain", "active")
SKIP_TABS = ("how to use",)
EXPORT_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=xlsx"


class SheetError(Exception):
    """The sheet as a whole cannot be used."""


def sheet_id(url):
    """The spreadsheet ID inside a Google Sheets link, or None."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", url or "")
    return m.group(1) if m else None


def export_url(url):
    """Download link for the whole sheet as .xlsx. A link that is not a
    Google Sheets link is used as it is (tests serve a file locally)."""
    sid = sheet_id(url)
    return EXPORT_URL.format(id=sid) if sid else url


def slug(name):
    """'Kerala fry Jerky' -> 'kerala_fry_jerky' — how a new product gets its
    ID when the ID cell is left empty."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _yes(v, default):
    t = _text(v).lower()
    if not t:
        return default
    if t in ("yes", "y", "true", "1"):
        return True
    if t in ("no", "n", "false", "0"):
        return False
    return None                                   # neither: a mistake


def _number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = _text(v).replace(" ", "")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _recipe_tab(ws):
    """(fields {label: value}, ingredient rows [(row, name, grams)], problem)."""
    fields, rows, header_at = {}, [], None
    for n, row in enumerate(ws.iter_rows(values_only=True), start=1):
        a = row[0] if len(row) > 0 else None
        b = row[1] if len(row) > 1 else None
        if header_at is None:
            label = _text(a).lower()
            if label == "ingredient" and _text(b).lower().startswith("gram"):
                header_at = n
            elif label in LABELS:
                fields[label] = b
            continue
        if _text(a) or _text(b):
            rows.append((n, a, b))
    if header_at is None:
        return fields, rows, "no 'Ingredient | Grams' header row"
    return fields, rows, None


def read_sheet(xlsx_bytes):
    """Parse the downloaded sheet.

    Returns a dict:
      products  [{id, name, meat, ingredients, (batch)}] in tab order,
                only active recipes without problems
      papain    [ids] of those products with Papain = yes
      problems  {product name: [problems]} for active recipes not taken
      ids       {product name: id} for every active recipe (taken or not)
      inactive  [names]
    Raises SheetError if the sheet as a whole cannot be used.
    """
    import openpyxl
    if not xlsx_bytes[:2] == b"PK":
        raise SheetError("the download is not a spreadsheet — the sheet is "
                         "probably not shared as 'Anyone with the link: Viewer'")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True,
                                    read_only=True)
    except Exception as e:
        raise SheetError(f"the download could not be opened ({type(e).__name__})")

    out, papain_ids, problems, ids, inactive = [], [], {}, {}, []
    id_owner = {}
    recipe_tabs = 0
    for title in wb.sheetnames:
        name = title.strip()
        if not name or name.lower() in SKIP_TABS or name.startswith("_"):
            continue
        recipe_tabs += 1
        fields, rows, bad = _recipe_tab(wb[title])
        probs = [bad] if bad else []

        active = _yes(fields.get("active"), True)
        if active is None:
            probs.append(f"Active is '{_text(fields.get('active'))}', not yes or no")
        elif not active:
            inactive.append(name)
            continue

        pid = _text(fields.get("id")) or slug(name)
        own_id = pid
        if not re.fullmatch(r"[a-z0-9_]+", pid):
            probs.append(f"ID '{pid}' may only use a-z, 0-9 and _")
        elif pid in id_owner:
            probs.append(f"ID '{pid}' is already used by the '{id_owner[pid]}' tab — "
                         f"on a copied recipe, empty the ID cell")
            own_id = None             # not this tab's: nothing of its own to keep
        else:
            id_owner[pid] = name

        kind = _text(fields.get("type")).lower()
        if kind in ("per kg meat", "per kg", "per kg of meat", "meat"):
            fixed = False
        elif kind in ("fixed batch", "fixed"):
            fixed = True
        else:
            fixed = False
            probs.append(f"Type is '{_text(fields.get('type'))}', not "
                         f"'{PER_MEAT}' or '{FIXED}'" if kind else "Type is empty")
        papain = _yes(fields.get("papain"), False)
        if papain is None:
            probs.append(f"Papain is '{_text(fields.get('papain'))}', not yes or no")
            papain = False
        if papain and fixed:
            probs.append("Papain is 'yes' on a fixed batch (no meat to base it on)")

        ingredients, seen = [], set()
        for n, a, b in rows:
            ing, grams = _text(a), _number(b)
            if not ing:
                probs.append(f"row {n} has grams but no ingredient name")
                continue
            if grams is None:
                raw = _text(b)
                probs.append(f"'{ing}' (row {n}): grams "
                             + (f"'{raw}' is not a number" if raw else "is empty"))
                continue
            if grams < 0:
                probs.append(f"'{ing}' (row {n}): grams is negative")
                continue
            if ing.lower() in seen:
                probs.append(f"'{ing}' is listed twice")
                continue
            seen.add(ing.lower())
            ingredients.append([ing, grams if fixed else round(grams / 10.0, 4)])
        if not probs and not ingredients:
            probs.append("no ingredients under the 'Ingredient | Grams' row")
        elif not probs and not any(q > 0 for _, q in ingredients):
            probs.append("every ingredient is 0 g")

        ids[name] = own_id
        if probs:
            problems[name] = probs
            continue
        prod = {"id": pid, "name": name, "meat": _text(fields.get("meat")) or None,
                "ingredients": ingredients}
        if fixed:
            prod["batch"] = "fixed"
        out.append(prod)
        if papain:
            papain_ids.append(pid)
    if recipe_tabs == 0:
        raise SheetError("the sheet has no recipe tabs")
    if not out and not problems:
        raise SheetError("every recipe tab is set to Active: no")
    return {"products": out, "papain": papain_ids, "problems": problems,
            "ids": ids, "inactive": inactive}


def merge(base, previous, parsed):
    """The recipes dict to use: settings from `base` (the repo's recipes.json:
    scales, tolerances, PIN, papain rates), products from the sheet. A product
    with a problem keeps its version from `previous` (the last recipes the
    station used) if it had one, else it is left out. Returns (cfg, kept, dropped)
    where kept/dropped list the problem products by name."""
    cfg = json.loads(json.dumps(base))
    prev_products = {p["id"]: p for p in (previous or {}).get("products", [])}
    prev_papain = set(((previous or {}).get("papain") or {}).get("applies_to") or [])
    good = {p["name"]: p for p in parsed["products"]}
    papain = set(parsed["papain"])
    products, kept, dropped, used = [], [], [], set()
    for name, pid in parsed["ids"].items():
        if name in good and good[name]["id"] not in used:
            products.append(good[name])
            used.add(good[name]["id"])
        elif pid and pid not in used and pid in prev_products:
            used.add(pid)
            products.append(prev_products[pid])
            kept.append(name)
            if pid in prev_papain:
                papain.add(pid)
        else:
            dropped.append(name)
    cfg["products"] = products
    if "papain" in cfg:
        order = [p["id"] for p in products]
        cfg["papain"]["applies_to"] = [i for i in order if i in papain]
    return cfg, kept, dropped


def digest(cfg):
    """Stable id of the recipes that matter (everything but the sync note)."""
    c = {k: v for k, v in cfg.items() if k != "_sync"}
    blob = json.dumps(c, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
