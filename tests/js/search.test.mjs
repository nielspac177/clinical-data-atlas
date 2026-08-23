// Pure-logic tests for search.js's ranker. `createSearchBox` is a DOM
// adapter and is exercised by hand/e2e, not here; importing this module
// under Node must not touch `document`/`window` at load time.
import assert from "node:assert/strict";
import test from "node:test";

import { buildSearch } from "../../site/assets/js/search.js";

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
