# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""libbynf - browse a Libby/OverDrive library's catalog with filters the app
itself lacks.

Libby's catalog filters are include-only (OR) and titles carry multiple subject
tags, so a biography-of-a-scientist tagged both "Science" and "Biography" always
leaks into a Science filter. There is no NOT operator in the app.

This hits OverDrive's public Thunder API (no key, no auth). Every title comes
back with a subjects[] list + BISAC codes, so we filter client-side: strip
biographies/memoirs by default, and optionally narrow to specific genres.

  uv run libbynf.py                          # newest nonfiction audiobooks, no bios
  uv run libbynf.py -t ebook                 # ebooks instead (also: book, magazine)
  uv run libbynf.py -t audiobook -t ebook    # both formats, merged
  uv run libbynf.py -g history -g science    # narrow to genres (AND); see --genres
  uv run libbynf.py --all-genres -g romance  # fiction too (drop nonfiction gate)
  uv run libbynf.py --bio                    # keep biographies (default strips them)
  uv run libbynf.py --genres                 # list the genre names this catalog uses
  uv run libbynf.py --sort popular -a        # most popular, available right now
  uv run libbynf.py --json > out.json        # raw filtered records
  uv run libbynf.py --selftest               # filter self-check
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

THUNDER = "https://thunder.api.overdrive.com/v2/libraries/{key}/media"
UA = "libbynf/1.1 (+personal library browser)"
DEFAULT_LIBS = ["toronto", "mississauga"]
LIB_TAG = {"toronto": "TPL", "mississauga": "MIS"}

MEDIA = {"audiobook", "ebook", "magazine"}
TYPE_ALIASES = {"book": "ebook", "books": "ebook", "audiobooks": "audiobook",
                "ebooks": "ebook", "magazines": "magazine"}

# Thunder sortBy values, mapped to friendly names.
SORTS = {
    "newest": "newlyadded",
    "popular": "mostpopular",
    "relevance": "relevance",
    "released": "releasedate",
    "title": "title",
    "author": "author",
}

BIO_SUBJECT_ID = "7"         # "Biography & Autobiography"
NONFICTION_ADULT_ID = "111"  # "Nonfiction" (adult bucket)


def media_type(s):
    v = TYPE_ALIASES.get(s.lower(), s.lower())
    if v not in MEDIA:
        raise argparse.ArgumentTypeError(f"type must be audiobook, ebook/book, or magazine (got '{s}')")
    return v


