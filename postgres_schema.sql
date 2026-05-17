CREATE EXTENSION IF NOT EXISTS pg_trgm;

DROP TABLE IF EXISTS diagnosis_aliases;
DROP TABLE IF EXISTS icd10_entries;

CREATE TABLE icd10_entries (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL,
    short_description TEXT NOT NULL,
    long_description TEXT NOT NULL,
    searchable_text TEXT NOT NULL,
    nf_excl TEXT,
    source_file TEXT NOT NULL,
    search_document tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(code, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(short_description, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(long_description, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(searchable_text, '')), 'C')
    ) STORED
);

CREATE TABLE diagnosis_aliases (
    id BIGSERIAL PRIMARY KEY,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    canonical_text TEXT NOT NULL,
    normalized_canonical_text TEXT NOT NULL,
    preferred_code TEXT,
    notes TEXT,
    source TEXT NOT NULL
);

CREATE INDEX idx_pg_icd10_code ON icd10_entries (code);
CREATE INDEX idx_pg_icd10_search_document ON icd10_entries USING GIN (search_document);
CREATE INDEX idx_pg_icd10_short_trgm ON icd10_entries USING GIN (short_description gin_trgm_ops);
CREATE INDEX idx_pg_icd10_long_trgm ON icd10_entries USING GIN (long_description gin_trgm_ops);
CREATE INDEX idx_pg_icd10_searchable_trgm ON icd10_entries USING GIN (searchable_text gin_trgm_ops);
CREATE INDEX idx_pg_alias_normalized ON diagnosis_aliases (normalized_alias);

-- Example load commands after build_db.py exports the CSV files:
-- \copy icd10_entries(code, short_description, long_description, nf_excl, source_file, searchable_text) FROM 'data/postgres_seed/icd10_entries.csv' CSV HEADER
-- \copy diagnosis_aliases(alias, normalized_alias, canonical_text, normalized_canonical_text, preferred_code, notes, source) FROM 'data/postgres_seed/diagnosis_aliases.csv' CSV HEADER
