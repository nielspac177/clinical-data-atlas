// Pure-logic tests for facets-toggle.js: the label the narrow-screen
// "Filters" disclosure wears, which is the only thing on screen that can
// say how many filters are active while the rail is closed.
// `createFacetsToggle` is a DOM adapter (inserts the button, toggles
// `aria-expanded`/`[data-expanded]`) and is exercised by e2e, not here;
// importing this module under Node must not touch `document` at load
// time.
import assert from "node:assert/strict";
import test from "node:test";

import {
  activeFilterCount,
  filtersLabel,
  NARROW_QUERY,
} from "../../site/assets/js/facets-toggle.js";

test("nothing selected reads as a plain 'Filters'", () => {
  assert.equal(activeFilterCount({}), 0);
  assert.equal(filtersLabel({}), "Filters");
  assert.equal(filtersLabel(undefined), "Filters");
  assert.equal(filtersLabel(null), "Filters");
  // The shape app-table.js actually holds between filter changes: every
  // facet present, all of them empty.
  assert.equal(
    filtersLabel({
      domain: [],
      modality: [],
      condition: [],
      access: [],
      source: [],
      country: [],
      species: [],
      years: undefined,
      q: "",
    }),
    "Filters",
  );
});

test("every selected value counts, across facets", () => {
  assert.equal(filtersLabel({ access: ["open"] }), "Filters (1)");
  assert.equal(
    activeFilterCount({ domain: ["neurology", "oncology"], access: ["open"] }),
    3,
  );
  assert.equal(
    filtersLabel({ domain: ["neurology", "oncology"], access: ["open"] }),
    "Filters (3)",
  );
  assert.equal(
    activeFilterCount({
      domain: ["neurology"],
      modality: ["MRI"],
      condition: ["epilepsy"],
      access: ["open"],
      source: ["openneuro"],
      country: ["US"],
      species: ["human"],
    }),
    7,
  );
});

test("a years range counts once, however many bounds it has", () => {
  assert.equal(activeFilterCount({ years: { start: 2015, end: 2018 } }), 1);
  assert.equal(activeFilterCount({ years: { start: 2015 } }), 1);
  assert.equal(activeFilterCount({ years: { end: 2018 } }), 1);
  assert.equal(filtersLabel({ access: ["open"], years: { start: 2015 } }), "Filters (2)");
});

test("an empty or absent years range counts for nothing", () => {
  assert.equal(activeFilterCount({ years: undefined }), 0);
  assert.equal(activeFilterCount({ years: {} }), 0);
  assert.equal(activeFilterCount({ years: { start: undefined, end: undefined } }), 0);
  // `url-state.js` can hand back nulls for an unparsed bound.
  assert.equal(activeFilterCount({ years: { start: null, end: null } }), 0);
});

test("year 0 is a bound like any other", () => {
  // Guards the `!= null` test against a `start || end` regression.
  assert.equal(activeFilterCount({ years: { start: 0 } }), 1);
});

test("the toolbar's text query is not a facet", () => {
  // `q` filters the table from a box the reader can see above it, so
  // attributing it to the collapsed panel would be a lie.
  assert.equal(activeFilterCount({ q: "epilepsy" }), 0);
  assert.equal(filtersLabel({ q: "epilepsy", access: ["open"] }), "Filters (1)");
});

test("unknown keys and non-arrays are ignored, not counted", () => {
  assert.equal(activeFilterCount({ sort: "-name", node: "openneuro:ds1" }), 0);
  assert.equal(activeFilterCount({ domain: "neurology" }), 0);
});

test("the breakpoint matches the one layout.css collapses at", () => {
  assert.equal(NARROW_QUERY, "(max-width: 899px)");
});
