# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Browse a Libby/OverDrive library from the terminal, biographies filtered out.

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
  uv run libbynf.py --genres                 # list this catalog's genre names + ids
  uv run libbynf.py --sort popular -a        # most popular, available right now
  uv run libbynf.py --json > out.json        # raw filtered records
  uv run libbynf.py --selftest               # filter self-check
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator
    from http.client import HTTPResponse

Item = dict[str, Any]  # a Thunder media record
Record = dict[str, Any]  # {item, copies: {lib: item}, gr?}
Facet = list[dict[str, Any]]  # subject facet entries


class Query(NamedTuple):
    """The stable-per-run parts of a catalog query (everything but the page)."""

    query: str
    types: list[str]
    sort: str
    per_page: int
    subjects: tuple[str, ...] = ()


THUNDER = "https://thunder.api.overdrive.com/v2/libraries/{key}/media"
UA = "libbynf/1.1 (+personal library browser)"
# Goodreads retired its public API (2020); we query its title-autocomplete endpoint.
UA_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
GR_CACHE = Path.home() / ".cache" / "libbynf" / "goodreads.json"
GR_TTL = 30 * 86400  # ratings drift slowly; refresh monthly
GR_MAX_WORKERS = 8
DEFAULT_LIBS = ["toronto", "mississauga"]
LIB_TAG = {"toronto": "TPL", "mississauga": "MIS"}

MEDIA = {"audiobook", "ebook", "magazine"}
TYPE_ALIASES = {
    "book": "ebook",
    "books": "ebook",
    "audiobooks": "audiobook",
    "ebooks": "ebook",
    "magazines": "magazine",
}

# Thunder sortBy values, mapped to friendly names.
SORTS = {
    "newest": "newlyadded",
    "popular": "mostpopular",
    "relevance": "relevance",
    "released": "releasedate",
    "title": "title",
    "author": "author",
}

BIO_SUBJECT_ID = "7"  # "Biography & Autobiography"
NONFICTION_ADULT_ID = "111"  # "Nonfiction" (adult bucket)

WAIT_GREEN_DAYS = 30  # estimated wait at/under this shows green
WAIT_YELLOW_DAYS = 120  # ...at/under this shows yellow, above shows red
NEVER_WAIT = 10**9  # sort sentinel for a copy with no wait estimate
DEDUP_HEADROOM = 3  # collect this many x --max candidates before cross-library dedup
NARRATORS_SHOWN = 2  # cap narrator names printed per title
MILLION = 1_000_000
THOUSAND = 1_000

# Color/links only when writing to a real terminal (keeps pipes and --json clean).
TTY = sys.stdout.isatty()
COLOR = TTY and "NO_COLOR" not in os.environ


def paint(s: str, code: str) -> str:
    """Wrap text in an ANSI SGR code, or return it unchanged when color is off."""
    return f"\x1b[{code}m{s}\x1b[0m" if COLOR else s


def hyperlink(label: str, url: str) -> str:
    """Return an OSC 8 terminal hyperlink, or the bare URL when it can't apply.

    OSC 8 works in Ghostty/iTerm/WezTerm/kitty. tmux strips it (leaving an
    unclickable label), so inside tmux — and when piped — show the visible URL
    and let the terminal's own URL matcher handle it.
    """
    if not TTY or os.environ.get("TMUX"):
        return url
    return f"\x1b]8;;{url}\x1b\\{label}\x1b]8;;\x1b\\"


def wait_code(item: Item) -> str:
    """Return an ANSI color code for a copy's estimated wait (green/yellow/red)."""
    d = item.get("estimatedWaitDays")
    if d is None:
        return "2"  # dim
    if d <= WAIT_GREEN_DAYS:
        return "32"  # green
    if d <= WAIT_YELLOW_DAYS:
        return "33"  # yellow
    return "31"  # red


def human(n: int) -> str:
    """Format a count compactly, e.g. 1392620 -> '1.4M', 2506 -> '2k'."""
    if n >= MILLION:
        return f"{n / MILLION:.1f}M"
    if n >= THOUSAND:
        return f"{n // THOUSAND}k"
    return str(n)


