// Pure-logic tests for table.js's CSV builder. `createTable` is a DOM
// adapter (renders rows, wires sortable headers) and is exercised by
// hand/e2e, not here; importing this module under Node must not touch
// `document` at load time.
import assert from "node:assert/strict";
import test from "node:test";

import { toCSV } from "../../site/assets/js/table.js";

const rows = [
  {
    id: "openneuro:ds000001",
    name: 'Resting-state MRI in "focal" epilepsy',
    source: "openneuro",
    domains: ["neurology", "neuroscience"],
    modalities: ["MRI"],
    conditions: ["epilepsy"],
    access: "open",
    years: { start: 2015, end: 2018 },
    sample_size: 42,
    sample_unit: "participants",
    countries: ["US"],
    species: "human",
    url: "https://openneuro.org/datasets/ds000001",
  },
  {
    id: "physionet:atlas-sepsis-registry",
    name: "National sepsis registry extract",
    source: "physionet",
    domains: ["infectious_disease", "public_health"],
    modalities: ["EHR"],
    conditions: ["sepsis"],
    access: "purchase",
    years: {},
    sample_size: null,
    sample_unit: null,
    countries: ["PE", "US"],
    species: "human",
    url: "https://physionet.org/content/atlas-sepsis-registry/1.0.0/",
  },
];

test("starts with a UTF-8 BOM", () => {
  const csv = toCSV(rows);
  assert.equal(csv.codePointAt(0), 0xfeff);
});

test("every field is quoted", () => {
  const csv = toCSV([rows[0]]);
  const lines = csv.replace(/^﻿/, "").split("\r\n").filter(Boolean);
  assert.equal(lines.length, 2); // header + one row
  for (const line of lines) {
    // Every comma-separated field in the line starts and ends with a
    // quote (a crude but effective structural check).
    const fields = line.match(/"(?:[^"]|"")*"/g);
    assert.ok(fields && fields.length > 0);
    assert.equal(fields.join(","), line);
  }
});

test("embedded quotes are doubled per RFC 4180", () => {
  const csv = toCSV([rows[0]]);
  assert.ok(csv.includes('"Resting-state MRI in ""focal"" epilepsy"'));
});

test("list-valued fields are joined with '; '", () => {
  const csv = toCSV([rows[0]]);
  assert.ok(csv.includes('"neurology; neuroscience"'));
  const csv2 = toCSV([rows[1]]);
  assert.ok(csv2.includes('"PE; US"'));
});

test("null/undefined fields render as empty quoted strings, not 'null'", () => {
  const csv = toCSV([rows[1]]);
  assert.ok(!csv.includes("null"));
  assert.ok(!csv.includes("undefined"));
});

test("uses CRLF line endings", () => {
  const csv = toCSV(rows);
  const withoutBom = csv.replace(/^﻿/, "");
  assert.ok(withoutBom.includes("\r\n"));
  // No bare LF that isn't part of a CRLF pair.
  assert.equal(withoutBom.replace(/\r\n/g, "").includes("\n"), false);
});

test("header row lists every column once", () => {
  const csv = toCSV([]);
  const header = csv.replace(/^﻿/, "").split("\r\n")[0];
  assert.ok(header.includes('"ID"'));
  assert.ok(header.includes('"Name"'));
  assert.ok(header.includes('"Sample size"'));
  assert.ok(header.includes('"URL"'));
});

test("an empty row set still produces a header-only CSV", () => {
  const csv = toCSV([]);
  const lines = csv.replace(/^﻿/, "").split("\r\n").filter(Boolean);
  assert.equal(lines.length, 1);
});

test("tolerates a missing rows argument", () => {
  assert.doesNotThrow(() => toCSV());
});

test("CSV/formula injection: a cell starting with =, +, -, @ is prefixed with '", () => {
  const evil = [
    { id: "a:1", name: "=SUM(A1:A9)", source: "curated", url: "" },
    { id: "a:2", name: "+1 234", source: "curated", url: "" },
    { id: "a:3", name: "-2 (decrease)", source: "curated", url: "" },
    { id: "a:4", name: "@mention", source: "curated", url: "" },
    // Leading whitespace must not smuggle a formula past the check.
    { id: "a:5", name: "  =HYPERLINK(\"http://evil\")", source: "curated", url: "" },
  ];
  const csv = toCSV(evil);
  assert.ok(csv.includes('"\'=SUM(A1:A9)"'));
  assert.ok(csv.includes('"\'+1 234"'));
  assert.ok(csv.includes('"\'-2 (decrease)"'));
  assert.ok(csv.includes('"\'@mention"'));
  assert.ok(csv.includes('"\'  =HYPERLINK(""http://evil"")"'));
});

test("CSV/formula injection: ordinary values are left untouched", () => {
  const csv = toCSV([{ id: "a:1", name: "Sepsis Registry", source: "curated", url: "" }]);
  assert.ok(csv.includes('"Sepsis Registry"'));
  assert.ok(!csv.includes("'Sepsis"));
});
