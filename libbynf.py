# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""libbynf - list NONFICTION audiobooks from Libby/OverDrive libraries with
biographies and memoirs stripped out.

Libby's own UI filters are include-only (OR), and titles carry multiple
subject tags, so a biography-of-a-scientist tagged both "Science" and
"Biography" always leaks into a Science filter. There is no NOT operator in
the app.

This hits OverDrive's public Thunder API (no key, no auth). Every title comes
back with a subjects[] list + BISAC codes, so we drop anything tagged
Biography & Autobiography / Memoir *client-side* - the exclusion Libby can't do.

  uv run libbynf.py                      # newest nonfiction, no bios, both libraries
  uv run libbynf.py -q "climate" -n 30   # keyword search
  uv run libbynf.py --sort popular -a    # most popular, available right now
  uv run libbynf.py --adult              # adult Nonfiction only (drop YA/juvenile)
  uv run libbynf.py --json > out.json    # raw filtered records
  uv run libbynf.py --selftest           # run the filter self-check
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

THUNDER = "https://thunder.api.overdrive.com/v2/libraries/{key}/media"
UA = "libbynf/1.0 (+personal audiobook browser)"
DEFAULT_LIBS = ["toronto", "mississauga"]
LIB_TAG = {"toronto": "TPL", "mississauga": "MIS"}

# Thunder sortBy values, mapped to friendly names.
SORTS = {
    "newest": "newlyadded",
    "popular": "mostpopular",
    "relevance": "relevance",
    "released": "releasedate",
    "title": "title",
    "author": "author",
}

BIO_SUBJECT_ID = "7"        # "Biography & Autobiography"
NONFICTION_ADULT_ID = "111"  # "Nonfiction" (adult bucket)


def fetch(key, query, sort, page, per_page):
    params = {
        "mediaTypes": "audiobook",
        "sortBy": SORTS[sort],
        "perPage": per_page,
        "page": page,
    }
    if query:
        params["query"] = query
    url = THUNDER.format(key=key) + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def is_biography(item):
    """True if the title is a biography/memoir/autobiography by any signal.

    Subject id 7 catches Libby's coarse tag (incl. YA/juvenile bios that carry
    a YAN* BISAC, not BIO*); the BISAC-description scan catches memoirs and
    anything whose fine-grained code says biography without the subject tag.
    """
    if any(s.get("id") == BIO_SUBJECT_ID for s in item.get("subjects", [])):
        return True
    for b in item.get("bisac", []):
        d = (b.get("description") or "").upper()
        if "BIOGRAPHY" in d or "MEMOIR" in d or "AUTOBIOGRAPH" in d:
            return True
    return False


def is_nonfiction(item, adult):
    subs = item.get("subjects", [])
    if adult:
        return any(s.get("id") == NONFICTION_ADULT_ID for s in subs)
    return any("NONFICTION" in (s.get("name") or "").upper() for s in subs)


def is_juvenile(item):
    mat = (item.get("ratings", {}).get("maturityLevel", {}) or {}).get("id", "")
    return mat in ("juvenile", "youngadult")


def available_now(item):
    return bool(item.get("isAvailable")) and (item.get("availableCopies", 0) or 0) > 0


def narrators(item):
    return [c.get("name") for c in item.get("creators", []) if c.get("role") == "Narrator"]


def title_key(item):
    return (
        (item.get("title") or "").strip().lower(),
        (item.get("firstCreatorName") or "").strip().lower(),
    )


def date_of(item):
    return item.get("publishDate") or item.get("estimatedReleaseDate") or ""


def keep(item, args):
    if not is_nonfiction(item, args.adult):
        return False
    if is_biography(item):
        return False
    if args.adult and is_juvenile(item):
        return False
    if args.available and not available_now(item):
        return False
    if args.min_rating and (item.get("starRating") or 0) < args.min_rating:
        return False
    return True


def collect(args):
    """Page each library, filter, and merge by (title, author) across libraries."""
    merged = {}  # title_key -> record
    order = []   # preserves first-seen (API sort) order
    for key in args.library:
        seen_here = 0
        for page in range(1, args.scan_pages + 1):
            try:
                data = fetch(key, args.query, args.sort, page, args.per_page)
            except urllib.error.HTTPError as e:
                sys.stderr.write(f"! {key} p{page}: HTTP {e.code}\n")
                break
            except urllib.error.URLError as e:
                sys.stderr.write(f"! {key} p{page}: {e.reason}\n")
                break
            items = data.get("items", [])
            if not items:
                break
            total = data.get("totalItems", 0)
            for it in items:
                if not keep(it, args):
                    continue
                k = title_key(it)
                if k in merged:
                    rec = merged[k]
                    rec["libs"].add(key)
                    if available_now(it) and not rec["available"]:
                        rec["available"] = True
                        rec["key"] = key  # link to a copy that's actually in
                else:
                    merged[k] = {
                        "item": it,
                        "libs": {key},
                        "available": available_now(it),
                        "key": key,
                    }
                    order.append(k)
                    seen_here += 1
            if page * args.per_page >= total:
                break
            if seen_here >= args.max * 3:  # headroom for dedup, then stop early
                break

    recs = [merged[k] for k in order]
    if args.sort in ("newest", "released"):
        recs.sort(key=lambda r: date_of(r["item"]), reverse=True)
    return recs[: args.max]


