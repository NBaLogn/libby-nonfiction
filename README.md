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
| `--min-rating` | drop titles below this star rating |
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
- Links point to `libbyapp.com/library/<key>/media/<id>`. If one 404s, search
  the title in Libby directly.
- Undocumented API — if OverDrive changes it, the field names in `libbynf.py`
  are where to look.
