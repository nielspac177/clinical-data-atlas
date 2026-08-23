/**
 * Every network read the site does.
 *
 * Four files, all static JSON next to the pages: `data/graph.json`,
 * `data/search-index.json`, `data/stats.json`, and one lazily-fetched
 * `data/records/<source>/<native>.json` per dataset the reader opens.
 *
 * Paths are relative to the document, which is what makes the site work
 * unchanged under a project base path (`/clinical-data-atlas/`) and from a
 * `file://`-adjacent local server. Every request carries `?v=<BUILD>` so a
 * new deploy is never served stale JSON from an old page's cache.
 */

import { BUILD } from "./config.js";

const GRAPH_PATH = "data/graph.json";
const INDEX_PATH = "data/search-index.json";
const STATS_PATH = "data/stats.json";

/**
 * Fetch a JSON file, cache-busted, throwing on anything but a 2xx.
 *
 * `fetch` only rejects on a transport failure — a 404 resolves happily
 * with `ok: false` — so an explicit status check is the difference
 * between "no data" and "the string `<!doctype html>` parsed as JSON".
 */
export async function fetchJSON(path) {
  const url = `${path}${path.includes("?") ? "&" : "?"}v=${encodeURIComponent(BUILD)}`;

  let response;
  try {
    response = await fetch(url, { credentials: "same-origin" });
  } catch (cause) {
    throw new Error(`Could not reach ${path}`, { cause });
  }
  if (!response.ok) {
    throw new Error(`${path} returned HTTP ${response.status}`);
  }
  return response.json();
}

/**
 * Memoise a loader on its promise, so N callers share one request.
 *
 * A rejection clears the memo: a reader who lost their connection for a
 * moment shouldn't have to reload the page to try again.
 */
function once(loader) {
  let pending = null;
  return () => {
    if (pending === null) {
      pending = loader().catch((error) => {
        pending = null;
        throw error;
      });
    }
    return pending;
  };
}

/** `{nodes, links}` — the whole graph (~1 MB). */
export const loadGraph = once(() => fetchJSON(GRAPH_PATH));

/** `{record_count, per_source, per_domain, per_modality, per_access, ...}`. */
export const loadStats = once(() => fetchJSON(STATS_PATH));

/** One search/table row per dataset. Loaded lazily on the graph page. */
export const loadIndex = once(() => fetchJSON(INDEX_PATH));

const records = new Map();

/**
 * The full catalog record behind a dataset id.
 *
 * `openneuro:ds000001` -> `data/records/openneuro/ds000001.json`: the
 * source is everything before the first colon, the native id everything
 * after it (native ids may contain colons of their own).
 */
export function loadRecord(id) {
  const key = String(id ?? "");
  const colon = key.indexOf(":");
  if (colon < 1 || colon === key.length - 1) {
    return Promise.reject(new Error(`Not a record id: ${key}`));
  }

  if (!records.has(key)) {
    const source = encodeURIComponent(key.slice(0, colon));
    const native = encodeURIComponent(key.slice(colon + 1));
    records.set(
      key,
      fetchJSON(`data/records/${source}/${native}.json`).catch((error) => {
        records.delete(key);
        throw error;
      }),
    );
  }
  return records.get(key);
}
