// Pure-logic tests for filters.js: the OR-within/AND-across facet
// predicate, the years-overlap test, and facetCounts' "other facets
// applied" semantics. `renderFacetOptions` is a DOM adapter, exercised by
// hand/e2e, not here.
import assert from "node:assert/strict";
import test from "node:test";

import {
  applyFilters,
  facetCounts,
  domainLabel,
  sourceLabel,
  capitalize,
} from "../../site/assets/js/filters.js";

const rows = [
  {
    id: "openneuro:ds000001",
    name: "Resting-state MRI in focal epilepsy",
    summary: "MRI from adults with epilepsy.",
    source: "openneuro",
    domains: ["neurology"],
    modalities: ["MRI"],
    conditions: ["epilepsy"],
    countries: ["US"],
    access: "open",
    species: "human",
    years: { start: 2015, end: 2018 },
  },
  {
    id: "openneuro:ds000002",
    name: "Rodent model of ischemic stroke",
    summary: "Rodent MRI stroke model.",
    source: "openneuro",
    domains: ["neuroscience", "neurology"],
    modalities: ["MRI"],
    conditions: ["stroke"],
    countries: ["US"],
    access: "open",
    species: "animal",
    years: { start: 2017, end: 2020 },
  },
  {
    id: "physionet:atlas-eeg-stroke",
    name: "Bedside EEG in acute stroke care",
    summary: "Continuous bedside EEG.",
    source: "physionet",
    domains: ["neurology", "critical_care"],
    modalities: ["EEG"],
    conditions: ["stroke"],
    countries: ["US"],
    access: "credentialed",
    species: "human",
    years: { start: 2018, end: 2022 },
  },
  {
    id: "physionet:atlas-sepsis-registry",
    name: "National sepsis registry extract",
    summary: "Registry of sepsis episodes.",
    source: "physionet",
    domains: ["infectious_disease", "public_health"],
    modalities: ["EHR"],
    conditions: ["sepsis"],
    countries: ["PE"],
    access: "purchase",
    species: "human",
    years: {}, // unknown years
  },
];

test("no filters returns every row unchanged", () => {
  assert.equal(applyFilters(rows, {}).length, rows.length);
  assert.equal(applyFilters(rows, undefined).length, rows.length);
});

test("OR within a facet: multiple domains keep rows in either", () => {
  const result = applyFilters(rows, { domain: ["infectious_disease", "critical_care"] });
  const ids = result.map((r) => r.id).sort();
  assert.deepEqual(ids, ["physionet:atlas-eeg-stroke", "physionet:atlas-sepsis-registry"]);
});

test("AND across facets: domain and access both constrain", () => {
  const result = applyFilters(rows, { domain: ["neurology"], access: ["open"] });
  const ids = result.map((r) => r.id).sort();
  // neurology ∩ open -> ds000001 and ds000002; the credentialed EEG row
  // is neurology but not open, so it's excluded.
  assert.deepEqual(ids, ["openneuro:ds000001", "openneuro:ds000002"]);
});

test("scalar facets (access/source/species) also OR within themselves", () => {
  const result = applyFilters(rows, { access: ["open", "purchase"] });
  const ids = result.map((r) => r.id).sort();
  assert.deepEqual(ids, [
    "openneuro:ds000001",
    "openneuro:ds000002",
    "physionet:atlas-sepsis-registry",
  ]);
});

test("years overlap: a range keeps rows whose coverage intersects it", () => {
  // [2019, 2019] overlaps ds000002 (2017-2020) and the EEG row (2018-2022)
  // but not ds000001 (2015-2018), and excludes the unknown-years row.
  const result = applyFilters(rows, { years: { start: 2019, end: 2019 } });
  const ids = result.map((r) => r.id).sort();
  assert.deepEqual(ids, ["openneuro:ds000002", "physionet:atlas-eeg-stroke"]);
});

test("years overlap: an unbounded end still excludes a row entirely before it", () => {
  const result = applyFilters(rows, { years: { start: 2021 } });
  const ids = result.map((r) => r.id);
  assert.deepEqual(ids, ["physionet:atlas-eeg-stroke"]);
});

test("years overlap: a row with no known years is excluded once a year filter is active", () => {
  const result = applyFilters(rows, { years: { start: 1900, end: 2100 } });
  assert.ok(!result.some((r) => r.id === "physionet:atlas-sepsis-registry"));
});

test("text query matches name, summary, source, conditions and id, case-insensitively", () => {
  assert.equal(applyFilters(rows, { q: "epilepsy" }).length, 1);
  assert.equal(applyFilters(rows, { q: "EPILEPSY" }).length, 1);
  assert.equal(applyFilters(rows, { q: "physionet" }).length, 2);
  assert.equal(applyFilters(rows, { q: "atlas-sepsis-registry" }).length, 1);
  assert.equal(applyFilters(rows, { q: "bedside" }).length, 1); // from summary
});

test("multi-token text query requires every token to match (possibly different fields)", () => {
  const result = applyFilters(rows, { q: "stroke rodent" });
  assert.equal(result.length, 1);
  assert.equal(result[0].id, "openneuro:ds000002");
});

test("combines a text query with facet filters (AND)", () => {
  const result = applyFilters(rows, { q: "stroke", access: ["open"] });
  assert.equal(result.length, 1);
  assert.equal(result[0].id, "openneuro:ds000002");
});

test("facetCounts computes counts with the other facets applied, not the target facet", () => {
  // Fix access=open; counts for the *domain* facet should reflect only
  // the open rows (ds000001, ds000002), regardless of any domain
  // selection already made.
  const counts = facetCounts(rows, { access: ["open"], domain: ["neurology"] }, "domain");
  assert.equal(counts.get("neurology"), 2);
  assert.equal(counts.get("neuroscience"), 1);
  assert.equal(counts.get("infectious_disease"), undefined);
});

test("facetCounts for a facet ignores that facet's own current selection", () => {
  // Selecting access=open should not zero out the *access* facet's own
  // count map down to just "open" - it should show what each access
  // value would yield.
  const counts = facetCounts(rows, { access: ["open"] }, "access");
  assert.equal(counts.get("open"), 2);
  assert.equal(counts.get("credentialed"), 1);
  assert.equal(counts.get("purchase"), 1);
});

test("facetCounts returns an empty map for an unknown facet name", () => {
  const counts = facetCounts(rows, {}, "not-a-real-facet");
  assert.equal(counts.size, 0);
});

test("label helpers", () => {
  assert.equal(domainLabel("critical_care"), "Critical care");
  assert.equal(domainLabel("obstetrics_gynecology"), "Obstetrics & gynecology");
  assert.equal(domainLabel("totally_unknown_domain"), "Totally unknown domain");
  assert.equal(sourceLabel("physionet"), "PhysioNet");
  assert.equal(sourceLabel("openneuro"), "OpenNeuro");
  assert.equal(sourceLabel("mystery_source"), "Mystery source");
  assert.equal(capitalize("epilepsy"), "Epilepsy");
  assert.equal(capitalize(""), "");
});
