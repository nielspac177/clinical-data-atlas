/**
 * Unit tests for `site/assets/js/graph-index.js`.
 *
 * The module is deliberately DOM-free so it can run under plain
 * `node --test tests/js/` with no browser and no bundler.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  buildIndex,
  deriveBackboneLinks,
  endpointId,
} from "../../site/assets/js/graph-index.js";

const FIXTURE = JSON.parse(
  readFileSync(new URL("../fixtures/site/graph.json", import.meta.url), "utf8"),
);

/** A fresh deep copy, so a test that mutates can't leak into the next one. */
function fixture() {
  return structuredClone(FIXTURE);
}

/** `[{source, target, weight}]` -> `["a|b", ...]`, for readable assertions. */
function pairs(links) {
  return links.map((l) => `${endpointId(l.source)}|${endpointId(l.target)}`);
}

/** Weight lookup keyed the same way. */
function weights(links) {
  return Object.fromEntries(
    links.map((l) => [`${endpointId(l.source)}|${endpointId(l.target)}`, l.weight]),
  );
}

// ---------------------------------------------------------------- buildIndex

test("buildIndex maps every node by id and keeps the node objects", () => {
  const raw = fixture();
  const index = buildIndex(raw);

  assert.equal(index.nodes.size, raw.nodes.length);
  assert.equal(index.nodes.get("source:openneuro").label, "OpenNeuro");
  // Identity matters: graph.js re-renders with the *same* objects so the
  // force layout keeps each node's position across expansions.
  assert.equal(index.nodes.get("source:openneuro"), raw.nodes.find((n) => n.id === "source:openneuro"));
});

test("buildIndex builds undirected adjacency", () => {
  const index = buildIndex(fixture());

  assert.deepEqual(
    [...index.adj.get("openneuro:ds000001")].sort(),
    [
      "condition:epilepsy",
      "institution:002pd6e78",
      "modality:MRI",
      "source:openneuro",
    ],
  );
  // The reverse direction exists too, even though links are one-way in JSON.
  assert.ok(index.adj.get("source:openneuro").has("openneuro:ds000001"));
  assert.equal(index.adj.get("source:openneuro").size, 3);
});

test("buildIndex collects the backbone ids", () => {
  const index = buildIndex(fixture());

  assert.equal(index.backboneIds.size, 10); // stats.json: backbone_count = 10
  assert.ok(index.backboneIds.has("modality:EEG"));
  assert.ok(!index.backboneIds.has("openneuro:ds000001"));
});

test("buildIndex drops dangling links and self-loops", () => {
  const raw = fixture();
  raw.links.push({ source: "openneuro:ds000001", target: "modality:GHOST", type: "modality" });
  raw.links.push({ source: "modality:EEG", target: "modality:EEG", type: "modality" });

  const index = buildIndex(raw);

  assert.equal(index.links.length, FIXTURE.links.length);
  assert.ok(!index.adj.get("openneuro:ds000001").has("modality:GHOST"));
  assert.ok(!index.adj.get("modality:EEG").has("modality:EEG"));
});

test("buildIndex tolerates an empty or absent graph", () => {
  const empty = buildIndex({});
  assert.equal(empty.nodes.size, 0);
  assert.deepEqual(empty.neighbours("nope"), []);
  assert.deepEqual(empty.subgraph(new Set()), { nodes: [], links: [] });
  assert.deepEqual(deriveBackboneLinks(empty), []);
});

// ---------------------------------------------------------------- neighbours

test("neighbours returns every neighbour, highest degree first", () => {
  const index = buildIndex(fixture());

  // institution:002pd6e78 (degree 4) outranks institution:03z12ta50 (2).
  assert.deepEqual(index.neighbours("condition:sepsis"), [
    "physionet:atlas-sepsis-ehr", // degree 5
    "physionet:atlas-sepsis-registry", // degree 5, id tie-break
  ]);
  assert.deepEqual(index.neighbours("source:physionet"), [
    "physionet:atlas-sepsis-ehr",
    "physionet:atlas-sepsis-registry",
    "physionet:atlas-eeg-stroke", // degree 4
  ]);
});

test("neighbours filters by node type", () => {
  const index = buildIndex(fixture());

  assert.deepEqual(index.neighbours("openneuro:ds000001", "modality"), ["modality:MRI"]);
  assert.deepEqual(index.neighbours("openneuro:ds000001", "dataset"), []);
  assert.deepEqual(index.neighbours("physionet:atlas-sepsis-ehr", "dataset"), [
    "physionet:atlas-sepsis-registry", // the one `related` edge in the fixture
  ]);
  assert.deepEqual(index.neighbours("modality:EEG", "dataset").length, 2);
});

