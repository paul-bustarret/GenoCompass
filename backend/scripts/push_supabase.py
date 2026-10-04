"""Load exports/supabase/ into a Postgres (Supabase) database in one transaction.

Run:  uv run --extra supabase python scripts/push_supabase.py

Reads the connection string from SUPABASE_DB_URL (never printed). Applies schema.sql (idempotent),
then for every table: TRUNCATE + COPY from its CSV. Any error rolls the whole load back.
Regenerate the files first with `uv run python -m atlas.export`.
"""
import os
import re
import sys
from pathlib import Path

SUPA = Path(__file__).resolve().parent.parent / "exports" / "supabase"
# load order (no foreign keys, so order only matters for readability)
TABLES = ["nodes", "edges", "coverage", "similarity", "clusters", "journeys", "subgraphs", "emails"]


def main() -> int:
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL is not set. Export the Postgres connection string "
                 "(Supabase: Project settings -> Database -> Connection string) and rerun.")
    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        sys.exit("psycopg is not installed: run with `uv run --extra supabase python scripts/push_supabase.py`.")
    schema = (SUPA / "schema.sql").read_text()
    for t in TABLES:
        if not (SUPA / f"{t}.csv").exists():
            sys.exit(f"missing {SUPA / f'{t}.csv'}; run `uv run python -m atlas.export` first.")
        if not re.search(rf"create table if not exists public\.{t} \(", schema):
            sys.exit(f"schema.sql has no table {t}")

    with psycopg.connect(url) as conn:  # commits on clean exit, rolls back on exception
        with conn.cursor() as cur:
            cur.execute(schema)
            cur.execute("truncate " + ", ".join(f"public.{t}" for t in TABLES))
            for t in TABLES:
                path = SUPA / f"{t}.csv"
                with open(path, encoding="utf-8") as f:
                    header = f.readline().strip()
                    with cur.copy(f"copy public.{t} ({header}) from stdin with (format csv)") as cp:
                        while chunk := f.read(1 << 20):
                            cp.write(chunk)
                cur.execute(f"select count(*) from public.{t}")
                print(f"{t}: {cur.fetchone()[0]} rows")
    print("done (committed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
