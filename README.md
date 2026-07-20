# libbynf

Browse a **Libby/OverDrive** library from the terminal with filters the app
itself lacks — chiefly **excluding biographies/memoirs**, which Libby can't do.

## Why

Libby's catalog filters are include-only (OR), and every title carries multiple
subject tags. A biography of a scientist is tagged *both* `Science` and
`Biography & Autobiography`, so it leaks into any `Science`/`Nonfiction` filter.
There is no NOT operator in the app.

This queries OverDrive's public **Thunder API** (no key, no auth, no login).
Each title comes back with its full `subjects[]` list + BISAC codes, so filtering
happens client-side.

A title is dropped as a biography (default; disable with `--bio`) if **either**:
- it has subject id `7` (`Biography & Autobiography`) — catches YA/juvenile bios too, or
- any BISAC description contains `BIOGRAPHY` / `MEMOIR` / `AUTOBIOGRAPH` — catches memoirs.

## Usage

Needs [`uv`](https://docs.astral.sh/uv/) (no dependencies — pure stdlib). Run
from inside this folder:

```bash
uv run libbynf.py                        # newest nonfiction audiobooks, no bios
uv run libbynf.py -t ebook               # ebooks instead (also: book, magazine)
uv run libbynf.py -t audiobook -t ebook  # both formats, merged
uv run libbynf.py -g history -g science  # narrow to genres (AND)
uv run libbynf.py --genres               # list the genre names this catalog uses
uv run libbynf.py --all-genres -g romance  # fiction too (drop the nonfiction gate)
uv run libbynf.py --bio                  # keep biographies (default strips them)
uv run libbynf.py --sort popular -a      # most popular, available to borrow now
uv run libbynf.py --no-goodreads         # skip Goodreads (offline/faster); OverDrive star
uv run libbynf.py --json > out.json      # raw filtered records for scripting
uv run libbynf.py --selftest             # run the filter self-check
```

Default libraries are Toronto (`toronto`) and Mississauga (`mississauga`);
a title in both is merged into one row showing **each library's own hold queue**
(`MIS 913 holds/~256d · TPL 4318 holds/~269d`), and the link points to whichever
has the shortest wait. Override the libraries with `-l KEY` (repeatable). A
library key is the slug in its Libby URL — `libbyapp.com/library/<key>`.

## Flags

| flag | effect |
|------|--------|
| `-t, --type` | `audiobook` (default), `ebook`/`book`, `magazine`; repeatable |
| `-g, --genre` | require this genre/subject (repeatable, **AND**, substring match) |
| `--genres` | list the genre names available in this catalog, then exit |
| `-q, --query` | keyword search (title/author/subject); omit to browse all |
| `--sort` | `newest` (default), `popular`, `relevance`, `released`, `title`, `author` |
| `-n, --max` | max titles to print (default 50) |
| `-a, --available` | only titles available to borrow now (skip hold queues) |
| `--all-genres` | don't require the `Nonfiction` subject (lets fiction through) |
| `--bio` | keep biographies/memoirs (default: strip them) |
| `--adult` | require adult `Nonfiction`; drop young-adult/juvenile |
| `--min-rating` | drop titles below this star rating (always OverDrive's — filtered before the Goodreads lookup) |
| `--no-goodreads`, `--no-gr` | skip the Goodreads lookup (faster / offline); show OverDrive's own star |
| `-l, --library` | library key, repeatable |
| `--scan-pages` | max pages to scan per library (default 25) |
| `--timeout` | max seconds to scan per library (default 20) |
| `--json` | emit raw filtered items as JSON |

## Notes

- **Genres are AND**: `-g history -g science` returns only titles tagged both.
  Run twice for an either/or. Narrow intersections may hit the `--timeout` cap
  (a `hit 20s scan cap` note on stderr) — raise `--timeout`/`--scan-pages` to
  dig deeper.
- **Magazines** carry no fiction/nonfiction or bio tags, so `-t magazine` alone
  auto-drops the nonfiction gate and bio-strip. Genre flags still apply
  (`-t magazine -g "food & wine"`).
- Ordering: `newest`/`released` are re-sorted by publish date after the two
  libraries merge; other sorts follow the API order, Toronto first.
- Links are Libby title-card deep links (`libbyapp.com/search/<key>/search/page-1/<id>`),
  scoped to the shorter-wait library. (`/library/<key>/media/<id>` is *not* a real
  Libby route — it bounces to the library home.)
- In a terminal the output is colored and the `↗ open in Libby` label is a
  clickable OSC 8 hyperlink; wait times are green/yellow/red by length. Piping
  the output or setting `NO_COLOR=1` falls back to plain text with raw URLs.
- Inside **tmux** (which strips OSC 8) the link is shown as the visible URL
  instead, so the terminal's own URL matcher keeps it clickable (Cmd+click in
  Ghostty).
- **Ratings**: the `★` is **Goodreads' rating + rating count, by default** (the
  `(1.4M)`-style count is the tell). Goodreads retired its API, so this queries
  its public title-autocomplete endpoint and validates the returned title+author
  before trusting a rating — a fuzzy match can't attach the wrong book's number
  (it shows nothing instead). One request per printed title, cached 30 days in
  `~/.cache/libbynf`. `--no-goodreads` skips it (offline, or to fall back to
  OverDrive's own patron star — which is unmaintained and usually absent on new
  titles).
- Undocumented APIs — if OverDrive or Goodreads change them, the field names in
  `libbynf.py` are where to look.