def fetch(key, query, types, sort, page, per_page):
    params = {
        "mediaTypes": ",".join(types),
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


def matches_genres(item, genres):
    """AND: every requested genre must substring-match at least one subject."""
    names = [(s.get("name") or "").lower() for s in item.get("subjects", [])]
    return all(any(g in n for n in names) for g in genres)


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


def keep(item, args, genres):
    if not args.all_genres and not is_nonfiction(item, args.adult):
        return False
    if not args.bio and is_biography(item):
        return False
    if genres and not matches_genres(item, genres):
        return False
    if args.adult and is_juvenile(item):
        return False
    if args.available and not available_now(item):
        return False
    if args.min_rating and (item.get("starRating") or 0) < args.min_rating:
        return False
    return True


def collect(args, genres):
    """Page each library, filter, and merge by (title, author) across libraries."""
    merged = {}  # title_key -> record
    order = []   # preserves first-seen (API sort) order
    for key in args.library:
        seen_here = 0
        start = time.monotonic()
        for page in range(1, args.scan_pages + 1):
            if time.monotonic() - start > args.timeout:
                sys.stderr.write(f"! {key}: hit {args.timeout:g}s scan cap at page {page}\n")
                break
            try:
                data = fetch(key, args.query, args.type, args.sort, page, args.per_page)
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
                if not keep(it, args, genres):
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


def list_genres(args):
    """Print the subject/genre facet for this catalog, so -g names are known."""
    data = fetch(args.library[0], args.query, args.type, args.sort, 1, args.per_page)
    subs = data.get("facets", {}).get("subjects", {}).get("items", [])
    if not subs:
        print("no genre facet returned")
        return
    for s in subs:
        print(f"{s.get('totalItemsText', ''):>9}  {s.get('name', '')}")


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
    return f"{title} — {author}\n   {' · '.join(bits)}\n   [{tags}] {avail}   {libby_url(rec)}"


def selftest():
    stanley = {"subjects": [{"id": "36", "name": "History"}, {"id": "111", "name": "Nonfiction"}],
               "bisac": [{"description": "History / Essays"}]}
    cassidy = {"subjects": [{"id": "7", "name": "Biography & Autobiography"},
                            {"id": "128", "name": "Young Adult Nonfiction"}],
               "bisac": [{"description": "Young Adult Nonfiction / Biography & Autobiography / General"}]}
    memoir = {"subjects": [{"id": "111", "name": "Nonfiction"}],
              "bisac": [{"description": "BIOGRAPHY & AUTOBIOGRAPHY / Memoirs"}]}
    fiction = {"subjects": [{"id": "26", "name": "Fiction"}, {"id": "77", "name": "Romance"}], "bisac": []}

    assert is_nonfiction(stanley, False) and not is_biography(stanley)   # keep
    assert is_biography(cassidy)                                          # drop (subject 7)
    assert is_nonfiction(cassidy, False) and not is_nonfiction(cassidy, True)  # YA nonfic, not adult
    assert is_biography(memoir)                                           # drop (BISAC memoir)
    assert not is_nonfiction(fiction, False)                             # drop (fiction)
    assert matches_genres(stanley, ["history"]) and not matches_genres(stanley, ["science"])
    assert matches_genres(fiction, ["romance"])                          # genre substring match
    print("selftest ok")


def main():
    p = argparse.ArgumentParser(description="Browse a Libby catalog, biographies stripped by default.")
    p.add_argument("-q", "--query", default="", help="keyword search (optional)")
    p.add_argument("-t", "--type", action="append", type=media_type, metavar="KIND",
                   help="audiobook (default), ebook/book, magazine; repeatable")
    p.add_argument("-g", "--genre", action="append", metavar="NAME",
                   help="require this genre/subject (repeatable, AND; substring match). See --genres")
    p.add_argument("-l", "--library", action="append", metavar="KEY",
                   help="library key (repeatable); default: toronto, mississauga")
    p.add_argument("--sort", choices=SORTS, default="newest")
    p.add_argument("-n", "--max", type=int, default=50, help="max titles to print")
    p.add_argument("-a", "--available", action="store_true", help="only titles available now")
    p.add_argument("--all-genres", action="store_true",
                   help="don't require the Nonfiction subject (browse fiction too)")
    p.add_argument("--bio", action="store_true", help="keep biographies/memoirs (default: strip them)")
    p.add_argument("--adult", action="store_true", help="adult Nonfiction only (drop YA/juvenile)")
    p.add_argument("--min-rating", type=float, default=0.0, metavar="R")
    p.add_argument("--per-page", type=int, default=100)
    p.add_argument("--scan-pages", type=int, default=25, help="max pages to scan per library")
    p.add_argument("--timeout", type=float, default=20.0, metavar="SEC",
                   help="max seconds to scan per library (guards over-narrow filters)")
    p.add_argument("--genres", action="store_true", help="list the genre names this catalog uses, then exit")
    p.add_argument("--json", action="store_true", help="emit raw filtered items as JSON")
    p.add_argument("--selftest", action="store_true", help="run filter self-check and exit")
    args = p.parse_args()

    if args.selftest:
        selftest()
        return
    if not args.type:
        args.type = ["audiobook"]
    if not args.library:
        args.library = list(DEFAULT_LIBS)
    if args.type == ["magazine"]:
        # magazines aren't fiction/nonfiction and carry no bio tags; those gates don't apply
        args.all_genres = True
        args.bio = True
    genres = [g.lower() for g in (args.genre or [])]

    if args.genres:
        list_genres(args)
        return

    recs = collect(args, genres)
    if args.json:
        json.dump([r["item"] for r in recs], sys.stdout, indent=2, ensure_ascii=False)
        return
    if not recs:
        hint = " (try --all-genres, drop -g/--available, or widen --query)"
        print("no matching titles" + hint)
        return
    for i, rec in enumerate(recs, 1):
        print(f"{i}. {fmt(rec)}")


if __name__ == "__main__":
    main()