test("neighbours ordering is stable regardless of input order", () => {
  const forward = buildIndex(fixture());
  const shuffled = fixture();
  shuffled.nodes.reverse();
  shuffled.links.reverse();

  assert.deepEqual(
    buildIndex(shuffled).neighbours("source:openneuro"),
    forward.neighbours("source:openneuro"),
  );
});

// ------------------------------------------------------------------ subgraph

test("subgraph keeps only the visible nodes and their internal links", () => {
  const index = buildIndex(fixture());
  const visible = new Set([
    "source:openneuro",
    "openneuro:ds000001",
    "modality:MRI",
  ]);

  const sub = index.subgraph(visible);

  assert.deepEqual(
    sub.nodes.map((n) => n.id).sort(),
    ["modality:MRI", "openneuro:ds000001", "source:openneuro"],
  );
  // ds000001 also links to a condition and an institution; both endpoints
  // must be visible for the link to survive.
  assert.deepEqual(pairs(sub.links).sort(), [
    "openneuro:ds000001|modality:MRI",
    "openneuro:ds000001|source:openneuro",
  ]);
});

test("subgraph accepts any iterable of ids and returns the shared node objects", () => {
  const index = buildIndex(fixture());
  const sub = index.subgraph(["modality:EEG", "openneuro:ds000003"]);

  assert.equal(sub.nodes.length, 2);
  assert.equal(sub.nodes[0], index.nodes.get("modality:EEG"));
  assert.equal(sub.links.length, 1);
});

// -------------------------------------------------------- deriveBackboneLinks

test("deriveBackboneLinks keeps every source–X co-occurrence, even at weight 1", () => {
  const index = buildIndex(fixture());
  const derived = deriveBackboneLinks(index);
  const w = weights(derived);

  // Every backbone node co-occurring with a source keeps that edge, so no
  // hub can float away from the source that owns it.
  assert.equal(w["source:openneuro|condition:epilepsy"], 2);
  assert.equal(w["source:openneuro|condition:stroke"], 1); // weight 1, kept anyway
  assert.equal(w["source:physionet|institution:03z12ta50"], 1);
  assert.equal(w["source:physionet|modality:EHR"], 2);

  const sourceEdges = derived.filter((l) => l.source.startsWith("source:"));
  assert.equal(sourceEdges.length, 12); // 2 sources x 6 hubs each
  // Orientation is stable: the source is always the `source` end.
  assert.ok(!derived.some((l) => l.target.startsWith("source:")));
});

test("deriveBackboneLinks keeps only weight >= 2 between non-source hubs", () => {
  const index = buildIndex(fixture());
  const derived = deriveBackboneLinks(index);

  const nonSource = derived.filter((l) => !l.source.startsWith("source:"));
  // Orientation is by node type (source, modality, condition, institution)
  // and then by id, so a hub edge always reads broad end first.
  assert.deepEqual(pairs(nonSource), [
    "condition:stroke|institution:002pd6e78",
    "modality:EHR|condition:sepsis",
    "modality:MRI|institution:002pd6e78",
  ]);
  assert.ok(nonSource.every((l) => l.weight >= 2));
  assert.equal(derived.length, 15);

  // Everything it emits is a backbone link between two backbone nodes.
  assert.ok(derived.every((l) => l.type === "backbone"));
  assert.ok(
    derived.every(
      (l) => index.backboneIds.has(l.source) && index.backboneIds.has(l.target),
    ),
  );
});

test("deriveBackboneLinks stands down when the data already has a backbone", () => {
  const raw = fixture();
  raw.links.push({
    source: "source:openneuro",
    target: "modality:MRI",
    type: "backbone",
    weight: 9,
  });

  assert.deepEqual(deriveBackboneLinks(buildIndex(raw)), []);
});

/**
 * Five modality hubs A–E, wired so the per-node top-3 rule has something to
 * cut: A–E has weight 2 (so it clears the threshold) but is in neither A's
 * nor E's three strongest edges, and is the only pair that should be lost.
 */
