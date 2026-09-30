#!/usr/bin/env python3
"""
Shopify prospect scorer
-----------------------
Takes a list of store domains, pulls each store's public /products.json,
and scores how messy the catalog is. Bigger + messier = better cleanup prospect.

Usage:
    pip install requests
    python shopify_prospect_scorer.py domains.txt
    python shopify_prospect_scorer.py domains.txt --max-pages 10 --delay 2 --out ranked.csv
    python shopify_prospect_scorer.py --demo            # offline test on built-in messy sample

domains.txt: one domain per line (e.g. examplewelding.ca). Lines starting with # are ignored.
A .csv also works; the first column is used.

Notes:
- Only the public storefront endpoint is read, one request per page of 250 products,
  with a delay between requests. Keep the delay polite (default 1.5s).
- Some stores disable /products.json or aren't on Shopify; they are marked and skipped.
- Output CSV includes example duplicate pairs you can paste into outreach as proof.
"""
import argparse
import csv
import math
import re
import sys
import time
from collections import Counter, defaultdict

UA = "Mozilla/5.0 (compatible; CatalogAuditBot/1.0; +data cleanup research)"
MAX_BLOCK = 300  # cap comparisons per block to keep runtime sane

VOLT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:volts?|vdc|v)\b", re.I)
INCH_RE = re.compile(r"\d+(?:[./]\d+)?\s*(?:\"|\u201d|inch(?:es)?|in)(?![A-Za-z])", re.I)


# ---------------------------------------------------------------- fetching
def clean_domain(s):
    s = s.strip().lower()
    s = re.sub(r"^https?://", "", s)
    return s.split("/")[0]


def fetch_products(domain, max_pages, delay, timeout, session):
    """Return (products, status). status is 'ok' or a reason string."""
    products = []
    for page in range(1, max_pages + 1):
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try:
            r = session.get(url, timeout=timeout, headers={"User-Agent": UA})
        except Exception as e:  # network errors, DNS, SSL
            return (products or None), f"error: {type(e).__name__}"
        if r.status_code != 200:
            if page == 1:
                return None, f"http {r.status_code} (not Shopify or endpoint disabled)"
            break
        try:
            data = r.json()
        except ValueError:
            return (products or None), "not JSON (not Shopify)"
        if "products" not in data:
            return (products or None), "no products key (not Shopify)"
        batch = data["products"]
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:
            break
        time.sleep(delay)
    return products, "ok"


