"""
setup_rankings_and_schemas.py

PART 1 — Download and load atp_players + atp_rankings into DuckDB
PART 2 — Reorganize all tables into schemas: core, charting, rankings
"""

import os
import sys
import ssl
import urllib.request
import urllib.error
import duckdb
from pathlib import Path

DB_PATH = "data/processed/tennis.duckdb"
RAW_ATP_DIR = Path("data/raw/tennis_atp")

# ── Files to download ────────────────────────────────────────────────────────
DOWNLOADS = {
    "atp_players.csv":           "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_players.csv",
    "atp_rankings_current.csv":  "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_rankings_current.csv",
    "atp_rankings_00s.csv":      "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_rankings_00s.csv",
    "atp_rankings_10s.csv":      "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_rankings_10s.csv",
    "atp_rankings_20s.csv":      "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_rankings_20s.csv",
    "atp_rankings_90s.csv":      "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_rankings_90s.csv",
    "atp_rankings_80s.csv":      "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_rankings_80s.csv",
    "atp_rankings_70s.csv":      "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_rankings_70s.csv",
}

# ── Schema assignments ────────────────────────────────────────────────────────
SCHEMA_MAP = {
    "core": [
        "atp_matches",
        "wta_matches",
        "atp_points",
        "wta_points",
        "atp_points_enhanced",
    ],
    # charting tables detected dynamically (anything starting with "charting_")
    "rankings": [
        "atp_players",
        "atp_rankings",
    ],
}


def hr(char="─", width=70):
    print(char * width)


# ─────────────────────────────────────────────────────────────────────────────
# PART 1A — Download files
# ─────────────────────────────────────────────────────────────────────────────
def download_files():
    hr()
    print("PART 1A — Downloading CSV files")
    hr()
    RAW_ATP_DIR.mkdir(parents=True, exist_ok=True)

    # SSL context that bypasses cert verification (needed on macOS without certs installed)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    downloaded = []
    for filename, url in DOWNLOADS.items():
        dest = RAW_ATP_DIR / filename
        if dest.exists():
            size_kb = dest.stat().st_size / 1024
            print(f"  {filename} already exists ({size_kb:,.1f} KB) — skipping download")
            downloaded.append(str(dest))
            continue
        try:
            print(f"  Downloading {filename} ... ", end="", flush=True)
            req = urllib.request.urlopen(url, context=ssl_ctx)
            dest.write_bytes(req.read())
            size_kb = dest.stat().st_size / 1024
            print(f"✓  ({size_kb:,.1f} KB)")
            downloaded.append(str(dest))
        except urllib.error.URLError as e:
            print(f"✗  FAILED: {e}")
        except Exception as e:
            print(f"✗  ERROR: {e}")

    return downloaded


# ─────────────────────────────────────────────────────────────────────────────
# PART 1B — Load atp_players and atp_rankings into DuckDB (main schema for now)
# ─────────────────────────────────────────────────────────────────────────────
def load_rankings(con):
    hr()
    print("PART 1B — Loading atp_players and atp_rankings into DuckDB (main)")
    hr()

    # ── atp_players ──────────────────────────────────────────────────────────
    players_path = str(RAW_ATP_DIR / "atp_players.csv")
    if not Path(players_path).exists():
        print("  ✗  atp_players.csv not found — skipping")
    else:
        print("  Loading atp_players ... ", end="", flush=True)
        con.execute("DROP TABLE IF EXISTS main.atp_players")
        con.execute(f"""
            CREATE TABLE main.atp_players AS
            SELECT * FROM read_csv('{players_path}', header=true, all_varchar=true)
        """)
        count = con.execute("SELECT COUNT(*) FROM main.atp_players").fetchone()[0]
        print(f"✓  {count:,} rows")

        print("\n  Sample — atp_players (3 rows):")
        rows = con.execute("SELECT * FROM main.atp_players LIMIT 3").fetchall()
        cols = [d[0] for d in con.execute("SELECT * FROM main.atp_players LIMIT 0").description]
        print("  " + " | ".join(cols))
        print("  " + "-" * 80)
        for r in rows:
            print("  " + " | ".join(str(v) for v in r))

    # ── atp_rankings (combine all ranking files) ──────────────────────────────
    ranking_glob = str(RAW_ATP_DIR / "atp_rankings_*.csv")
    ranking_files = sorted(Path(RAW_ATP_DIR).glob("atp_rankings_*.csv"))
    if not ranking_files:
        print("\n  ✗  No atp_rankings_*.csv files found — skipping")
    else:
        print(f"\n  Combining {len(ranking_files)} ranking files:")
        for f in ranking_files:
            print(f"    · {f.name}")

        print("\n  Loading atp_rankings ... ", end="", flush=True)
        con.execute("DROP TABLE IF EXISTS main.atp_rankings")
        con.execute(f"""
            CREATE TABLE main.atp_rankings AS
            SELECT
                CAST(ranking_date AS VARCHAR) AS ranking_date,
                rank                          AS ranking,
                player                        AS player_id,
                points                        AS ranking_points
            FROM read_csv('{ranking_glob}', header=true, union_by_name=true)
        """)
        count = con.execute("SELECT COUNT(*) FROM main.atp_rankings").fetchone()[0]
        print(f"✓  {count:,} rows")

        print("\n  Sample — atp_rankings (3 rows):")
        rows = con.execute(
            "SELECT * FROM main.atp_rankings ORDER BY ranking_date DESC LIMIT 3"
        ).fetchall()
        cols = [d[0] for d in con.execute("SELECT * FROM main.atp_rankings LIMIT 0").description]
        print("  " + " | ".join(cols))
        print("  " + "-" * 80)
        for r in rows:
            print("  " + " | ".join(str(v) for v in r))