function topThreeGraph() {
  const hubs = ["A", "B", "C", "D", "E"];
  const nodes = hubs.map((h) => ({
    id: `modality:${h}`,
    type: "modality",
    label: h,
    backbone: true,
    degree: 0,
  }));
  nodes.push({ id: "condition:rare", type: "condition", label: "Rare", backbone: true, degree: 1 });

  const links = [];
  const datasets = [
    ["A", "B", "C", "D", "E"],
    ["A", "B", "C", "D", "E"],
    ["B", "C", "D", "E"],
    ["A", "B"],
    ["A", "B"],
    ["A", "B"],
    ["A", "C"],
    ["A", "C"],
    ["A", "D"],
  ];
  datasets.forEach((members, i) => {
    const id = `demo:d${String(i).padStart(2, "0")}`;
    nodes.push({ id, type: "dataset", label: id, degree: members.length });
    for (const member of members) {
      links.push({ source: id, target: `modality:${member}`, type: "modality" });
    }
  });
  // One lonely condition, seen once: below the weight threshold everywhere.
  links.push({ source: "demo:d08", target: "condition:rare", type: "condition" });

  for (const node of nodes) node.degree = node.degree || 0;
  return { nodes, links };
}

test("deriveBackboneLinks keeps each hub's three strongest edges and drops the rest", () => {
  const index = buildIndex(topThreeGraph());
  const derived = deriveBackboneLinks(index);
  const w = weights(derived);

  // Weights: A–B 5, A–C 4, A–D 3, A–E 2, and 3 between every other pair.
  assert.equal(w["modality:A|modality:B"], 5);
  assert.equal(w["modality:A|modality:C"], 4);
  assert.equal(w["modality:A|modality:D"], 3);

  // A's top 3 are B, C, D; E's top 3 are B, C, D. Nobody speaks for A–E.
  assert.deepEqual(pairs(derived), [
    "modality:A|modality:B",
    "modality:A|modality:C",
    "modality:A|modality:D",
    "modality:B|modality:C",
    "modality:B|modality:D",
    "modality:B|modality:E",
    "modality:C|modality:D",
    "modality:C|modality:E",
    "modality:D|modality:E",
  ]);
  assert.ok(!("modality:A|modality:E" in w));
  // The once-seen condition never reaches weight 2, so it gets no edge.
  assert.ok(!derived.some((l) => l.source.startsWith("condition:") || l.target.startsWith("condition:")));
});

test("source edges do not crowd a hub out of its own top 3", () => {
  const raw = topThreeGraph();
  // Give every dataset a source: each hub now has two heavy source edges on
  // top of its co-occurrences. Those are kept unconditionally, so they must
  // not consume the three slots reserved for hub-to-hub structure.
  raw.nodes.push(
    { id: "source:one", type: "source", label: "One", backbone: true, degree: 9 },
    { id: "source:two", type: "source", label: "Two", backbone: true, degree: 9 },
  );
  for (const node of raw.nodes) {
    if (node.type !== "dataset") continue;
    raw.links.push({ source: node.id, target: "source:one", type: "source" });
    raw.links.push({ source: node.id, target: "source:two", type: "source" });
  }

  const derived = deriveBackboneLinks(buildIndex(raw));
  const hubToHub = pairs(derived).filter((p) => !p.includes("source:"));

  assert.deepEqual(hubToHub, [
    "modality:A|modality:B",
    "modality:A|modality:C",
    "modality:A|modality:D",
    "modality:B|modality:C",
    "modality:B|modality:D",
    "modality:B|modality:E",
    "modality:C|modality:D",
    "modality:C|modality:E",
    "modality:D|modality:E",
  ]);
});

// --------------------------------------------------------------- determinism

test("deriveBackboneLinks is deterministic for identical input", () => {
  const once = deriveBackboneLinks(buildIndex(fixture()));
  const twice = deriveBackboneLinks(buildIndex(fixture()));

  assert.equal(JSON.stringify(once), JSON.stringify(twice));
});

test("deriveBackboneLinks does not depend on node or link ordering", () => {
  const forward = deriveBackboneLinks(buildIndex(fixture()));

  const shuffled = fixture();
  shuffled.nodes.reverse();
  shuffled.links.sort((a, b) => (a.target < b.target ? 1 : -1));
  const reordered = deriveBackboneLinks(buildIndex(shuffled));

  assert.equal(JSON.stringify(reordered), JSON.stringify(forward));
});

test("derived links are sorted by source then target", () => {
  const derived = deriveBackboneLinks(buildIndex(fixture()));
  const keys = derived.map((l) => [l.source, l.target]);
  const sorted = [...keys].sort(
    (a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0) ||
      (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0),
  );

  assert.deepEqual(keys, sorted);
});

// --------------------------------------------------------------- endpointId

test("endpointId reads both raw ids and force-graph's resolved objects", () => {
  assert.equal(endpointId("modality:EEG"), "modality:EEG");
  // 3d-force-graph replaces link endpoints with the node objects in place.
  assert.equal(endpointId({ id: "modality:EEG", type: "modality" }), "modality:EEG");
  assert.equal(endpointId(undefined), undefined);
});
