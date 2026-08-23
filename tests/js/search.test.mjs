// Pure-logic tests for search.js's ranker and the combobox's query-state
// helpers. `createSearchBox` itself is a DOM adapter and is exercised by
// hand/e2e, not here; importing this module under Node must not touch
// `document`/`window` at load time.
import assert from "node:assert/strict";
import test from "node:test";

import {
  LOADING_MESSAGE,
  buildSearch,
  needsRerun,
  searchState,
} from "../../site/assets/js/search.js";

// A small fixture-like row set, shaped like search-index.json rows.
const rows = [
  {
    id: "physionet:mimic-iv",
    name: "MIMIC-IV Clinical Database",
    summary: "De-identified ICU electronic health records from a US hospital.",
    source: "physionet",
    conditions: ["sepsis"],
  },
  {
    id: "physionet:mimic-cxr",
    name: "MIMIC-CXR: Chest X-rays",
    summary: "Chest radiographs paired with free-text reports.",
    source: "physionet",
    conditions: [],
  },
  {
    id: "openneuro:ds000001",
    name: "Resting-state MRI in focal epilepsy",
    summary: "Structural and functional MRI from adults with epilepsy.",
    source: "openneuro",
    conditions: ["epilepsy"],
  },
  {
    id: "physionet:icu-registry",
    name: "ICU Outcomes Registry",
    // "mimic" only appears buried in the summary, not the name.
    summary: "A registry built to mimic the cohort structure of MIMIC-III.",
    source: "physionet",
    conditions: [],
  },
  {
    id: "openneuro:ds000099",
    name: "Antiepileptic Drug Response Trial",
    summary: "Trial of antiepileptic medication response.",
    source: "openneuro",
    conditions: ["epilepsy"],
  },
  {
    id: "openneuro:ds000100",
    name: "Epilepsy Monitoring Unit EEG",
    summary: "Continuous EEG during epilepsy monitoring unit admission.",
    source: "openneuro",
    conditions: ["epilepsy"],
  },
];

test("buildSearch returns a query function", () => {
  const query = buildSearch(rows);
  assert.equal(typeof query, "function");
});

test('"MIMIC" ranks the dataset named MIMIC top, ahead of a mere summary mention', () => {
  const query = buildSearch(rows);
  const results = query("MIMIC");
  assert.ok(results.length >= 2);
  // Both MIMIC-named datasets should rank above the row that only
  // mentions "MIMIC" in its summary.
  const topIds = results.slice(0, 2).map((r) => r.id).sort();
  assert.deepEqual(topIds, ["physionet:mimic-cxr", "physionet:mimic-iv"]);
  assert.equal(results[0].score > results.find((r) => r.id === "physionet:icu-registry").score, true);
});

test("search is case-insensitive", () => {
  const query = buildSearch(rows);
  const lower = query("mimic").map((r) => r.id);
  const upper = query("MIMIC").map((r) => r.id);
  const mixed = query("MiMiC").map((r) => r.id);
  assert.deepEqual(lower, upper);
  assert.deepEqual(lower, mixed);
});

test("a name-token prefix match outranks a mid-word substring match", () => {
  const query = buildSearch(rows);
  const results = query("epilep");
  const byId = new Map(results.map((r) => [r.id, r.score]));
  // "Epilepsy Monitoring Unit EEG" and "Resting-state MRI in focal
  // epilepsy" both have a token that starts with "epilep" (prefix tier).
  // "Antiepileptic Drug Response Trial" only contains "epilep" mid-word
  // inside "antiepileptic" (substring tier) - it must rank strictly lower.
  assert.ok(byId.has("openneuro:ds000100"));
  assert.ok(byId.has("openneuro:ds000099"));
  assert.ok(
    byId.get("openneuro:ds000100") > byId.get("openneuro:ds000099"),
    "token-prefix hit should outscore a mid-word substring hit",
  );
});

test("a field hit outside the name (conditions/source/summary) still matches, lower tier", () => {
  const query = buildSearch(rows);
  const results = query("sepsis");
  assert.equal(results.length, 1);
  assert.equal(results[0].id, "physionet:mimic-iv");
});

test("no query, or a query matching nothing, returns an empty array", () => {
  const query = buildSearch(rows);
  assert.deepEqual(query(""), []);
  assert.deepEqual(query("   "), []);
  assert.deepEqual(query("zzzznonexistentzzzz"), []);
});

test("ordering is stable: identical re-runs produce identical order", () => {
  const query = buildSearch(rows);
  const first = query("e").map((r) => r.id);
  const second = query("e").map((r) => r.id);
  assert.deepEqual(first, second);
});

test("ties break by name ascending", () => {
  // Same-length names hitting the same tier via the same field produce an
  // identical score (the length nudge only breaks ties *within* a tier
  // for names of differing length) - the remaining tie goes to `name`.
  const tiedRows = [
    { id: "a:2", name: "Zeta Cohort", summary: "", source: "curated", conditions: [] },
    { id: "a:1", name: "Beta Cohort", summary: "", source: "curated", conditions: [] },
  ];
  const query = buildSearch(tiedRows);
  const results = query("cohort");
  assert.equal(results[0].score, results[1].score);
  assert.deepEqual(results.map((r) => r.name), ["Beta Cohort", "Zeta Cohort"]);
});