def media_type(s: str) -> str:
    """Normalize a --type value to a Thunder mediaType, or reject it."""
    v = TYPE_ALIASES.get(s.lower(), s.lower())
    if v not in MEDIA:
        msg = f"type must be audiobook, ebook/book, or magazine (got '{s}')"
        raise argparse.ArgumentTypeError(msg)
    return v


def _urlopen(url: str, headers: dict[str, str], timeout: int) -> HTTPResponse:
    """Open an http(s) URL, rejecting other schemes (the ruff S310 audit)."""
    if not url.startswith(("http:", "https:")):
        msg = f"refusing non-http(s) URL: {url}"
        raise ValueError(msg)
    req = urllib.request.Request(url, headers=headers)  # noqa: S310  # scheme guarded
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310  # scheme guarded


def fetch(key: str, q: Query, page: int) -> dict[str, Any]:
    """Fetch one page of a library's media catalog from the Thunder API."""
    params: list[tuple[str, Any]] = [
        ("mediaTypes", ",".join(q.types)),
        ("sortBy", SORTS[q.sort]),
        ("perPage", q.per_page),
        ("page", page),
    ]
    if q.query:
        params.append(("query", q.query))
    params += [
        ("subject", s) for s in q.subjects
    ]  # server-side genre filter (repeated = AND)
    url = THUNDER.format(key=key) + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA, "Accept": "application/json"}
    with _urlopen(url, headers, 30) as r:
        return json.load(r)


def fetch_subject_facet(key: str, types: list[str]) -> Facet:
    """Return the catalog-wide subject facet: [{id, name, totalItems}, ...]."""
    data = fetch(key, Query("", types, "relevance", 1), 1)
    return data.get("facets", {}).get("subjects", {}).get("items", [])


def resolve_genres(genres: list[str], facet: Facet) -> tuple[list[str], list[str]]:
    """Split requested genres into (server-side subject ids, client-side names).

    A genre maps to a server-side id only when it hits exactly one subject (exact
    name wins, else a unique substring). Ambiguous (several subjects, no exact) or
    unknown genres fall back to the client-side name filter — nothing is lost, it
    just isn't sped up. Server ids are AND-ed by Thunder, matching -g's AND.
    """
    server_ids, client = [], []
    for g in genres:
        matches = [s for s in facet if g in (s.get("name") or "").lower()]
        exact = [s for s in matches if (s.get("name") or "").lower() == g]
        chosen = exact or matches
        if len(chosen) == 1:
            server_ids.append(chosen[0]["id"])
        else:
            client.append(g)  # 0 = unknown, >1 = ambiguous
    return server_ids, client


def is_biography(item: Item) -> bool:
    """Return True if the title is a biography/memoir/autobiography by any signal.

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


def is_nonfiction(item: Item, *, adult: bool) -> bool:
    """Return True if the title carries a Nonfiction subject (adult-only if asked)."""
    subs = item.get("subjects", [])
    if adult:
        return any(s.get("id") == NONFICTION_ADULT_ID for s in subs)
    return any("NONFICTION" in (s.get("name") or "").upper() for s in subs)


def matches_genres(item: Item, genres: list[str]) -> bool:
    """Return True when every genre substring-matches at least one subject (AND)."""
    names = [(s.get("name") or "").lower() for s in item.get("subjects", [])]
    return all(any(g in n for n in names) for g in genres)


def is_juvenile(item: Item) -> bool:
    """Return True for young-adult or juvenile maturity levels."""
    mat = (item.get("ratings", {}).get("maturityLevel", {}) or {}).get("id", "")
    return mat in ("juvenile", "youngadult")


def available_now(item: Item) -> bool:
    """Return True if the title has a copy available to borrow immediately."""
    return bool(item.get("isAvailable")) and (item.get("availableCopies", 0) or 0) > 0


def narrators(item: Item) -> list[str]:
    """Return the names of the title's narrators."""
    return [
        c.get("name") for c in item.get("creators", []) if c.get("role") == "Narrator"
    ]


def title_key(item: Item) -> tuple[str, str]:
    """Return a (title, author) key for merging the same title across libraries."""
    return (
        (item.get("title") or "").strip().lower(),
        (item.get("firstCreatorName") or "").strip().lower(),
    )


def date_of(item: Item) -> str:
    """Return the title's publish/release date string, or '' if unknown."""
    return item.get("publishDate") or item.get("estimatedReleaseDate") or ""


