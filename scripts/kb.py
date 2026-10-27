"""The knowledge base and team scopes, per product, until the AI admin pages
call our API (component 10).

    python scripts/kb.py                                   every product's documents
    python scripts/kb.py list RICH                         one product's
    python scripts/kb.py upload RICH guide.md faq.md --by you@t2.sa
    python scripts/kb.py upload RICH C:\\docs\\rich --by you@t2.sa
                                                           every .md and .txt in a folder
    python scripts/kb.py index                             index what is waiting, now
    python scripts/kb.py retry <id> --by you@t2.sa
    python scripts/kb.py delete <id> --by you@t2.sa
    python scripts/kb.py search RICH "how is a sender name renewed?"
    python scripts/kb.py scopes RICH teams.md --by you@t2.sa     ALL for every product
    python scripts/kb.py scopes RICH                       show the scopes in use

Needs QDRANT_URL in .env, and RAGent2 installed, to index and search.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from support_agent import options  # noqa: E402
from support_agent.config import get_settings  # noqa: E402
from support_agent.db import migrate, postgres  # noqa: E402
from support_agent.knowledge import documents, retriever, scopes  # noqa: E402


def _show(code: str | None) -> None:
    rows = documents.listed(code)
    print(f"{len(rows)} document(s)" + (f" for {code.upper()}" if code else ""))
    for r in rows:
        note = f"  ({r['error']})" if r["error"] else ""
        chunks = f", {r['chunk_count']} chunks" if r["chunk_count"] else ""
        print(f"  #{r['id']:<4} {r['product']:<9} {r['status']:<9} {r['filename']}"
              f"  [{r['size_bytes'] or 0:,} bytes{chunks}, by {r['uploaded_by']}]{note}")


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(f for f in p.iterdir()
                          if f.is_file() and f.suffix.lower() in documents.TEXT_TYPES
                          + documents.DOCLING_TYPES)
        else:
            out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="list",
                    choices=["list", "upload", "index", "retry", "delete", "search", "scopes"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--by", default="")
    a = ap.parse_args()

    try:
        migrate.apply()
        options.sync()                                   # the products, if not fresh
        if a.command == "list":
            _show(a.args[0] if a.args else None)
        elif a.command == "upload":
            if len(a.args) < 2:
                print("upload PRODUCT FILE [FILE ...]")
                return 1
            for f in _files(a.args[1:]):
                if not f.is_file():
                    print(f"  not found: {f}")
                    continue
                try:
                    doc, new = documents.upload(a.args[0], f.name, f.read_bytes(), who=a.by)
                    print(f"  {'added' if new else 'already there'}: #{doc['id']} {f.name}")
                except documents.DocumentError as exc:
                    print(f"  refused: {f.name}: {exc}")
            print("Indexing runs in the background with the server, or now with: "
                  "scripts\\kb.py index")
        elif a.command == "index":
            if not get_settings().qdrant_url:
                print("QDRANT_URL is not set: nothing can be indexed.")
                return 1
            print("Indexing (a few seconds to minutes per document) ...")
            done, failed = documents.index_pending(limit=100)
            print(f"Indexed {done}, failed {failed}.")
            _show(None)
        elif a.command == "retry":
            documents.retry(int(a.args[0]), who=a.by)
            print("Back to pending.")
        elif a.command == "delete":
            documents.delete(int(a.args[0]), who=a.by)
            print("Deleted.")
        elif a.command == "search":
            code, question = a.args[0].upper(), " ".join(a.args[1:])
            why = retriever.unavailable(code)
            if why:
                print(f"Cannot search {code}: {why}")
                return 1
            r = retriever.retrieve(question, code)
            print(f"{r.verdict.value}, {len(r.evidence)} passage(s), {r.elapsed_ms / 1000:.1f} s"
                  + (f", error: {r.error}" if r.error else ""))
            for e in r.evidence:
                print(f"\n  [{e.relation}] {e.document}" + (f" / {e.section}" if e.section else ""))
                print("  " + e.text.strip()[:500].replace("\n", "\n  "))
        elif a.command == "scopes":
            code = None if a.args[0].upper() == "ALL" else a.args[0]
            if len(a.args) > 1:
                f = Path(a.args[1])
                scopes.save(code, f.read_bytes(), who=a.by, filename=f.name)
                print(f"Saved for {code or 'every product'}.")
            row = scopes.current(code)
            print(f"\nIn use for {code or 'every product'}: "
                  + (f"version {row['versions']}, {row['filename']}, by {row['uploaded_by']}\n\n"
                     + row["content"] if row else "none"))
        return 0
    except (ValueError, IndexError) as exc:
        print(f"Not done: {exc}")
        return 1
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
