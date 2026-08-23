// Pure-logic tests for url-state.js's parse/format core. `readState` /
// `writeState` / `onChange` are thin `window.location`/`history` adapters
// over `parseState`/`formatState` and are exercised in the browser via the
// e2e suite (Task 3.4); this file never touches `window`.
import assert from "node:assert/strict";
import test from "node:test";

import {
  formatState,
  parseState,
} from "../../site/assets/js/url-state.js";

test("round trip: every key survives parse(format(x))", () => {
  const state = {
    q: "mimic",
    domain: ["neurology", "oncology"],
    modality: ["MRI", "EEG"],
    condition: ["epilepsy"],
    access: ["open", "registration"],
    source: ["openneuro"],
    country: ["US", "PE"],
    species: ["human"],
    years: { start: 2010, end: 2020 },
    sort: "-year",
  };

  const qs = formatState(state);
  assert.deepEqual(parseState(qs), state);
  assert.deepEqual(parseState(`?${qs}`), state);
});

test("round trip: a node-only graph selection", () => {
  const state = { node: "openneuro:ds000001" };
  assert.deepEqual(parseState(formatState(state)), state);
});

test("empty state round-trips to empty state", () => {
  assert.deepEqual(parseState(formatState({})), {});
  assert.equal(formatState({}), "");
  assert.deepEqual(parseState(""), {});
  assert.deepEqual(parseState("?"), {});
});

test("defaults are omitted from the formatted query string", () => {
  // sort "name" is the table's default ascending sort -> not serialized.
  assert.equal(formatState({ sort: "name" }), "");
  assert.ok(!formatState({ sort: "name", q: "x" }).includes("sort="));

  // Empty lists / empty query text don't get written either.
  assert.equal(formatState({ domain: [], q: "" }), "");
});

test("malformed values are ignored on parse, not preserved or thrown", () => {
  assert.deepEqual(parseState("sort=bogus"), {});
  assert.deepEqual(parseState("years=not-a-range"), {});
  assert.deepEqual(parseState("years=2020"), {});
  assert.deepEqual(parseState("years=abcd-efgh"), {});
  // Blank/whitespace-only list entries collapse to "no filter".
  assert.deepEqual(parseState("domain=,,%20,"), {});
  // Unknown keys are simply not part of the known shape.
  assert.deepEqual(parseState("unknown=1&q=x"), { q: "x" });
});

test("list values round-trip even with spaces inside a token", () => {
  // Commas are the list separator itself, so a token may not contain one;
  // spaces (e.g. a multi-word condition label) must still survive intact.
  const state = { condition: ["multiple sclerosis", "stroke"] };
  assert.deepEqual(parseState(formatState(state)), state);
});

test("years requires both bounds to be written", () => {
  assert.equal(formatState({ years: { start: 2010 } }), "");
  assert.equal(formatState({ years: {} }), "");
  const qs = formatState({ years: { start: 2010, end: 2020 } });
  assert.match(qs, /years=2010-2020/);
});

test("formatState never emits an unrecognized sort token", () => {
  assert.equal(formatState({ sort: "bogus" }), "");
});