def _gr_norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _gr_key(title: str, author: str) -> str:
    return f"{_gr_norm(title)}\t{_gr_norm(author)}"


def _gr_pick(
    title: str, author: str, candidates: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """Return the first autocomplete candidate matching ours that has a rating.

    Guards against Goodreads' fuzzy matcher returning a plausible wrong book: the
    title must equal/prefix ours (or vice versa, to allow subtitles) and the
    authors must share a word. Returns None rather than risk a wrong rating.
    """
    ot, oa = _gr_norm(title), set(_gr_norm(author).split())
    for b in candidates or []:
        gt = _gr_norm(b.get("title"))
        if not (gt == ot or gt.startswith(ot) or ot.startswith(gt)):
            continue
        ga = set(_gr_norm((b.get("author") or {}).get("name")).split())
        if oa and not (oa & ga):
            continue
        try:
            rating = float(b.get("avgRating") or 0)
        except (TypeError, ValueError):
            continue
        if rating <= 0:
            continue
        return {"rating": rating, "count": int(b.get("ratingsCount") or 0)}
    return None


def _gr_lookup(key: tuple[str, str]) -> dict[str, Any] | None:
    """Query Goodreads' title-autocomplete and return the validated match."""
    title, author = key
    url = (
        "https://www.goodreads.com/book/auto_complete?format=json&q="
        + urllib.parse.quote(title)
    )
    try:
        with _urlopen(url, {"User-Agent": UA_BROWSER}, 15) as r:
            data = json.load(r)
    except (OSError, ValueError):  # network error or non-JSON body
        return None
    return _gr_pick(title, author, data)


def enrich_goodreads(recs: list[Record]) -> None:
    """Attach rec['gr'] = {rating, count} from Goodreads, disk-cached.

    Goodreads retired its API, so this queries the public title-autocomplete
    endpoint and validates title+author before trusting a rating. Hits and misses
    are both cached (keyed by title+author) so repeat runs are free until GR_TTL.
    """
    try:
        with GR_CACHE.open() as f:
            cache = json.load(f)
    except (OSError, ValueError):  # missing/corrupt cache
        cache = {}
    now = time.time()
    keys = [
        (r["item"].get("title") or "", r["item"].get("firstCreatorName") or "")
        for r in recs
    ]

    stale = {
        _gr_key(*k): k
        for k in keys
        if now - cache.get(_gr_key(*k), {}).get("t", 0) > GR_TTL
    }
    if stale:
        sys.stderr.write(f"… fetching {len(stale)} Goodreads ratings\n")
        ckeys, tuples = list(stale), list(stale.values())
        with ThreadPoolExecutor(max_workers=GR_MAX_WORKERS) as ex:
            for ckey, res in zip(ckeys, ex.map(_gr_lookup, tuples), strict=True):
                cache[ckey] = {"r": res, "t": now}  # r may be None (negative cache)
        with contextlib.suppress(OSError):  # best-effort cache write
            GR_CACHE.parent.mkdir(parents=True, exist_ok=True)
            with GR_CACHE.open("w") as f:
                json.dump(cache, f)

    for rec, k in zip(recs, keys, strict=True):
        hit = cache.get(_gr_key(*k), {}).get("r")
        if hit:
            rec["gr"] = hit


def keep(item: Item, args: argparse.Namespace, genres: list[str]) -> bool:
    """Return True if the item passes every active filter."""
    if not args.all_genres and not is_nonfiction(item, adult=args.adult):
        return False
    if not args.bio and is_biography(item):
        return False
    if genres and not matches_genres(item, genres):
        return False
    if args.adult and is_juvenile(item):
        return False
    if args.available and not available_now(item):
        return False
    return not (args.min_rating and (item.get("starRating") or 0) < args.min_rating)


def _pages(
    key: str, args: argparse.Namespace, q: Query
) -> Iterator[tuple[int, list[Item], int]]:
    """Yield (page, items, total) per page until a cap, error, or empty stops it."""
    start = time.monotonic()
    for page in range(1, args.scan_pages + 1):
        if time.monotonic() - start > args.timeout:
            sys.stderr.write(
                f"! {key}: hit {args.timeout:g}s scan cap at page {page}\n"
            )
            return
        try:
            data = fetch(key, q, page)
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"! {key} p{page}: HTTP {e.code}\n")
            return
        except urllib.error.URLError as e:
            sys.stderr.write(f"! {key} p{page}: {e.reason}\n")
            return
        items = data.get("items", [])
        if not items:
            return
        yield page, items, data.get("totalItems", 0)


