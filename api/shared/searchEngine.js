const fs = require("node:fs/promises");
const path = require("node:path");

const TOKEN_PATTERN = /[A-Za-z0-9]+/g;
const NON_ALNUM_PATTERN = /[^a-z0-9]+/g;
const ENTRIES_PATH = path.resolve(__dirname, "..", "data", "icd10_entries.ndjson");
const ALIASES_PATH = path.resolve(__dirname, "..", "data", "diagnosis_aliases.json");

let cachePromise;

function normalizeText(value) {
  return String(value || "").toLowerCase().replace(NON_ALNUM_PATTERN, " ").trim();
}

function normalizeCode(value) {
  return (String(value || "").toUpperCase().match(TOKEN_PATTERN) || []).join("");
}

function tokenize(value) {
  return normalizeText(value).match(TOKEN_PATTERN) || [];
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function aliasRegex(normalizedAlias) {
  return new RegExp(`(?<![a-z0-9])${escapeRegExp(normalizedAlias)}(?![a-z0-9])`, "g");
}

function tokenMatches(queryToken, candidateToken) {
  if (queryToken === candidateToken) {
    return true;
  }
  if (queryToken.length >= 4 && candidateToken.length >= 4) {
    if (queryToken.startsWith(candidateToken) || candidateToken.startsWith(queryToken)) {
      return true;
    }
  }
  if (queryToken.length >= 5 && candidateToken.length >= 5) {
    if (queryToken.slice(0, 3) !== candidateToken.slice(0, 3)) {
      return false;
    }
    return similarity(queryToken, candidateToken) >= 0.82;
  }
  return false;
}

function tokenOverlapRatio(queryTokens, candidateTokens) {
  if (!queryTokens.length) {
    return 0;
  }

  let matches = 0;
  for (const queryToken of queryTokens) {
    if (candidateTokens.some((candidateToken) => tokenMatches(queryToken, candidateToken))) {
      matches += 1;
    }
  }
  return matches / queryTokens.length;
}

function similarity(a, b) {
  if (a === b) {
    return 1;
  }
  if (!a.length || !b.length) {
    return 0;
  }

  const matrix = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = 1; i <= a.length; i += 1) {
    for (let j = 1; j <= b.length; j += 1) {
      if (a[i - 1] === b[j - 1]) {
        matrix[i][j] = matrix[i - 1][j - 1] + 1;
      } else {
        matrix[i][j] = Math.max(matrix[i - 1][j], matrix[i][j - 1]);
      }
    }
  }

  const lcs = matrix[a.length][b.length];
  return (2 * lcs) / (a.length + b.length);
}

async function loadData() {
  const [entryBlob, aliasBlob] = await Promise.all([
    fs.readFile(ENTRIES_PATH, "utf8"),
    fs.readFile(ALIASES_PATH, "utf8")
  ]);

  const entries = entryBlob
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => {
      const row = JSON.parse(line);
      return {
        ...row,
        normalizedCode: normalizeCode(row.code),
        normalizedShort: normalizeText(row.short_description),
        normalizedLong: normalizeText(row.long_description),
        normalizedSearchable: normalizeText(row.searchable_text),
        searchableTokens: tokenize(row.searchable_text)
      };
    });

  const aliases = JSON.parse(aliasBlob).map((row) => ({
    ...row,
    regex: aliasRegex(row.normalized_alias)
  }));

  return { entries, aliases };
}

async function getCache() {
  if (!cachePromise) {
    cachePromise = loadData();
  }
  return cachePromise;
}

function resolveAliases(query, aliases) {
  const normalizedQuery = normalizeText(query);
  const variants = new Set([String(query || "").trim()].filter(Boolean));
  const preferredCodes = new Set();
  const matchedRules = [];

  for (const alias of aliases) {
    if (!alias.regex.test(normalizedQuery)) {
      alias.regex.lastIndex = 0;
      continue;
    }
    alias.regex.lastIndex = 0;

    matchedRules.push(alias);
    if (alias.preferred_code) {
      preferredCodes.add(alias.preferred_code);
    }
    variants.add(alias.canonical_text);
    variants.add(normalizedQuery.replace(alias.regex, alias.normalized_canonical_text));
  }

  return {
    variants: [...variants].filter(Boolean),
    preferredCodes,
    matchedRules,
    normalizedQuery
  };
}

