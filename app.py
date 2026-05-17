from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse


DB_PATH = Path("data/icd10_runtime.sqlite3")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")
NORMALIZE_PATTERN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class AliasRule:
    alias: str
    normalized_alias: str
    canonical_text: str
    normalized_canonical_text: str
    preferred_code: str
    notes: str


@dataclass
class SearchResult:
    code: str
    short_description: str
    long_description: str
    score: float


def get_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def normalize_text(value: str) -> str:
    return NORMALIZE_PATTERN.sub(" ", value.lower()).strip()


def normalize_query(user_query: str) -> str:
    return " ".join(user_query.strip().split())


def normalize_code(value: str) -> str:
    return "".join(TOKEN_PATTERN.findall(value.upper()))


def build_fts_query(user_query: str) -> str:
    tokens = TOKEN_PATTERN.findall(user_query.lower())
    if not tokens:
        return ""

    prefix_terms = [f"{token}*" for token in tokens]
    if len(tokens) == 1:
        return prefix_terms[0]

    phrase = " ".join(tokens).replace('"', '""')
    return f'"{phrase}" OR ' + " AND ".join(prefix_terms)


@lru_cache(maxsize=4)
def load_alias_rules(db_path_str: str) -> tuple[AliasRule, ...]:
    db_path = Path(db_path_str)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                alias,
                normalized_alias,
                canonical_text,
                normalized_canonical_text,
                COALESCE(preferred_code, '') AS preferred_code,
                COALESCE(notes, '') AS notes
            FROM diagnosis_aliases
            ORDER BY length(normalized_alias) DESC, alias ASC
            """
        ).fetchall()

    return tuple(
        AliasRule(
            alias=row["alias"],
            normalized_alias=row["normalized_alias"],
            canonical_text=row["canonical_text"],
            normalized_canonical_text=row["normalized_canonical_text"],
            preferred_code=row["preferred_code"],
            notes=row["notes"],
        )
        for row in rows
    )


def alias_regex(normalized_alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])")


def resolve_aliases(user_query: str, alias_rules: Iterable[AliasRule]) -> tuple[set[str], set[str], tuple[AliasRule, ...]]:
    normalized_query = normalize_text(user_query)
    variants = {normalize_query(user_query)}
    preferred_codes: set[str] = set()
    matched_rules: list[AliasRule] = []

    if not normalized_query:
        return variants, preferred_codes, tuple()

    for rule in alias_rules:
        matcher = alias_regex(rule.normalized_alias)
        if not matcher.search(normalized_query):
            continue

        matched_rules.append(rule)
        if rule.preferred_code:
            preferred_codes.add(rule.preferred_code)
        variants.add(rule.canonical_text)
        expanded_variant = matcher.sub(rule.normalized_canonical_text, normalized_query)
        if expanded_variant:
            variants.add(expanded_variant)

    return variants, preferred_codes, tuple(matched_rules)


def fetch_candidates(connection: sqlite3.Connection, search_text: str, limit: int = 120) -> list[sqlite3.Row]:
    fts_query = build_fts_query(search_text)
    results: list[sqlite3.Row] = []

    if fts_query:
        results = connection.execute(
            """
            SELECT
                e.code,
                e.short_description,
                e.long_description,
                e.searchable_text,
                bm25(icd10_fts, 10.0, 6.0, 4.0, 2.0) AS fts_rank
            FROM icd10_fts
            JOIN icd10_entries e ON e.id = icd10_fts.rowid
            WHERE icd10_fts MATCH :fts_query
            ORDER BY fts_rank ASC
            LIMIT :limit
            """,
            {"fts_query": fts_query, "limit": limit},
        ).fetchall()

    if results:
        return results

    contains = f"%{normalize_text(search_text)}%"
    return connection.execute(
        """
        SELECT
            code,
            short_description,
            long_description,
            searchable_text,
            999.0 AS fts_rank
        FROM icd10_entries
        WHERE lower(searchable_text) LIKE :contains
        ORDER BY length(long_description) ASC
        LIMIT :limit
        """,
        {"contains": contains, "limit": limit},
    ).fetchall()


def fetch_rows_by_codes(connection: sqlite3.Connection, codes: Iterable[str]) -> list[sqlite3.Row]:
    code_list = [code for code in codes if code]
    if not code_list:
        return []

    placeholders = ", ".join("?" for _ in code_list)
    return connection.execute(
        f"""
        SELECT
            code,
            short_description,
            long_description,
            searchable_text,
            999.0 AS fts_rank
        FROM icd10_entries
        WHERE code IN ({placeholders})
        """,
        code_list,
    ).fetchall()


def token_matches(query_token: str, candidate_token: str) -> bool:
    if query_token == candidate_token:
        return True
    if len(query_token) >= 4 and len(candidate_token) >= 4:
        if query_token.startswith(candidate_token) or candidate_token.startswith(query_token):
            return True
    if len(query_token) >= 5 and len(candidate_token) >= 5:
        if query_token[:3] != candidate_token[:3]:
            return False
        return SequenceMatcher(None, query_token, candidate_token).ratio() >= 0.82
    return False


def token_overlap_ratio(query_tokens: list[str], candidate_tokens: list[str]) -> float:
    if not query_tokens:
        return 0.0
    matches = 0
    for query_token in query_tokens:
        if any(token_matches(query_token, candidate_token) for candidate_token in candidate_tokens):
            matches += 1
    return matches / len(query_tokens)


def compute_variant_score(
    row: sqlite3.Row,
    variant: str,
    original_normalized_query: str,
    preferred_codes: set[str],
    matched_rules: tuple[AliasRule, ...],
) -> float:
    variant_normalized = normalize_text(variant)
    query_code = normalize_code(variant)
    code_normalized = normalize_code(row["code"])
    short_normalized = normalize_text(row["short_description"])
    long_normalized = normalize_text(row["long_description"])
    searchable_normalized = normalize_text(row["searchable_text"])
    alias_only_query = any(rule.normalized_alias == original_normalized_query for rule in matched_rules)

    score = 0.0

    if query_code and query_code == code_normalized:
        score += 260.0
    if variant_normalized == long_normalized:
        score += 225.0
    if variant_normalized == short_normalized:
        score += 205.0
    if long_normalized.startswith(variant_normalized):
        score += 125.0
    if short_normalized.startswith(variant_normalized):
        score += 105.0
    if variant_normalized and variant_normalized in long_normalized:
        score += 65.0
    if variant_normalized and variant_normalized in short_normalized:
        score += 55.0
    if variant_normalized and variant_normalized in searchable_normalized:
        score += 35.0

    variant_tokens = TOKEN_PATTERN.findall(variant_normalized)
    searchable_tokens = TOKEN_PATTERN.findall(searchable_normalized)
    score += token_overlap_ratio(variant_tokens, searchable_tokens) * 80.0
    score += token_overlap_ratio(TOKEN_PATTERN.findall(original_normalized_query), searchable_tokens) * 35.0

    sequence_ratio = max(
        SequenceMatcher(None, variant_normalized, long_normalized).ratio(),
        SequenceMatcher(None, variant_normalized, short_normalized).ratio(),
    )
    score += sequence_ratio * 25.0

    fts_rank = float(row["fts_rank"] or 999.0)
    score += max(0.0, 18.0 - min(18.0, fts_rank))

    if row["code"] in preferred_codes:
        score += 260.0 if alias_only_query else 0.0

    if matched_rules:
        for rule in matched_rules:
            if row["code"] == rule.preferred_code and rule.normalized_alias == original_normalized_query:
                score += 35.0
            if rule.normalized_canonical_text and rule.normalized_canonical_text in long_normalized:
                score += 8.0

    return score


def search_icd10(connection: sqlite3.Connection, db_path: Path, user_query: str, limit: int = 20) -> list[SearchResult]:
    cleaned = normalize_query(user_query)
    if not cleaned:
        return []

    alias_rules = load_alias_rules(str(db_path.resolve()))
    variants, preferred_codes, matched_rules = resolve_aliases(cleaned, alias_rules)

    candidate_map: dict[str, tuple[sqlite3.Row, float]] = {}
    normalized_original = normalize_text(cleaned)

    for variant in variants:
        for row in fetch_candidates(connection, variant):
            score = compute_variant_score(row, variant, normalized_original, preferred_codes, matched_rules)
            existing = candidate_map.get(row["code"])
            if existing is None or score > existing[1]:
                candidate_map[row["code"]] = (row, score)

    for row in fetch_rows_by_codes(connection, preferred_codes):
        score = max(
            compute_variant_score(row, variant, normalized_original, preferred_codes, matched_rules)
            for variant in variants
        )
        existing = candidate_map.get(row["code"])
        if existing is None or score > existing[1]:
            candidate_map[row["code"]] = (row, score)

    ranked = sorted(
        candidate_map.values(),
        key=lambda item: (
            -item[1],
            len(item[0]["long_description"]),
            item[0]["code"],
        ),
    )

    return [
        SearchResult(
            code=row["code"],
            short_description=row["short_description"],
            long_description=row["long_description"],
            score=round(score, 3),
        )
        for row, score in ranked[:limit]
    ]


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ICD-10 Diagnosis Search</title>
  <style>
    :root {
      --bg: #f4efe5;
      --panel: #fffdf9;
      --ink: #1d2a36;
      --muted: #5f6c78;
      --accent: #0d6b78;
      --accent-soft: #d7eef1;
      --border: #d5d7d2;
      --shadow: 0 18px 50px rgba(29, 42, 54, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(13, 107, 120, 0.14), transparent 28%),
        linear-gradient(180deg, #f8f4eb 0%, var(--bg) 100%);
      min-height: 100vh;
    }
    .shell {
      max-width: 1024px;
      margin: 0 auto;
      padding: 48px 20px 64px;
    }
    .hero {
      background: var(--panel);
      border: 1px solid rgba(213, 215, 210, 0.9);
      border-radius: 28px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .hero-top {
      padding: 36px 32px 18px;
      background:
        linear-gradient(135deg, rgba(13, 107, 120, 0.1), rgba(13, 107, 120, 0.02)),
        linear-gradient(90deg, rgba(255,255,255,0.85), rgba(255,255,255,0.98));
    }
    h1 {
      margin: 0 0 12px;
      font-size: clamp(2rem, 4vw, 3.2rem);
      line-height: 1;
      letter-spacing: -0.03em;
    }
    p {
      margin: 0;
      color: var(--muted);
      font-size: 1.05rem;
      line-height: 1.6;
    }
    .search-wrap {
      padding: 28px 32px 32px;
    }
    form {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
    }
    input {
      width: 100%;
      padding: 16px 18px;
      border-radius: 16px;
      border: 1px solid var(--border);
      background: #fff;
      font-size: 1rem;
      color: var(--ink);
      outline: none;
    }
    input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 4px rgba(13, 107, 120, 0.12);
    }
    button {
      border: 0;
      border-radius: 16px;
      background: var(--accent);
      color: #fff;
      padding: 0 22px;
      font-size: 1rem;
      font-weight: 700;
      cursor: pointer;
    }
    button:hover { filter: brightness(0.95); }
    .hint {
      margin-top: 12px;
      font-size: 0.95rem;
      color: var(--muted);
    }
    .results {
      margin-top: 24px;
      display: grid;
      gap: 14px;
    }
    .card {
      background: rgba(255, 255, 255, 0.78);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 18px;
    }
    .card-top {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px;
      margin-bottom: 10px;
    }
    .code {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      padding: 6px 12px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }
    .short {
      font-size: 1.06rem;
      font-weight: 700;
    }
    .long {
      color: var(--muted);
      line-height: 1.6;
    }
    .empty, .loading {
      padding: 18px;
      border-radius: 18px;
      background: rgba(255,255,255,0.65);
      border: 1px dashed var(--border);
      color: var(--muted);
    }
    .examples {
      margin-top: 18px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .pill {
      border: 1px solid var(--border);
      background: white;
      border-radius: 999px;
      padding: 8px 12px;
      color: var(--muted);
      cursor: pointer;
    }
    .pill:hover {
      border-color: var(--accent);
      color: var(--accent);
    }
    @media (max-width: 720px) {
      .shell { padding-top: 28px; }
      .hero-top, .search-wrap { padding-left: 20px; padding-right: 20px; }
      form { grid-template-columns: 1fr; }
      button { padding: 15px 18px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="hero-top">
        <h1>ICD-10 Diagnosis Search</h1>
        <p>Search by diagnosis wording, code fragments, or common aliases. The ranking engine expands shortcuts like HTN and DM2, then returns the closest ICD-10 matches first.</p>
      </div>
      <div class="search-wrap">
        <form id="search-form">
          <input id="query" name="query" type="text" placeholder="Example: dm2 neuropathy or high blood pressure" autocomplete="off">
          <button type="submit">Search</button>
        </form>
        <div class="hint">Wildcard-style matching is supported. Try partial phrases like "cholera vib", aliases like "HTN", or mixed terms like "dm2 neuropathy".</div>
        <div class="examples">
          <button class="pill" type="button" data-query="HTN">HTN</button>
          <button class="pill" type="button" data-query="DM2 neuropathy">DM2 neuropathy</button>
          <button class="pill" type="button" data-query="high blood pressure">high blood pressure</button>
          <button class="pill" type="button" data-query="cholera vib">cholera vib</button>
        </div>
        <section id="results" class="results">
          <div class="empty">Enter a diagnosis description to look up the best ICD-10 codes.</div>
        </section>
      </div>
    </section>
  </main>
  <script>
    const form = document.getElementById("search-form");
    const queryInput = document.getElementById("query");
    const resultsEl = document.getElementById("results");
    const exampleButtons = document.querySelectorAll("[data-query]");

    function escapeHtml(value) {
      return value
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function renderResults(items, query) {
      if (!items.length) {
        resultsEl.innerHTML = `<div class="empty">No ICD-10 matches were found for <strong>${escapeHtml(query)}</strong>.</div>`;
        return;
      }

      resultsEl.innerHTML = items.map((item, index) => `
        <article class="card">
          <div class="card-top">
            <span class="code">${escapeHtml(item.code)}</span>
            <div class="short">${escapeHtml(item.short_description)}${index === 0 ? " (best match)" : ""}</div>
          </div>
          <div class="long">${escapeHtml(item.long_description)}</div>
        </article>
      `).join("");
    }

    async function runSearch(query) {
      const trimmed = query.trim();
      if (!trimmed) {
        resultsEl.innerHTML = '<div class="empty">Enter a diagnosis description to look up the best ICD-10 codes.</div>';
        return;
      }

      resultsEl.innerHTML = '<div class="loading">Searching ICD-10 database...</div>';
      const response = await fetch(`/api/search?q=${encodeURIComponent(trimmed)}`);
      const payload = await response.json();
      renderResults(payload.results || [], trimmed);
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      runSearch(queryInput.value);
    });

    exampleButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.query || "";
        queryInput.value = value;
        runSearch(value);
      });
    });
  </script>
</body>
</html>
"""