test("within the same tier, a tighter (shorter) name match ranks first", () => {
  const rows2 = [
    { id: "a:long", name: "Sepsis Outcomes In The Adult ICU", summary: "", source: "curated", conditions: [] },
    { id: "a:short", name: "Sepsis Registry", summary: "", source: "curated", conditions: [] },
  ];
  const query = buildSearch(rows2);
  const results = query("sepsis");
  assert.equal(results[0].id, "a:short");
});

test("respects the limit parameter", () => {
  const many = Array.from({ length: 30 }, (_, i) => ({
    id: `x:${i}`,
    name: `Sepsis Cohort ${i}`,
    summary: "",
    source: "curated",
    conditions: [],
  }));
  const query = buildSearch(many);
  assert.equal(query("sepsis").length, 20); // default limit
  assert.equal(query("sepsis", 5).length, 5);
  assert.equal(query("sepsis", 100).length, 30);
});

test("rows missing an id are skipped rather than crashing", () => {
  const query = buildSearch([{ name: "No id here" }, ...rows]);
  assert.doesNotThrow(() => query("mimic"));
});

test("buildSearch tolerates a missing/empty rows argument", () => {
  assert.deepEqual(buildSearch()("anything"), []);
  assert.deepEqual(buildSearch([])("anything"), []);
});

// --------------------------------------------------------------- searchState
//
// The search index is a second download, fetched lazily (on focus, in
// app-graph.js). `searchState` is the pure part of what the combobox does
// with a query: it has to tell "the index isn't here yet" apart from "the
// index says nothing matches", so a query typed during the fetch can be
// re-run once the index lands instead of being stuck on "No results".

test("searchState: an empty query is idle, with nothing to announce", () => {
  for (const q of ["", "   ", null, undefined]) {
    const state = searchState(q, buildSearch(rows));
    assert.equal(state.status, "idle");
    assert.deepEqual(state.items, []);
    assert.equal(state.message, "");
  }
});

test("searchState: no search function yet is 'loading', never 'No results'", () => {
  for (const notReady of [null, undefined]) {
    const state = searchState("mimic", notReady);
    assert.equal(state.status, "loading");
    assert.deepEqual(state.items, []);
    assert.equal(state.message, LOADING_MESSAGE);
    assert.ok(!/No results/.test(state.message));
  }
});

test("searchState: a query that matches reports its results", () => {
  const state = searchState("mimic", buildSearch(rows));
  assert.equal(state.status, "results");
  assert.equal(state.items[0].id, "physionet:mimic-cxr");
  assert.equal(state.message, `${state.items.length} results for "mimic"`);
});

test("searchState: one result is announced in the singular", () => {
  const state = searchState("antiepileptic", buildSearch(rows));
  assert.equal(state.items.length, 1);
  assert.equal(state.message, '1 result for "antiepileptic"');
});

test("searchState: a loaded index with no match is 'empty'", () => {
  const state = searchState("nothingmatchesthis", buildSearch(rows));
  assert.equal(state.status, "empty");
  assert.deepEqual(state.items, []);
  assert.equal(state.message, 'No results for "nothingmatchesthis"');
});

test("searchState: honours the limit and trims the query", () => {
  assert.equal(searchState("  mimic  ", buildSearch(rows), 1).items.length, 1);
  assert.equal(searchState("  mimic  ", buildSearch(rows)).query, "mimic");
});

test("needsRerun: only a pending query still showing loading/no-results", () => {
  const loading = searchState("mimic", null);
  const empty = searchState("nothingmatchesthis", buildSearch(rows));
  const results = searchState("mimic", buildSearch(rows));

  assert.equal(needsRerun(loading, "mimic"), true);
  assert.equal(needsRerun(empty, "nothingmatchesthis"), true);
  // Already answered by a live index, or the reader cleared the box while
  // the fetch was in flight: leave what's on screen alone.
  assert.equal(needsRerun(results, "mimic"), false);
  assert.equal(needsRerun(loading, ""), false);
  assert.equal(needsRerun(loading, "   "), false);
  assert.equal(needsRerun(undefined, "mimic"), false);
});

test("a query typed before the index lands is answered once it arrives", async () => {
  // Exactly the race the combobox hits: the reader types while the index
  // is still downloading, so `getSearch()` returns null and the debounced
  // query has nothing to run against.
  let search = null;
  const ready = Promise.resolve().then(() => {
    search = buildSearch(rows);
  });

  let state = searchState("mimic", search);
  assert.equal(state.status, "loading");
  assert.equal(state.message, LOADING_MESSAGE);

  await ready;

  // The box re-runs the query it still holds rather than leaving the
  // reader on a stale, wrong "No results".
  assert.equal(needsRerun(state, "mimic"), true);
  state = searchState("mimic", search);
  assert.equal(state.status, "results");
  assert.deepEqual(
    state.items.map((r) => r.id),
    ["physionet:mimic-cxr", "physionet:mimic-iv", "physionet:icu-registry"],
  );
  assert.ok(!/No results/.test(state.message));
});
