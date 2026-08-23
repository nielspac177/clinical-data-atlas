/**
 * URL query-string state for the graph and table pages.
 *
 * The DOM-free core is `parseState`/`formatState` — a pure pair that turns
 * a query string into a plain state object and back. `readState`/
 * `writeState`/`onChange` are thin adapters over `window.location`/
 * `window.history`/`popstate`, built on top of that core, so this module
 * stays importable (and testable) without a DOM: nothing at module scope
 * touches `window`.
 *
 * Keys: `node` (single id, graph selection), `q` (free-text query),
 * seven comma-joined facet lists (`domain`, `modality`, `condition`,
 * `access`, `source`, `country`, `species`), `years` (`"2010-2020"`, or
 * open-ended: `"2018-"` / `"-2020"`), and
 * `sort` (one of `SORT_VALUES`). A key absent from the state is a key
 * absent from the URL — there is no serialized "unset" marker.
 *
 * Convention (per the caller, not enforced here): the graph page pushes a
 * history entry when `node` changes (so Back steps through selections);
 * table/legend filters replace the current entry (so filtering doesn't
 * spam history). That's the `{ push }` option on `writeState`.
 */

/** The seven facets, comma-joined in the query string. */
const LIST_KEYS = Object.freeze([
  "domain",
  "modality",
  "condition",
  "access",
  "source",
  "country",
  "species",
]);

/**
 * Accepted `sort` tokens: `name|-name|year|-year|n|-n|source|-source` per
 * the design, plus `access|-access` — the table's Access column header is
 * also sortable (`th[data-column="access"][aria-sort]` in the real
 * `table.html`), so its sort state round-trips through the URL too.
 */
const SORT_VALUES = new Set([
  "name",
  "-name",
  "year",
  "-year",
  "n",
  "-n",
  "source",
  "-source",
  "access",
  "-access",
]);

/** The default sort — omitted from the URL rather than written out. */
const DEFAULT_SORT = "name";

// Years are never negative in this domain, so `-` is purely the range
// separator; either side may be omitted for an open-ended range
// ("2018-" = from 2018 on, "-2020" = up to 2020).
const YEARS_RE = /^(\d+)?-(\d+)?$/;

function splitList(raw) {
  return raw
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

/**
 * Parse a `location.search`-shaped string (with or without a leading
 * `?`) into a state object. Absent or malformed values are simply
 * omitted — this never throws and never fabricates a key.
 */
export function parseState(search) {
  const params = new URLSearchParams(String(search ?? ""));
  const state = {};

  const node = params.get("node");
  if (node) state.node = node;

  const q = params.get("q");
  if (q) state.q = q;

  for (const key of LIST_KEYS) {
    const raw = params.get(key);
    if (!raw) continue;
    const values = splitList(raw);
    if (values.length > 0) state[key] = values;
  }

  const years = params.get("years");
  if (years) {
    const match = YEARS_RE.exec(years.trim());
    if (match) {
      const start = match[1] !== undefined ? Number(match[1]) : undefined;
      const end = match[2] !== undefined ? Number(match[2]) : undefined;
      if (start !== undefined || end !== undefined) {
        state.years = {};
        if (start !== undefined) state.years.start = start;
        if (end !== undefined) state.years.end = end;
      }
      // Both sides absent ("years=-") -> an empty range, i.e. no filter.
    }
    // No match at all (garbage, no "-") -> malformed, silently ignored.
  }

  const sort = params.get("sort");
  if (sort && SORT_VALUES.has(sort)) {
    state.sort = sort;
  }

  return state;
}

/**
 * Serialize a state object back into a query string (no leading `?`).
 * Default/empty values are omitted so the URL never grows a `key=`
 * pair that doesn't mean anything (`sort=name`, `domain=`, ...).
 */
export function formatState(state) {
  const params = new URLSearchParams();
  const s = state ?? {};

  if (s.node) params.set("node", s.node);
  if (s.q) params.set("q", s.q);

  for (const key of LIST_KEYS) {
    const values = s[key];
    if (Array.isArray(values) && values.length > 0) {
      params.set(key, values.join(","));
    }
  }

  const years = s.years;
  if (years && (Number.isFinite(years.start) || Number.isFinite(years.end))) {
    const startPart = Number.isFinite(years.start) ? years.start : "";
    const endPart = Number.isFinite(years.end) ? years.end : "";
    params.set("years", `${startPart}-${endPart}`);
  }

  if (s.sort && s.sort !== DEFAULT_SORT && SORT_VALUES.has(s.sort)) {
    params.set("sort", s.sort);
  }

  return params.toString();
}

function isEmptyValue(value) {
  return (
    value === null ||
    value === undefined ||
    value === "" ||
    (Array.isArray(value) && value.length === 0)
  );
}

/** Merge `partial` into `base`, dropping keys whose new value is "empty". */
function mergeState(base, partial) {
  const next = { ...base };
  for (const [key, value] of Object.entries(partial ?? {})) {
    if (isEmptyValue(value)) delete next[key];
    else next[key] = value;
  }
  return next;
}

/** Read the current state from `window.location.search`. */
export function readState() {
  return parseState(window.location.search);
}

function buildUrl(state) {
  const qs = formatState(state);
  const { pathname, hash } = window.location;
  return qs ? `${pathname}?${qs}${hash}` : `${pathname}${hash}`;
}

const listeners = new Set();
let popstateWired = false;

function notify(state) {
  for (const cb of listeners) cb(state);
}

/**
 * Merge `partial` over the current state and reflect it in the URL.
 * `{ push: true }` uses `pushState` (a new history entry, e.g. the graph
 * page's `?node=`); the default `replaceState` is right for anything a
 * reader would not expect Back to step through one filter at a time.
 * Does not itself notify `onChange` listeners with `partial` — history
 * mutation doesn't fire `popstate`, so this returns the merged state for
 * the caller and leaves `onChange` for actual navigation (Back/Forward).
 */
export function writeState(partial, { push = false } = {}) {
  const next = mergeState(readState(), partial);
  const url = buildUrl(next);
  const method = push ? "pushState" : "replaceState";
  window.history[method](next, "", url);
  return next;
}

/**
 * Subscribe to state changes driven by browser navigation (Back/Forward).
 * Returns an unsubscribe function. Lazily wires the single `popstate`
 * listener on first use, so importing this module has no side effects.
 */
export function onChange(cb) {
  listeners.add(cb);
  if (!popstateWired && typeof window !== "undefined") {
    popstateWired = true;
    window.addEventListener("popstate", () => notify(readState()));
  }
  return () => listeners.delete(cb);
}