def collect(
    args: argparse.Namespace, genres: list[str], subjects: list[str]
) -> list[Record]:
    """Page each library, filter, and merge by (title, author) across libraries."""
    merged: dict[tuple[str, str], Record] = {}
    order: list[tuple[str, str]] = []  # preserves first-seen (API sort) order
    q = Query(args.query, args.type, args.sort, args.per_page, tuple(subjects))
    for key in args.library:
        seen_here = 0
        for page, items, total in _pages(key, args, q):
            for it in items:
                if not keep(it, args, genres):
                    continue
                k = title_key(it)
                if k in merged:
                    merged[k]["copies"][key] = it  # same title, other library's queue
                else:
                    merged[k] = {"item": it, "copies": {key: it}}
                    order.append(k)
                    seen_here += 1
            if page * args.per_page >= total or seen_here >= args.max * DEDUP_HEADROOM:
                break

    recs = [merged[k] for k in order]
    if args.sort in ("newest", "released"):
        recs.sort(key=lambda r: date_of(r["item"]), reverse=True)
    return recs[: args.max]


def list_genres(args: argparse.Namespace) -> None:
    """Print the subject/genre facet for this catalog, so -g names are known."""
    data = fetch(
        args.library[0], Query(args.query, args.type, args.sort, args.per_page), 1
    )
    subs = data.get("facets", {}).get("subjects", {}).get("items", [])
    if not subs:
        sys.stdout.write("no genre facet returned\n")
        return
    for s in subs:
        sys.stdout.write(
            f"{s.get('totalItemsText', ''):>9}  "
            f"[{s.get('id', '')}] {s.get('name', '')}\n"
        )


def best_lib(copies: dict[str, Item]) -> str:
    """Pick a library to link: an available copy first, else the shortest wait.

    Wait, not hold count: more copies can mean a shorter wait despite more holds.
    """

    def rank(lib: str) -> tuple[int, int]:
        it = copies[lib]
        if available_now(it):
            return (0, 0)
        return (1, it.get("estimatedWaitDays") or NEVER_WAIT)

    return min(copies, key=rank)


def avail_chunk(lib: str, item: Item) -> str:
    """Render one library's availability, e.g. 'MIS 913 holds ~256d' (color-coded)."""
    tag = paint(LIB_TAG.get(lib, lib), "1")
    if available_now(item):
        return f"{tag} {paint('available', '32')}"
    holds = paint(f"{item.get('holdsCount', 0)} holds", "2")
    wd = item.get("estimatedWaitDays")
    wait = paint(f"~{wd}d" if wd else "~?d", wait_code(item))
    return f"{tag} {holds} {wait}"


def render(rec: Record, idx: int, width: int) -> str:
    """Render one result as its multi-line terminal block."""
    it, copies = rec["item"], rec["copies"]
    pad = " " * (width + 2)

    num = paint(f"{idx:>{width}}.", "2")
    title = paint(it.get("title") or "(untitled)", "1")
    gr = rec.get("gr")
    if gr:  # Goodreads (count in the millions is the tell that it isn't OverDrive's)
        cnt = f" ({human(gr['count'])})" if gr.get("count") else ""
        rating = "  " + paint(f"★{gr['rating']}{cnt}", "33")
    elif it.get("starRating"):
        rating = "  " + paint(f"★{it['starRating']}", "33")
    else:
        rating = ""
    lines = [f"{num} {title}{rating}"]

    meta = [it.get("firstCreatorName") or "?"]
    narr = narrators(it)
    if narr:
        meta.append(paint("narr. " + ", ".join(narr[:NARRATORS_SHOWN]), "2"))
    if it.get("duration"):
        meta.append(paint(str(it["duration"]), "2"))
    lines.append(pad + " · ".join(meta))

    names = [
        s.get("name")
        for s in it.get("subjects", [])
        if s.get("name") and s.get("name") != "Nonfiction"
    ]
    if names:
        lines.append(pad + paint(" · ".join(names), "2"))

    lines.append(
        pad + "    ".join(avail_chunk(lib, copies[lib]) for lib in sorted(copies))
    )

    url = f"https://libbyapp.com/search/{best_lib(copies)}/search/page-1/{it.get('id')}"
    lines.append(pad + paint(hyperlink("↗ open in Libby", url), "36"))
    return "\n".join(lines)