# ---------------------------------------------------------------- analysis
def norm_title(t):
    t = (t or "").lower()
    t = re.sub(r"(\d)\s*(?:volts?|vdc|v)\b", r"\1v", t)
    t = re.sub(r"(\d)\s*(?:\"|\u201d|inch(?:es)?|in)(?![a-z])", r"\1in", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_vendor(v):
    return re.sub(r"[^a-z0-9]", "", (v or "").lower())


def near_duplicates(products):
    """Return (set of product indexes involved in a duplicate pair, example pairs)."""
    blocks = defaultdict(list)
    for i, p in enumerate(products):
        nt = norm_title(p.get("title"))
        key = " ".join(nt.split()[:2])
        blocks[key].append((i, nt, p.get("title", "")))
    involved, examples = set(), []
    for items in blocks.values():
        if len(items) < 2:
            continue
        items = items[:MAX_BLOCK]
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                ia, na, ra = items[a]
                ib, nb, rb = items[b]
                # Only titles that are IDENTICAL after normalising case, punctuation, units
                # ("20 Volt"="20V", 1/2 inch = 1/2") count as duplicates. Fuzzy matching is
                # deliberately not used: it wrongly links colour/variant codes such as
                # 2236QTIMAX-R (red) vs -O (orange), which are different products.
                if na == nb:
                    involved.update((ia, ib))
                    if len(examples) < 3:
                        examples.append(f"{ra}  <->  {rb}")
    return involved, examples


def analyze(products):
    n = len(products)
    variants = [(p, v) for p in products for v in (p.get("variants") or [])]
    nv = len(variants)

    skus = [(v.get("sku") or "").strip() for _, v in variants]
    missing_sku = sum(1 for s in skus if not s)
    sku_counts = Counter(s.lower() for s in skus if s)
    dup_sku_rows = sum(c for c in sku_counts.values() if c > 1)

    missing_type = sum(1 for p in products if not (p.get("product_type") or "").strip())
    missing_vendor = sum(1 for p in products if not (p.get("vendor") or "").strip())
    short_desc = sum(
        1 for p in products
        if len(re.sub(r"<[^>]+>", "", p.get("body_html") or "").strip()) < 40
    )
    zero_price = sum(
        1 for _, v in variants
        if not v.get("price") or float(v.get("price") or 0) == 0
    )
    all_caps = sum(
        1 for p in products
        if (p.get("title") or "").isupper() and len(p.get("title", "")) > 6
    )

    # vendor spelling variants: "DeWalt" / "DEWALT" / "Dewalt "
    raw_vendors = {(p.get("vendor") or "").strip() for p in products if (p.get("vendor") or "").strip()}
    by_key = defaultdict(set)
    for v in raw_vendors:
        by_key[norm_vendor(v)].add(v)
    extra_vendor_spellings = sum(len(s) - 1 for s in by_key.values())
    vendor_variant_rate = extra_vendor_spellings / max(len(raw_vendors), 1)

    # unit formatting inconsistency inside titles
    titles = [p.get("title") or "" for p in products]
    volt_forms = Counter(re.sub(r"\d+", "#", m) for t in titles for m in VOLT_RE.findall(t))
    inch_forms = Counter(re.sub(r"\d+", "#", m) for t in titles for m in INCH_RE.findall(t))
    volt_incons = len(volt_forms) > 1
    inch_incons = len(inch_forms) > 1

    involved, examples = near_duplicates(products)
    dup_title_rate = len(involved) / max(n, 1)

    rate = lambda x, d: x / max(d, 1)
    issue_score = (
        min(rate(dup_sku_rows, nv) * 4, 1) * 15      # duplicate SKUs
        + rate(missing_sku, nv) * 15                 # missing SKUs
        + min(dup_title_rate * 4, 1) * 25            # near-duplicate products
        + rate(missing_type, n) * 10                 # no product type
        + min(vendor_variant_rate * 3, 1) * 10       # vendor spelling variants
        + (5 if volt_incons else 0) + (5 if inch_incons else 0)
        + rate(short_desc, n) * 10                   # thin/empty descriptions
        + rate(zero_price, nv) * 5                   # zero / missing prices
    )
    size_factor = min(1.0, math.log10(n + 1) / 3.5)  # ~3,000 products -> 1.0
    return {
        "products": n,
        "variants": nv,
        "missing_sku_pct": round(100 * rate(missing_sku, nv), 1),
        "duplicate_sku_rows": dup_sku_rows,
        "near_dup_products": len(involved),
        "near_dup_pct": round(100 * dup_title_rate, 1),
        "missing_type_pct": round(100 * rate(missing_type, n), 1),
        "missing_vendor_pct": round(100 * rate(missing_vendor, n), 1),
        "vendor_spelling_variants": extra_vendor_spellings,
        "volt_formats": len(volt_forms),
        "inch_formats": len(inch_forms),
        "short_desc_pct": round(100 * rate(short_desc, n), 1),
        "zero_price_pct": round(100 * rate(zero_price, nv), 1),
        "all_caps_titles": all_caps,
        "issue_score": round(issue_score, 1),
        "opportunity_score": round(issue_score * size_factor, 1),
        "examples": " || ".join(examples),
    }


# ---------------------------------------------------------------- demo data
def demo_products():
    prods = []

    def add(title, vendor, sku, ptype="", body="", price="99.00"):
        prods.append({
            "title": title, "vendor": vendor, "product_type": ptype, "body_html": body,
            "variants": [{"sku": sku, "price": price}],
        })

    add("DeWalt 20V MAX Cordless Drill 1/2\"", "DeWalt", "DCD771", "Drills", "<p>Compact 20V drill with 2 batteries and charger included.</p>")
    add("Dewalt 20 Volt Max Cordless Drill 1/2 inch", "DEWALT", "DCD771", "", "")
    add("DEWALT 20V MAX CORDLESS DRILL 1/2 IN", "Dewalt ", "", "Drills", "short")
    add("Milwaukee M18 Fuel Impact Driver", "Milwaukee", "2853-20", "Drivers", "<p>Brushless impact driver with 1/4 inch hex chuck, 2000 in-lbs torque.</p>")
    add("Milwaukee M18 FUEL Impact Driver 1/4in", "Milwaukee Tool", "2853-20", "Drivers", "<p>Brushless impact driver with 1/4 inch hex chuck, 2000 in-lbs torque.</p>")
    add("Lincoln Electric Power MIG 210 MP Welder", "Lincoln Electric", "K3963-1", "Welders", "<p>Multi-process welder, 120/230V input.</p>")
    add("Lincoln Electric Power MIG 210MP Welder 230 V", "Lincoln", "", "", "")
    add("Miller Multimatic 215", "Miller", "907693", "Welders", "<p>Multiprocess welder with Auto-Set, MIG TIG and Stick capability.</p>", "0.00")
    add("Brake Pad Set Front Ceramic", "Wagner", "", "", "")
    add("BRAKE PAD SET, FRONT, CERAMIC", "WAGNER", "", "", "")
    add("Brake Rotor Front 12 volt sensor kit", "ACDelco", "18A925A", "Brakes", "<p>Front rotor with sensor kit.</p>")
    add("Hex Bolt 3/8 in x 2 in Grade 8", "Grade8Co", "HB-38-2", "Fasteners", "<p>Zinc plated grade 8 hex bolt, box of 100 pieces.</p>")
    add("Hex Bolt 3/8\" x 2\" Gr 8", "Grade8Co", "HB-38-2", "Fasteners", "")
    for i in range(20):
        add(f"Cutting Wheel {4 + i // 2}\" x .045 Type {i % 2 + 1}", "Norton", f"CW{i}", "Abrasives", "<p>Reinforced thin cutting wheel for metal fabrication.</p>")
    return prods


# ---------------------------------------------------------------- main
def read_domains(path):
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip() or row[0].strip().startswith("#"):
                continue
            d = clean_domain(row[0])
            if d and d not in out:
                out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser(description="Score Shopify catalogs by cleanup potential")
    ap.add_argument("domains", nargs="?", help="text/csv file of store domains")
    ap.add_argument("--out", default="prospects_scored.csv")
    ap.add_argument("--max-pages", type=int, default=20, help="pages of 250 products per store")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--demo", action="store_true", help="offline test with built-in messy sample")
    args = ap.parse_args()

    rows = []
    if args.demo:
        m = analyze(demo_products())
        rows.append({"domain": "demo-tool-store.example", "status": "ok (demo)", **m})
    else:
        if not args.domains:
            ap.error("provide a domains file, or use --demo")
        try:
            import requests
        except ImportError:
            sys.exit("Missing dependency: pip install requests")
        session = requests.Session()
        domains = read_domains(args.domains)
        for i, d in enumerate(domains, 1):
            print(f"[{i}/{len(domains)}] {d} ...", end=" ", flush=True)
            products, status = fetch_products(d, args.max_pages, args.delay, args.timeout, session)
            if not products:
                print(status)
                rows.append({"domain": d, "status": status})
                continue
            m = analyze(products)
            print(f"{m['products']} products, opportunity {m['opportunity_score']}")
            rows.append({"domain": d, "status": "ok", **m})
            time.sleep(args.delay)

    scored = sorted((r for r in rows if "opportunity_score" in r),
                    key=lambda r: r["opportunity_score"], reverse=True)
    skipped = [r for r in rows if "opportunity_score" not in r]

    cols = ["domain", "status", "opportunity_score", "issue_score", "products", "variants",
            "near_dup_pct", "near_dup_products", "missing_sku_pct", "duplicate_sku_rows",
            "missing_type_pct", "missing_vendor_pct", "vendor_spelling_variants",
            "volt_formats", "inch_formats", "short_desc_pct", "zero_price_pct",
            "all_caps_titles", "examples"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(scored + skipped)

    print("\nRanked prospects (opportunity = mess score x catalog size):")
    print(f"{'domain':38}{'opp':>7}{'issues':>8}{'prods':>7}{'dup%':>7}{'noSKU%':>8}")
    for r in scored:
        print(f"{r['domain'][:37]:38}{r['opportunity_score']:>7}{r['issue_score']:>8}"
              f"{r['products']:>7}{r['near_dup_pct']:>7}{r['missing_sku_pct']:>8}")
    if skipped:
        print(f"\nSkipped {len(skipped)} (not Shopify / blocked / error) - see CSV status column.")
    print(f"\nSaved: {args.out}")
    if scored and scored[0].get("examples"):
        print("\nExample duplicates from top prospect (use in outreach):")
        for ex in scored[0]["examples"].split(" || "):
            print("  -", ex)


if __name__ == "__main__":
    main()
