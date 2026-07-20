# libbynf

List **nonfiction audiobooks** from your Libby libraries with **biographies and
memoirs stripped out** — the exclusion Libby's own app can't do.

## Why

Libby's catalog filters are include-only (OR), and every title carries multiple
subject tags. A biography of a scientist is tagged *both* `Science` and
`Biography & Autobiography`, so it leaks into any `Science`/`Nonfiction` filter.
There is no NOT operator in the app.

This queries OverDrive's public **Thunder API** (no key, no auth, no login).
Each title comes back with its full `subjects[]` list + BISAC codes, so anything
tagged Biography / Autobiography / Memoir is dropped client-side.

A title is dropped as biography if **either**:
- it has subject id `7` (`Biography & Autobiography`) — catches YA/juvenile bios too, or
- any BISAC description contains `BIOGRAPHY` / `MEMOIR` / `AUTOBIOGRAPH` — catches memoirs.

## Usage

Needs [`uv`](https://docs.astral.sh/uv/) (no dependencies — pure stdlib):

```bash
uv run libbynf.py                      # newest nonfiction, no bios, both libraries
uv run libbynf.py -q "climate" -n 30   # keyword search, 30 results
uv run libbynf.py --sort popular -a    # most popular, available to borrow right now
uv run libbynf.py --adult              # adult Nonfiction only (drops YA/juvenile)
uv run libbynf.py --json > out.json    # raw filtered records for scripting
uv run libbynf.py --selftest           # run the filter self-check
```

Default libraries are Toronto (`toronto`) and Mississauga (`mississauga`);
duplicates across both are merged and tagged `[MIS·TPL]`. Override with
`-l KEY` (repeatable). A library key is the slug in its Libby URL —
`libbyapp.com/library/<key>`.

## Flags

| flag | effect |
|------|--------|
| `-q, --query` | keyword search (title/author/subject); omit to browse all |
| `--sort` | `newest` (default), `popular`, `relevance`, `released`, `title`, `author` |
| `-n, --max` | max titles to print (default 50) |
| `-a, --available` | only titles available to borrow now (skip hold queues) |
| `--adult` | require adult `Nonfiction` subject; drop young-adult/juvenile |
| `--min-rating` | drop titles below this star rating |
| `-l, --library` | library key, repeatable |
| `--json` | emit raw filtered items as JSON |

## Notes

- Ordering: `newest`/`released` are re-sorted by publish date after the two
  libraries are merged; other sorts follow the API order, Toronto first.
- Links point to `libbyapp.com/library/<key>/media/<id>`. If one 404s, search
  the title in Libby directly.
- Undocumented API — if OverDrive changes it, the field names in `libbynf.py`
  are where to look.