def _expect(ok: object, label: str) -> None:
    """Raise AssertionError(label) when ok is falsy (self-test check)."""
    if not ok:
        raise AssertionError(label)


def selftest() -> None:
    """Check the pure filter/format helpers behave, then write 'selftest ok'."""
    stanley = {
        "subjects": [
            {"id": "36", "name": "History"},
            {"id": "111", "name": "Nonfiction"},
        ],
        "bisac": [{"description": "History / Essays"}],
    }
    cassidy = {
        "subjects": [
            {"id": "7", "name": "Biography & Autobiography"},
            {"id": "128", "name": "Young Adult Nonfiction"},
        ],
        "bisac": [
            {"description": "Young Adult Nonfiction / Biography & Autobiography"}
        ],
    }
    memoir = {
        "subjects": [{"id": "111", "name": "Nonfiction"}],
        "bisac": [{"description": "BIOGRAPHY & AUTOBIOGRAPHY / Memoirs"}],
    }
    fiction = {
        "subjects": [{"id": "26", "name": "Fiction"}, {"id": "77", "name": "Romance"}],
        "bisac": [],
    }

    _expect(is_nonfiction(stanley, adult=False), "stanley is nonfiction")
    _expect(not is_biography(stanley), "stanley not a biography")
    _expect(is_biography(cassidy), "cassidy dropped (subject 7)")
    _expect(is_nonfiction(cassidy, adult=False), "cassidy is YA nonfiction")
    _expect(not is_nonfiction(cassidy, adult=True), "cassidy not adult nonfiction")
    _expect(is_biography(memoir), "memoir dropped (BISAC)")
    _expect(not is_nonfiction(fiction, adult=False), "fiction dropped")
    _expect(matches_genres(stanley, ["history"]), "stanley matches history")
    _expect(not matches_genres(stanley, ["science"]), "stanley not science")
    _expect(matches_genres(fiction, ["romance"]), "fiction matches romance")
    _expect(human(1392620) == "1.4M", "human 1.4M")
    _expect(human(2506) == "2k", "human 2k")
    _expect(human(15) == "15", "human 15")

    gr_cands = [
        {
            "title": "Raising Human Beings",
            "author": {"name": "Ross Greene"},
            "avgRating": "4.2",
            "ratingsCount": 2376,
        },
        {
            "title": "Human Raised: Nurturing Connection",
            "author": {"name": "Dana Suskind"},
            "avgRating": "4.67",
            "ratingsCount": 12,
        },
    ]
    gr_hit = _gr_pick(
        "Human Raised", "Dana Suskind", gr_cands
    )  # picks 2nd, not fuzzy 1st
    _expect(gr_hit is not None, "gr_hit found")
    _expect(gr_hit and gr_hit["rating"] == float(gr_cands[1]["avgRating"]), "gr rating")
    wrong = _gr_pick(
        "The Industrial Revolution",
        "Robert Allen",
        [
            {
                "title": "The Fourth Industrial Revolution",
                "author": {"name": "Klaus Schwab"},
                "avgRating": "3.56",
            }
        ],
    )
    _expect(wrong is None, "wrong match rejected")

    facet = [
        {"id": "36", "name": "History"},
        {"id": "115", "name": "Historical Fiction"},
        {"id": "26", "name": "Fiction"},
    ]
    _expect(resolve_genres(["history"], facet) == (["36"], []), "history server id")
    _expect(resolve_genres(["fiction"], facet) == (["26"], []), "fiction exact id")
    _expect(resolve_genres(["histor"], facet) == ([], ["histor"]), "histor ambiguous")
    _expect(
        resolve_genres(["kayaking"], facet) == ([], ["kayaking"]), "kayaking unknown"
    )
    sys.stdout.write("selftest ok\n")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    p = argparse.ArgumentParser(
        description="Browse a Libby catalog, biographies stripped by default."
    )
    p.add_argument("-q", "--query", default="", help="keyword search (optional)")
    p.add_argument(
        "-t",
        "--type",
        action="append",
        type=media_type,
        metavar="KIND",
        help="audiobook (default), ebook/book, magazine; repeatable",
    )
    p.add_argument(
        "-g",
        "--genre",
        action="append",
        metavar="NAME",
        help="require this genre/subject (repeatable, AND; substring match). "
        "See --genres",
    )
    p.add_argument(
        "-l",
        "--library",
        action="append",
        metavar="KEY",
        help="library key (repeatable); default: toronto, mississauga",
    )
    p.add_argument("--sort", choices=SORTS, default="popular")
    p.add_argument("-n", "--max", type=int, default=50, help="max titles to print")
    p.add_argument(
        "-a", "--available", action="store_true", help="only titles available now"
    )
    p.add_argument(
        "--all-genres",
        action="store_true",
        help="don't require the Nonfiction subject (browse fiction too)",
    )
    p.add_argument(
        "--bio",
        action="store_true",
        help="keep biographies/memoirs (default: strip them)",
    )
    p.add_argument(
        "--adult", action="store_true", help="adult Nonfiction only (drop YA/juvenile)"
    )
    p.add_argument("--min-rating", type=float, default=0.0, metavar="R")
    p.add_argument(
        "--no-goodreads",
        "--no-gr",
        dest="no_goodreads",
        action="store_true",
        help="skip the Goodreads lookup (faster / works offline); show OverDrive's own "
        "star instead. Goodreads is queried by default, cached in ~/.cache/libbynf",
    )
    p.add_argument("--per-page", type=int, default=100)
    p.add_argument(
        "--scan-pages", type=int, default=25, help="max pages to scan per library"
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        metavar="SEC",
        help="max seconds to scan per library (guards over-narrow filters)",
    )
    p.add_argument(
        "--genres",
        action="store_true",
        help="list the catalog's genre names + ids, then exit",
    )
    p.add_argument(
        "--json", action="store_true", help="emit raw filtered items as JSON"
    )
    p.add_argument(
        "--selftest", action="store_true", help="run filter self-check and exit"
    )
    return p


