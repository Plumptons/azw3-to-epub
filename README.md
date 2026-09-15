# azw3-to-epub

Reference complementary tool for Matt’s media PC: watch `/media/books`, convert AZW3 → EPUB, then nudge Bindery + Storyteller.

**Runtime:** paste into the **main** media-PC compose (same Docker network as Bindery / Storyteller / ReadarrBookshelf). Prefer Docker DNS (`http://bindery:8787`, `http://storyteller:8001`) — not LAN-IP-first.

**Books:** Bindery and ReadarrBookshelf both stay. This service talks to Bindery for ebook-link hygiene; do not treat Bindery as the only book manager.

---

## Quick start (media PC)

1. Copy secrets into the compose folder’s `.env` (never commit real values):
   - `BINDERY_API_KEY=`
   - `STORYTELLER_USERNAME=` / `STORYTELLER_PASSWORD=`
   - Also rotate/set `STORYTELLER_SECRET_KEY=` on the **Storyteller** service itself if it was ever pasted in YAML.
2. Paste [`compose.snippet.yml`](./compose.snippet.yml) into the main stack compose.
3. `docker compose pull azw3-to-epub && docker compose up -d azw3-to-epub`

Local/dev: copy [`.env.example`](./.env.example) → `.env`, set `MEDIA_DIR`, then `docker compose up --build`.

---

## Safety contracts

| Action | Default | Mutates disk? | Notes |
|--------|---------|---------------|--------|
| AZW3 → EPUB convert | on | **Adds** `.epub` beside source | Never overwrites a newer EPUB |
| `DELETE_SOURCE` | **`false`** | Only if explicitly `true` | Even then, skips delete when Bindery already removed the AZW3 |
| Bindery queue clear | on | **No library files** | `deleteFiles=false` always |
| Bindery ebook replace | on | May remove AZW3 via Bindery API after EPUB import | Parks EPUB first so sibling sweep cannot kill it |
| Folder coalesce | on | Moves/merges `Title (N)` siblings | Set `FOLDER_COALESCE_DRY_RUN=true` to log only |
| Storyteller merge / series / read-along | on | Storyteller API only | Read-along limited to night window |
| `scripts/check_bindery_files_on_disk.py` | report | **No** unless `--fix` | `--fix` unlinks **catalogue** paths only (`deleteFiles=false`) |

### Never delete library files as a “fix”

- Catalogue unlink (Bindery metadata) is allowed when documented and gated (`--fix`).
- Queue row removal must not pass `deleteFiles=true`.
- Coalesce may remove **duplicate identical** files already present in the primary folder, or empty split folders after a merge — not arbitrary library content.
- `DELETE_SOURCE=false` stays the production default.

### Dry-run / report modes

- **Disk auditor:** omit `--fix` → report only.
- **Folder coalesce:** `FOLDER_COALESCE_DRY_RUN=true` → log planned moves/unlinks; no filesystem changes.
- **Converter:** there is no “convert dry-run”; leave the service stopped, or point `LIBRARY_DIR` at a scratch tree, to avoid writes.

---

## Layout

```
azw3-to-epub/          # watcher + Bindery/Storyteller clients
scripts/               # one-shot ops (disk check, series sync)
compose.snippet.yml    # paste into main media-PC compose
docker-compose.yml     # local/dev service def
.env.example           # placeholders only
```

---

## Dual-compat note

ReadarrBookshelf remains in the stack. New book tooling must stay dual-compatible — do not assume Bindery-only ownership of paths or retirement of ReadarrBookshelf.
