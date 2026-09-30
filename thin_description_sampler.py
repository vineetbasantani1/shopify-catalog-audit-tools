#!/usr/bin/env python3
"""
Thin-description sampler
------------------------
For one Shopify store: finds products whose description is missing or very short,
picks a random sample, and writes two files:

  1. <domain>_spotcheck.txt  - product page URLs to open by hand and confirm the
                               gap is real (some stores keep specs elsewhere on the page).
  2. <domain>_sample.csv     - a work sheet for the free sample offer. Each row has the
                               product's current data plus attributes extracted from the
                               TITLE (brand, model code, voltage, drive size, mm size, torque),
                               and EMPTY columns for the new description and where the specs
                               came from. Fill those from the manufacturer's own spec sheet.

It does NOT write descriptions for you. Copy specs only from manufacturer-provided
material, never from another retailer's product page.

Usage:
    pip install requests
    python thin_description_sampler.py mprtools.com
    python thin_description_sampler.py mprtools.com --sample 40 --max-pages 60 --min-chars 40
    python thin_description_sampler.py --demo
"""
import argparse
import csv
import random
import re
import sys
import time

UA = "Mozilla/5.0 (compatible; CatalogAuditBot/1.0; +data cleanup research)"

VOLT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:v|volts?)\b", re.I)
DRIVE = re.compile(r"(\d+/\d+|\d+(?:\.\d+)?)\s*(?:\"|”|in\b|inch(?:es)?)\s*(?:sq\.?\s*)?(?:dr|drive)\b", re.I)
MM = re.compile(r"(\d+(?:\.\d+)?)\s*mm\b", re.I)
TORQUE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(ft[-\s]?lbs?|in[-\s]?lbs?|nm)\b", re.I)
RPM = re.compile(r"(\d[\d,]*)\s*rpm\b", re.I)
MODEL = re.compile(r"\b(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{3,}\b")


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def extract_attrs(p):
    title = p.get("title") or ""
    models = MODEL.findall(title.upper())
    first = lambda rx: (rx.search(title).group(0).strip() if rx.search(title) else "")
    return {
        "brand": (p.get("vendor") or "").strip(),
        "model_code_guess": models[0] if models else "",
        "voltage": first(VOLT),
        "drive_size": first(DRIVE),
        "size_mm": first(MM),
        "torque": first(TORQUE),
        "rpm": first(RPM),
    }


def fetch_products(domain, max_pages, delay, timeout, scheme="https"):
    import requests
    products = []
    for page in range(1, max_pages + 1):
        url = f"{scheme}://{domain}/products.json?limit=250&page={page}"
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        if r.status_code != 200:
            if page == 1:
                sys.exit(f"{domain}: HTTP {r.status_code} - not Shopify or products.json is disabled.")
            break
        batch = r.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        print(f"  page {page}: {len(products)} products so far", flush=True)
        if len(batch) < 250:
            break
        time.sleep(delay)
    return products


def demo_products():
    ps = []
    for i in range(60):
        thin = i % 3 != 0
        ps.append({
            "title": f"VIM Tools XZN{600 + i}H Triple Square Impact Driver, 3/8\" Sq. Dr., {6 + i % 4} mm" if i % 2 else
                     f"Ingersoll Rand 2236QTIMAX-R 1/2\" Dr. Impact Wrench, 1500 Ft-lb, 7500 RPM, 20V",
            "vendor": "VIM Tools" if i % 2 else "Ingersoll Rand",
            "handle": f"item-{i}", "product_type": "Impact",
            "body_html": "" if thin and i % 2 else ("<p>See specs.</p>" if thin else "<p>" + "Long detailed description. " * 6 + "</p>"),
            "tags": ["impact"], "images": [{"src": f"https://cdn.example/{i}.jpg"}],
            "variants": [{"sku": f"SKU{i}", "price": "49.99"}],
        })
    return ps


def main():
    ap = argparse.ArgumentParser(description="Sample products with thin descriptions")
    ap.add_argument("domain", nargs="?")
    ap.add_argument("--sample", type=int, default=30, help="products in the work sheet")
    ap.add_argument("--spot", type=int, default=10, help="URLs in the spot-check list")
    ap.add_argument("--min-chars", type=int, default=40, help="text length below which a description is 'thin'")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7, help="fixed seed so the same sample can be re-created")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--demo", action="store_true", help="offline test with built-in sample data")
    args = ap.parse_args()

    if args.demo:
        domain, products = "demo-store.example", demo_products()
    else:
        if not args.domain:
            ap.error("give a store domain, or use --demo")
        domain = re.sub(r"^https?://", "", args.domain.strip().lower()).split("/")[0]
        print(f"Fetching {domain} ...")
        products = fetch_products(domain, args.max_pages, args.delay, args.timeout)

    total = len(products)
    thin = [p for p in products if len(strip_html(p.get("body_html"))) < args.min_chars]
    pct = 100 * len(thin) / max(total, 1)
    print(f"\n{domain}: {len(thin)} of {total} products have a description under {args.min_chars} characters ({pct:.1f}%).")
    if not thin:
        print("Nothing to sample.")
        return

    rng = random.Random(args.seed)
    sample = rng.sample(thin, min(args.sample, len(thin)))
    spot = sample[: args.spot]

    base = f"{args.outdir}/{domain.replace('.', '_')}"
    with open(f"{base}_spotcheck.txt", "w", encoding="utf-8") as f:
        f.write(f"Open each page and check whether the product really has no useful description or specs.\n"
                f"Store: {domain}   Thin products: {len(thin)} of {total} ({pct:.1f}%)\n\n")
        for p in spot:
            f.write(f"https://{domain}/products/{p.get('handle', '')}\n    {p.get('title', '')}\n")

    cols = ["product_url", "title", "brand", "product_type", "sku", "price", "image_url",
            "current_description", "model_code_guess", "voltage", "drive_size", "size_mm", "torque", "rpm",
            "NEW_description", "spec_source_url"]
    with open(f"{base}_sample.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for p in sample:
            v = (p.get("variants") or [{}])[0]
            imgs = p.get("images") or []
            row = {
                "product_url": f"https://{domain}/products/{p.get('handle', '')}",
                "title": p.get("title", ""),
                "product_type": p.get("product_type", ""),
                "sku": v.get("sku", ""),
                "price": v.get("price", ""),
                "image_url": imgs[0].get("src", "") if imgs else "",
                "current_description": strip_html(p.get("body_html")),
                "NEW_description": "",
                "spec_source_url": "",
            }
            row.update(extract_attrs(p))
            w.writerow(row)

    print(f"\nSaved {base}_spotcheck.txt ({len(spot)} URLs to open by hand)")
    print(f"Saved {base}_sample.csv ({len(sample)} products; fill NEW_description and spec_source_url)")
    print("\nNext: open the spot-check URLs. If most pages really have no useful description, the gap is real.")


if __name__ == "__main__":
    main()
