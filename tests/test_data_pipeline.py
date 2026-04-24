"""
Integration tests for src/data_pipeline.py.

Each test creates a minimal temporary CSV structure, runs the pipeline
against it, and verifies the resulting DuckDB database.
"""

import sys
from pathlib import Path

import duckdb
import pytest

from src.pipeline.data_pipeline import run_pipeline  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal CSV content — one header row + one data row per file.
# ---------------------------------------------------------------------------

ATP_MATCH_CSV = (
    "tourney_id,tourney_name,surface,tourney_date,winner_name,loser_name,score\n"
    "2020-001,Test Open,Hard,20200101,Player A,Player B,6-3 6-4\n"
)

WTA_MATCH_CSV = (
    "tourney_id,tourney_name,surface,tourney_date,winner_name,loser_name,score\n"
    "2020-W01,Test WTA,Clay,20200601,Player C,Player D,6-2 6-1\n"
)

CHARTING_M_POINTS_CSV = (
    "match_id,Pt,Set1,Set2,Gm1,Gm2,Pts,Svr,Ret,Notes\n"
    "20200101-M-001,1,0,0,0,0,0,1,2,\n"
)

CHARTING_W_POINTS_CSV = (
    "match_id,Pt,Set1,Set2,Gm1,Gm2,Pts,Svr,Ret,Notes\n"
    "20200601-W-001,1,0,0,0,0,0,1,2,\n"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_data(tmp_path: Path) -> dict:
    """
    Build a minimal Sackmann-like directory tree under tmp_path and return
    the (db_path, tables) config needed by run_pipeline.
    """
    atp_dir = tmp_path / "tennis_atp"
    wta_dir = tmp_path / "tennis_wta"
    charting_dir = tmp_path / "tennis_MatchChartingProject"
    atp_dir.mkdir()
    wta_dir.mkdir()
    charting_dir.mkdir()

    (atp_dir / "atp_matches_2020.csv").write_text(ATP_MATCH_CSV)
    (wta_dir / "wta_matches_2020.csv").write_text(WTA_MATCH_CSV)
    (charting_dir / "charting-m-points-2020.csv").write_text(CHARTING_M_POINTS_CSV)
    (charting_dir / "charting-w-points-2020.csv").write_text(CHARTING_W_POINTS_CSV)

    db_path = str(tmp_path / "processed" / "tennis.duckdb")

    tables = [
        (str(atp_dir / "atp_matches_????.csv"), "atp_matches"),
        (str(wta_dir / "wta_matches_????.csv"), "wta_matches"),
        (str(charting_dir / "charting-m-points*.csv"), "atp_points"),
        (str(charting_dir / "charting-w-points*.csv"), "wta_points"),
    ]

    return {"db_path": db_path, "tables": tables}


@pytest.fixture()
def loaded_db(sample_data: dict) -> dict:
    """Run the pipeline once and return sample_data for assertions."""
    run_pipeline(db_path=sample_data["db_path"], tables=sample_data["tables"])
    return sample_data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_database_file_exists(loaded_db: dict) -> None:
    """The DuckDB file must be created at the specified path."""
    assert Path(loaded_db["db_path"]).exists(), "DuckDB file was not created"


def test_all_tables_exist(loaded_db: dict) -> None:
    """atp_matches, wta_matches, and atp_points must all be present."""
    con = duckdb.connect(loaded_db["db_path"])
    existing = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    con.close()

    expected = {"atp_matches", "wta_matches", "atp_points", "wta_points"}
    missing = expected - existing
    assert not missing, f"Missing tables: {missing}"


@pytest.mark.parametrize("table_name", ["atp_matches", "wta_matches", "atp_points", "wta_points"])
def test_table_has_rows(loaded_db: dict, table_name: str) -> None:
    """Each table must contain at least one row."""
    con = duckdb.connect(loaded_db["db_path"])
    count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
    con.close()

    assert count > 0, f"{table_name} is empty"


def test_run_pipeline_returns_summary(sample_data: dict) -> None:
    """run_pipeline should return a dict with an entry for every loaded table."""
    results = run_pipeline(
        db_path=sample_data["db_path"],
        tables=sample_data["tables"],
    )

    assert set(results.keys()) == {"atp_matches", "wta_matches", "atp_points", "wta_points"}
    for table_name, (rows, cols) in results.items():
        assert rows > 0, f"{table_name}: expected rows > 0, got {rows}"
        assert cols > 0, f"{table_name}: expected cols > 0, got {cols}"


def test_no_files_skipped_gracefully(tmp_path: Path) -> None:
    """Pipeline should return an empty dict when no CSVs are found, not raise."""
    db_path = str(tmp_path / "empty.duckdb")
    tables = [(str(tmp_path / "nonexistent_????.csv"), "ghost_table")]

    results = run_pipeline(db_path=db_path, tables=tables)

    assert results == {}, "Expected empty results when no files match"