# ─────────────────────────────────────────────────────────────────────────────
# PART 2 — Reorganize tables into schemas
# ─────────────────────────────────────────────────────────────────────────────
def reorganize_schemas(con):
    hr()
    print("PART 2 — Reorganizing tables into schemas")
    hr()

    # Discover all current main.* tables
    all_tables = [
        r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
    ]

    # Build charting list dynamically
    charting_tables = [t for t in all_tables if t.startswith("charting_")]
    SCHEMA_MAP["charting"] = charting_tables

    # Create schemas
    for schema in ["core", "charting", "rankings"]:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        print(f"  Schema '{schema}' ready")

    print()

    # Move each table
    total_moved = 0
    for schema, tables in SCHEMA_MAP.items():
        print(f"  ── Moving into '{schema}' ──")
        for table in tables:
            if table not in all_tables:
                print(f"    ⚠  main.{table} not found — skipping")
                continue

            src = f"main.{table}"
            dst = f"{schema}.{table}"

            try:
                # Count source rows
                src_count = con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]

                # Create in target schema
                con.execute(f"DROP TABLE IF EXISTS {dst}")
                con.execute(f"CREATE TABLE {dst} AS SELECT * FROM {src}")

                # Verify row count matches
                dst_count = con.execute(f"SELECT COUNT(*) FROM {dst}").fetchone()[0]
                if src_count != dst_count:
                    print(f"    ✗  {table}: row count mismatch ({src_count} → {dst_count}) — NOT dropping source")
                    continue

                # Safe to drop source
                con.execute(f"DROP TABLE {src}")
                print(f"    ✓  {table}  ({dst_count:,} rows)")
                total_moved += 1

            except Exception as e:
                print(f"    ✗  {table}: ERROR — {e}")

    print(f"\n  Total tables moved: {total_moved}")


# ─────────────────────────────────────────────────────────────────────────────
# Final verification — list all tables by schema
# ─────────────────────────────────────────────────────────────────────────────
def print_schema_summary(con):
    hr()
    print("FINAL — All tables by schema")
    hr()

    rows = con.execute("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
        ORDER BY table_schema, table_name
    """).fetchall()

    current_schema = None
    for schema, table in rows:
        if schema != current_schema:
            if current_schema is not None:
                print()
            print(f"  [{schema}]")
            current_schema = schema
        print(f"    · {table}")

    # Anything left in main?
    main_tables = [t for s, t in rows if s == "main"]
    if not main_tables:
        print("\n  ✓  main schema is clean (no user tables remaining)")
    else:
        print(f"\n  ⚠  {len(main_tables)} table(s) still in main: {main_tables}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print()
    hr("═")
    print("  Tennis Engine — Rankings Load + Schema Reorganization")
    hr("═")
    print()

    # PART 1A — Download
    download_files()
    print()

    # Connect to DB
    con = duckdb.connect(DB_PATH)
    print(f"  Connected to {DB_PATH}")
    print()

    try:
        # PART 1B — Load
        load_rankings(con)
        print()

        # PART 2 — Reorganize
        reorganize_schemas(con)
        print()

        # Summary
        print_schema_summary(con)

    finally:
        con.close()
        print()
        hr("═")
        print("  Done.")
        hr("═")
        print()
