from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Iterable

import pandas as pd


SOURCE_SHEET = "Valid ICD10 FY2026 & NF Exclude"
EXPECTED_COLUMNS = {
    "CODE": "code",
    "SHORT DESCRIPTION (VALID ICD-10 FY2026)": "short_description",
    "LONG DESCRIPTION (VALID ICD-10 FY2026)": "long_description",
    "NF EXCL": "nf_excl",
}
ALIAS_FILE = Path("diagnosis_aliases.json")
NORMALIZE_PATTERN = re.compile(r"[^a-z0-9]+")
AZURE_DATA_DIR = Path("api/data")


def normalize_text(value: str) -> str:
    return NORMALIZE_PATTERN.sub(" ", value.lower()).strip()


def load_aliases(alias_path: Path) -> list[dict[str, str]]:
    payload = json.loads(alias_path.read_text(encoding="utf-8"))
    aliases: list[dict[str, str]] = []

    for row in payload:
        alias = str(row["alias"]).strip()
        canonical_text = str(row["canonical_text"]).strip()
        preferred_code = str(row.get("preferred_code", "")).strip().upper()
        notes = str(row.get("notes", "")).strip()
        if not alias or not canonical_text:
            continue

        aliases.append(
            {
                "alias": alias,
                "normalized_alias": normalize_text(alias),
                "canonical_text": canonical_text,
                "normalized_canonical_text": normalize_text(canonical_text),
                "preferred_code": preferred_code,
                "notes": notes,
                "source": alias_path.name,
            }
        )

    aliases.sort(key=lambda item: (-len(item["normalized_alias"]), item["alias"]))
    return aliases


def build_searchable_text(
    code: str,
    short_description: str,
    long_description: str,
    aliases: Iterable[dict[str, str]],
) -> str:
    normalized_blob = normalize_text(f"{short_description} {long_description}")
    extra_terms: list[str] = []

    for alias in aliases:
        canonical = alias["normalized_canonical_text"]
        if canonical and canonical in normalized_blob:
            extra_terms.append(alias["alias"])
            if alias["preferred_code"] and alias["preferred_code"] == code:
                extra_terms.append(f"{alias['alias']} default")

    parts = [code, short_description, long_description]
    if extra_terms:
        parts.append(" ".join(extra_terms))
    return " ".join(part for part in parts if part).strip()


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = MEMORY;
        PRAGMA synchronous = OFF;

        DROP TABLE IF EXISTS icd10_entries;
        DROP TABLE IF EXISTS diagnosis_aliases;
        DROP TABLE IF EXISTS icd10_fts;

        CREATE TABLE icd10_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            short_description TEXT NOT NULL,
            long_description TEXT NOT NULL,
            searchable_text TEXT NOT NULL,
            nf_excl TEXT,
            source_file TEXT NOT NULL
        );

        CREATE TABLE diagnosis_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            canonical_text TEXT NOT NULL,
            normalized_canonical_text TEXT NOT NULL,
            preferred_code TEXT,
            notes TEXT,
            source TEXT NOT NULL
        );

        CREATE INDEX idx_icd10_code ON icd10_entries (code);
        CREATE INDEX idx_alias_lookup ON diagnosis_aliases (normalized_alias);

        CREATE VIRTUAL TABLE icd10_fts USING fts5(
            code,
            short_description,
            long_description,
            searchable_text,
            content='icd10_entries',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )


