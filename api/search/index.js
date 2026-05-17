const { searchIcd10 } = require("../shared/searchEngine");

module.exports = async function (context, req) {
  try {
    const query = (req.query.q || req.query.query || "").trim();
    const limit = Number.parseInt(req.query.limit || "20", 10);
    const results = await searchIcd10(query, Number.isNaN(limit) ? 20 : limit);

    context.res = {
      status: 200,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store"
      },
      body: {
        query,
        resultCount: results.length,
        results
      }
    };
  } catch (error) {
    context.log.error("Search failed", error);
    context.res = {
      status: 500,
      headers: {
        "Content-Type": "application/json; charset=utf-8"
      },
      body: {
        error: "Search request failed."
      }
    };
  }
};