class ICD10RequestHandler(BaseHTTPRequestHandler):
    db_path: Path = DB_PATH

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return

        if parsed.path == "/api/search":
            self.handle_search(parsed.query)
            return

        if parsed.path == "/health":
            self.handle_health()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def handle_search(self, raw_query: str) -> None:
        params = parse_qs(raw_query)
        user_query = params.get("q", [""])[0]
        with get_connection(self.db_path) as connection:
            results = search_icd10(connection, self.db_path, user_query)
        payload = {"query": user_query, "results": [asdict(item) for item in results]}
        self.send_json(payload)

    def handle_health(self) -> None:
        with get_connection(self.db_path) as connection:
            entry_count = connection.execute("SELECT COUNT(*) FROM icd10_entries").fetchone()[0]
            alias_count = connection.execute("SELECT COUNT(*) FROM diagnosis_aliases").fetchone()[0]
        self.send_json(
            {
                "status": "ok",
                "database": str(self.db_path),
                "entry_count": int(entry_count),
                "alias_count": int(alias_count),
            }
        )

    def send_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ICD-10 local web search app.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the server to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind the server to")
    parser.add_argument(
        "--db-path",
        default=str(DB_PATH),
        help="Path to the SQLite database created by build_db.py",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database file not found: {db_path}. Run build_db.py first to create it."
        )

    ICD10RequestHandler.db_path = db_path
    load_alias_rules.cache_clear()
    load_alias_rules(str(db_path.resolve()))

    server = ThreadingHTTPServer((args.host, args.port), ICD10RequestHandler)
    print(f"Serving ICD-10 search at http://{args.host}:{args.port}")
    print(f"Using database: {db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nServer stopped.")


if __name__ == "__main__":
    main()