def load_primary_sheet(excel_path: Path) -> pd.DataFrame:
    frame = pd.read_excel(excel_path, sheet_name=SOURCE_SHEET)
    missing = [column for column in EXPECTED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Missing expected columns in '{SOURCE_SHEET}': {missing}")

    frame = frame.rename(columns=EXPECTED_COLUMNS)
    frame = frame[list(EXPECTED_COLUMNS.values())].copy()
    frame = frame.fillna("")

    for column in frame.columns:
        frame[column] = frame[column].astype(str).str.strip()

    frame = frame[frame["code"] != ""].copy()
    frame["code"] = frame["code"].str.upper()
    frame["source_file"] = excel_path.name
    return frame


def export_postgres_seed_files(
    frame: pd.DataFrame,
    aliases: list[dict[str, str]],
    export_dir: Path,
) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(export_dir / "icd10_entries.csv", index=False)
    pd.DataFrame(aliases).to_csv(export_dir / "diagnosis_aliases.csv", index=False)


def export_azure_seed_files(
    frame: pd.DataFrame,
    aliases: list[dict[str, str]],
    export_dir: Path,
) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)

    slim_rows = frame[
        [
            "code",
            "short_description",
            "long_description",
            "searchable_text",
        ]
    ].to_dict(orient="records")

    with (export_dir / "icd10_entries.ndjson").open("w", encoding="utf-8") as handle:
        for row in slim_rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")

    (export_dir / "diagnosis_aliases.json").write_text(
        json.dumps(aliases, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def import_excel(
    excel_path: Path,
    db_path: Path,
    alias_path: Path,
    export_dir: Path,
    azure_export_dir: Path,
) -> tuple[int, int]:
    aliases = load_aliases(alias_path)
    frame = load_primary_sheet(excel_path)
    frame["searchable_text"] = frame.apply(
        lambda row: build_searchable_text(
            row["code"],
            row["short_description"],
            row["long_description"],
            aliases,
        ),
        axis=1,
    )
    frame = frame[
        [
            "code",
            "short_description",
            "long_description",
            "searchable_text",
            "nf_excl",
            "source_file",
        ]
    ].copy()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    export_postgres_seed_files(frame, aliases, export_dir)
    export_azure_seed_files(frame, aliases, azure_export_dir)

    connection = sqlite3.connect(db_path)
    try:
        create_schema(connection)
        frame.to_sql("icd10_entries", connection, if_exists="append", index=False)
        pd.DataFrame(aliases).to_sql("diagnosis_aliases", connection, if_exists="append", index=False)
        connection.execute(
            """
            INSERT INTO icd10_fts(rowid, code, short_description, long_description, searchable_text)
            SELECT id, code, short_description, long_description, searchable_text
            FROM icd10_entries
            """
        )
        connection.commit()
        row_count = connection.execute("SELECT COUNT(*) FROM icd10_entries").fetchone()[0]
        alias_count = connection.execute("SELECT COUNT(*) FROM diagnosis_aliases").fetchone()[0]
        return int(row_count), int(alias_count)
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import ICD-10 Excel data into a local search database.")
    parser.add_argument("excel_path", help="Path to the source Excel file")
    parser.add_argument(
        "--db-path",
        default="data/icd10_runtime.sqlite3",
        help="Destination SQLite database path",
    )
    parser.add_argument(
        "--alias-path",
        default=str(ALIAS_FILE),
        help="JSON file containing diagnosis aliases and preferred-code hints",
    )
    parser.add_argument(
        "--export-dir",
        default="data/postgres_seed",
        help="Directory where Postgres-ready CSV seed files are written",
    )
    parser.add_argument(
        "--azure-export-dir",
        default=str(AZURE_DATA_DIR),
        help="Directory where Azure Static Web Apps API seed files are written",
    )
    args = parser.parse_args()

    excel_path = Path(args.excel_path).expanduser().resolve()
    db_path = Path(args.db_path).expanduser().resolve()
    alias_path = Path(args.alias_path).expanduser().resolve()
    export_dir = Path(args.export_dir).expanduser().resolve()
    azure_export_dir = Path(args.azure_export_dir).expanduser().resolve()

    if not excel_path.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_path}")
    if not alias_path.exists():
        raise FileNotFoundError(f"Alias file not found: {alias_path}")

    count, alias_count = import_excel(excel_path, db_path, alias_path, export_dir, azure_export_dir)
    print(f"Imported {count} ICD-10 rows into {db_path}")
    print(f"Loaded {alias_count} diagnosis aliases from {alias_path}")
    print(f"Wrote Postgres seed CSV files to {export_dir}")
    print(f"Wrote Azure API seed files to {azure_export_dir}")


if __name__ == "__main__":
    main()