function scoreEntry(entry, variant, originalNormalizedQuery, preferredCodes, matchedRules) {
  const variantNormalized = normalizeText(variant);
  const queryCode = normalizeCode(variant);
  const aliasOnlyQuery = matchedRules.some((rule) => rule.normalized_alias === originalNormalizedQuery);
  let score = 0;

  if (queryCode && queryCode === entry.normalizedCode) {
    score += 260;
  }
  if (variantNormalized === entry.normalizedLong) {
    score += 225;
  }
  if (variantNormalized === entry.normalizedShort) {
    score += 205;
  }
  if (entry.normalizedLong.startsWith(variantNormalized)) {
    score += 125;
  }
  if (entry.normalizedShort.startsWith(variantNormalized)) {
    score += 105;
  }
  if (variantNormalized && entry.normalizedLong.includes(variantNormalized)) {
    score += 65;
  }
  if (variantNormalized && entry.normalizedShort.includes(variantNormalized)) {
    score += 55;
  }
  if (variantNormalized && entry.normalizedSearchable.includes(variantNormalized)) {
    score += 35;
  }

  score += tokenOverlapRatio(tokenize(variantNormalized), entry.searchableTokens) * 80;
  const originalTokens = tokenize(originalNormalizedQuery);
  const originalOverlap = tokenOverlapRatio(originalTokens, entry.searchableTokens);
  score += originalOverlap * 35;
  if (originalOverlap >= 0.999) {
    score += 70;
  }
  if (originalTokens.length > 1 && originalOverlap < 0.999) {
    score -= (1 - originalOverlap) * 60;
  }
  score += Math.max(
    similarity(variantNormalized, entry.normalizedLong),
    similarity(variantNormalized, entry.normalizedShort)
  ) * 25;

  if (preferredCodes.has(entry.code)) {
    score += aliasOnlyQuery ? 260 : 0;
  }

  for (const rule of matchedRules) {
    if (entry.code === rule.preferred_code && rule.normalized_alias === originalNormalizedQuery) {
      score += 35;
    }
    if (rule.normalized_canonical_text && entry.normalizedLong.includes(rule.normalized_canonical_text)) {
      score += 8;
    }
  }

  return score;
}

async function searchIcd10(query, limit = 20) {
  const cleaned = String(query || "").trim();
  if (!cleaned) {
    return [];
  }

  const { entries, aliases } = await getCache();
  const { variants, preferredCodes, matchedRules, normalizedQuery } = resolveAliases(cleaned, aliases);
  const scored = [];

  for (const entry of entries) {
    let bestScore = 0;
    for (const variant of variants) {
      const score = scoreEntry(entry, variant, normalizedQuery, preferredCodes, matchedRules);
      if (score > bestScore) {
        bestScore = score;
      }
    }

    if (bestScore > 0) {
      scored.push({
        code: entry.code,
        short_description: entry.short_description,
        long_description: entry.long_description,
        score: Number(bestScore.toFixed(3))
      });
    }
  }

  scored.sort((left, right) => {
    if (right.score !== left.score) {
      return right.score - left.score;
    }
    if (left.long_description.length !== right.long_description.length) {
      return left.long_description.length - right.long_description.length;
    }
    return left.code.localeCompare(right.code);
  });

  return scored.slice(0, Math.max(1, Math.min(limit, 50)));
}

async function getDatasetInfo() {
  const { entries, aliases } = await getCache();
  return {
    entryCount: entries.length,
    aliasCount: aliases.length
  };
}

module.exports = {
  getDatasetInfo,
  searchIcd10
};
