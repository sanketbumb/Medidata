const { getDatasetInfo } = require("../shared/searchEngine");

module.exports = async function (context) {
  const info = await getDatasetInfo();
  context.res = {
    status: 200,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store"
    },
    body: {
      status: "ok",
      runtime: "azure-functions",
      ...info
    }
  };
};