def _resolve_genre_filter(
    args: argparse.Namespace, genres: list[str]
) -> tuple[list[str], list[str]]:
    """Map genres to server-side subject ids where unique; log the split."""
    if not genres:
        return [], genres
    facet = fetch_subject_facet(args.library[0], args.type)
    subjects, client = resolve_genres(genres, facet)
    if subjects:
        sys.stderr.write(
            f"… genre filtered server-side (subject={','.join(map(str, subjects))})\n"
        )
    if client:
        sys.stderr.write(
            f"… client-side scan for genre(s): {', '.join(client)} "
            "(ambiguous/unknown name)\n"
        )
    return subjects, client


def _emit(recs: list[Record], args: argparse.Namespace) -> None:
    """Print the results as JSON or as formatted terminal blocks."""
    if args.json:
        items = [
            {**r["item"], "goodreads": r["gr"]} if r.get("gr") else r["item"]
            for r in recs
        ]
        json.dump(items, sys.stdout, indent=2, ensure_ascii=False)
        return
    if not recs:
        sys.stdout.write(
            "no matching titles "
            "(try --all-genres, drop -g/--available, or widen --query)\n"
        )
        return
    width = len(str(len(recs)))
    body = "\n\n".join(render(rec, i, width) for i, rec in enumerate(recs, 1))
    sys.stdout.write(body + "\n")


def main() -> None:
    """Parse arguments, run the query, and print the results."""
    args = build_parser().parse_args()

    if args.selftest:
        selftest()
        return
    if not args.type:
        args.type = ["audiobook"]
    if not args.library:
        args.library = list(DEFAULT_LIBS)
    if args.type == ["magazine"]:
        # magazines aren't fiction/nonfiction and carry no bio tags; gates don't apply
        args.all_genres = True
        args.bio = True
    if args.genres:
        list_genres(args)
        return

    genres = [g.lower() for g in (args.genre or [])]
    subjects, genres = _resolve_genre_filter(args, genres)
    recs = collect(args, genres, subjects)
    if not args.no_goodreads:
        enrich_goodreads(recs)
    _emit(recs, args)


if __name__ == "__main__":
    main()