def libby_url(rec):
    return f"https://libbyapp.com/library/{rec['key']}/media/{rec['item'].get('id')}"


def fmt(rec):
    it = rec["item"]
    title = it.get("title") or "(untitled)"
    author = it.get("firstCreatorName") or "?"
    bits = []
    narr = narrators(it)
    if narr:
        bits.append("narr. " + ", ".join(narr[:2]))
    if it.get("duration"):
        bits.append(str(it["duration"]))
    if it.get("starRating"):
        bits.append(f"★{it['starRating']}")
    names = [s.get("name") for s in it.get("subjects", [])]
    subs = ", ".join(n for n in names if n and n != "Nonfiction")
    if subs:
        bits.append(subs)
    tags = "·".join(sorted(str(LIB_TAG.get(l, l)) for l in rec["libs"]))
    avail = "available" if rec["available"] else (
        f"{it.get('holdsCount', 0)} holds/~{it.get('estimatedWaitDays', '?')}d")
    head = f"{title} — {author}"
    return f"{head}\n   {' · '.join(bits)}\n   [{tags}] {avail}   {libby_url(rec)}"


def selftest():
    stanley = {"subjects": [{"id": "36", "name": "History"}, {"id": "111", "name": "Nonfiction"}],
               "bisac": [{"description": "History / Essays"}]}
    cassidy = {"subjects": [{"id": "7", "name": "Biography & Autobiography"},
                            {"id": "128", "name": "Young Adult Nonfiction"}],
               "bisac": [{"description": "Young Adult Nonfiction / Biography & Autobiography / General"}]}
    memoir = {"subjects": [{"id": "111", "name": "Nonfiction"}],
              "bisac": [{"description": "BIOGRAPHY & AUTOBIOGRAPHY / Memoirs"}]}
    fiction = {"subjects": [{"id": "26", "name": "Fiction"}], "bisac": []}

    assert is_nonfiction(stanley, False) and not is_biography(stanley)   # keep
    assert is_biography(cassidy)                                          # drop (subject 7)
    assert is_nonfiction(cassidy, False) and not is_nonfiction(cassidy, True)  # YA nonfic, not adult
    assert is_biography(memoir)                                           # drop (BISAC memoir)
    assert not is_nonfiction(fiction, False)                             # drop (fiction)
    print("selftest ok")


def main():
    p = argparse.ArgumentParser(description="Nonfiction audiobooks from Libby, biographies stripped.")
    p.add_argument("-q", "--query", default="", help="keyword search (optional)")
    p.add_argument("-l", "--library", action="append", metavar="KEY",
                   help="library key (repeatable); default: toronto, mississauga")
    p.add_argument("--sort", choices=SORTS, default="newest")
    p.add_argument("-n", "--max", type=int, default=50, help="max titles to print")
    p.add_argument("-a", "--available", action="store_true", help="only titles available now")
    p.add_argument("--adult", action="store_true", help="adult Nonfiction only (drop YA/juvenile)")
    p.add_argument("--min-rating", type=float, default=0.0, metavar="R")
    p.add_argument("--per-page", type=int, default=100)
    p.add_argument("--scan-pages", type=int, default=25, help="max pages to scan per library")
    p.add_argument("--json", action="store_true", help="emit raw filtered items as JSON")
    p.add_argument("--selftest", action="store_true", help="run filter self-check and exit")
    args = p.parse_args()

    if args.selftest:
        selftest()
        return
    if not args.library:
        args.library = list(DEFAULT_LIBS)

    recs = collect(args)
    if args.json:
        json.dump([r["item"] for r in recs], sys.stdout, indent=2, ensure_ascii=False)
        return
    if not recs:
        print("no matching nonfiction audiobooks (try dropping --available or widening --query)")
        return
    for i, rec in enumerate(recs, 1):
        print(f"{i}. {fmt(rec)}")


if __name__ == "__main__":
    main()
