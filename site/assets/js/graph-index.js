/**
 * Indexing over `data/graph.json`: adjacency, neighbour lookups, subgraphs,
 * and the derived "backbone" that the scene opens with.
 *
 * Deliberately pure — no DOM, no fetch, no globals — so it runs under
 * `node --test` and so the graph page can be reasoned about without a
 * browser. Everything it returns is deterministic for a given input,
 * independent of the order nodes and links happen to arrive in.
 */

/** Hub types, broadest first. Fixes the orientation of derived links. */
const TYPE_RANK = {
  source: 0,
  modality: 1,
  condition: 2,
  institution: 3,
  dataset: 4,
};

/** How many co-occurrence edges a hub may contribute to the backbone. */
const KEEP_PER_NODE = 3;

/** Below this many shared datasets a co-occurrence isn't worth drawing. */
const MIN_WEIGHT = 2;

/** Key separator: a control character no id can contain. */
const SEP = "\u0000";

/**
 * The id at one end of a link.
 *
 * `graph.json` ships plain string ids, but 3d-force-graph rewrites both
 * ends of every link in place to point at the node objects. Anything that
 * reads a link after it has been rendered has to cope with both.
 */
export function endpointId(end) {
  return end && typeof end === "object" ? end.id : end;
}

/** Total, locale-independent string order (`localeCompare` is neither). */
function cmpId(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * Index a `{nodes, links}` payload.
 *
 * Returns `{nodes, adj, links, backboneIds, neighbours, subgraph, degreeOf}`:
 * - `nodes` — `Map<id, node>`, holding the *same* node objects that were
 *   passed in. The graph re-renders with those objects so the force layout
 *   keeps each node's position when the scene changes.
 * - `adj` — `Map<id, Set<id>>`, undirected.
 * - `links` — the input links minus self-loops and links with an unknown
 *   endpoint (real pipeline output has neither; hand-made data might).
 * - `backboneIds` — ids of the nodes flagged `backbone: true`.
 */
export function buildIndex({ nodes = [], links = [] } = {}) {
  const nodeMap = new Map();
  for (const node of nodes) {
    if (node && node.id != null) nodeMap.set(node.id, node);
  }

  const adj = new Map();
  for (const id of nodeMap.keys()) adj.set(id, new Set());

  const kept = [];
  for (const link of links) {
    if (!link) continue;
    const from = endpointId(link.source);
    const to = endpointId(link.target);
    if (from === to) continue;
    if (!nodeMap.has(from) || !nodeMap.has(to)) continue;
    adj.get(from).add(to);
    adj.get(to).add(from);
    kept.push(link);
  }

  const backboneIds = new Set();
  for (const [id, node] of nodeMap) {
    if (node.backbone === true) backboneIds.add(id);
  }

  /** The node's own degree from the pipeline, or its degree in this graph. */
  function degreeOf(id) {
    const node = nodeMap.get(id);
    if (node && Number.isFinite(node.degree)) return node.degree;
    return adj.get(id)?.size ?? 0;
  }

  /**
   * Neighbours of `id`, optionally only those of one node `type`.
   *
   * Ordered by degree descending, ties broken by id: callers that have to
   * cut the list short (an expansion cap, a label budget) then keep the
   * most connected — and always the same ones.
   */
  function neighbours(id, type) {
    const set = adj.get(id);
    if (!set) return [];

    const out = [];
    for (const other of set) {
      if (type && nodeMap.get(other)?.type !== type) continue;
      out.push(other);
    }
    out.sort((a, b) => degreeOf(b) - degreeOf(a) || cmpId(a, b));
    return out;
  }

  /** The induced subgraph over `visibleIds` (a Set or any iterable). */
  function subgraph(visibleIds) {
    const visible =
      visibleIds instanceof Set ? visibleIds : new Set(visibleIds ?? []);

    const outNodes = [];
    for (const [id, node] of nodeMap) {
      if (visible.has(id)) outNodes.push(node);
    }
    const outLinks = kept.filter(
      (link) =>
        visible.has(endpointId(link.source)) &&
        visible.has(endpointId(link.target)),
    );
    return { nodes: outNodes, links: outLinks };
  }

  return {
    nodes: nodeMap,
    adj,
    links: kept,
    backboneIds,
    neighbours,
    subgraph,
    degreeOf,
  };
}

/**
 * Co-occurrence links between backbone nodes, so the opening scene is a
 * readable map of hubs rather than a cloud of unconnected labels.
 *
 * Two hubs co-occur once per dataset that links to both; that count is the
 * link's `weight`. From those candidates:
 *
 * - **every source–hub edge is kept**, whatever its weight, so no modality,
 *   condition or institution floats away from the source it came from;
 * - **each hub additionally keeps its three heaviest non-source edges**,
 *   and only those with weight >= 2. Source edges are already unconditional,
 *   so they are excluded from that ranking — otherwise the source, which
 *   co-occurs with everything, would fill all three slots and the
 *   hub-to-hub structure would collapse into a star.
 *
 * An edge survives if *either* endpoint kept it. Returns `[]` when the
 * data already ships its own `type: 'backbone'` links — the pipeline is
 * then the authority, not this heuristic.
 */
export function deriveBackboneLinks(index) {
  if (!index || !index.nodes) return [];
  for (const link of index.links) {
    if (link.type === "backbone") return [];
  }

  const typeOf = (id) => index.nodes.get(id)?.type;
  const isHub = (id) =>
    index.backboneIds.size > 0
      ? index.backboneIds.has(id)
      : typeOf(id) !== "dataset";

  // Orientation: broad end first, so a link reads source -> modality ->
  // condition -> institution and the same pair always keys the same way.
  const orient = (a, b) => {
    const rankA = TYPE_RANK[typeOf(a)] ?? 9;
    const rankB = TYPE_RANK[typeOf(b)] ?? 9;
    if (rankA !== rankB) return rankA < rankB ? [a, b] : [b, a];
    return cmpId(a, b) <= 0 ? [a, b] : [b, a];
  };

  const weights = new Map(); // "from\0to" -> shared dataset count
  for (const [id, node] of index.nodes) {
    if (node.type !== "dataset") continue;

    const hubs = [];
    for (const other of index.adj.get(id) ?? []) {
      if (isHub(other)) hubs.push(other);
    }
    hubs.sort(cmpId);

    for (let i = 0; i < hubs.length; i += 1) {
      for (let j = i + 1; j < hubs.length; j += 1) {
        const [from, to] = orient(hubs[i], hubs[j]);
        const key = `${from}${SEP}${to}`;
        weights.set(key, (weights.get(key) ?? 0) + 1);
      }
    }
  }

  const keep = new Set();
  const candidates = new Map(); // hub id -> its non-source options

  for (const [key, weight] of weights) {
    const [from, to] = key.split(SEP);
    if (typeOf(from) === "source" || typeOf(to) === "source") {
      keep.add(key);
      continue;
    }
    if (weight < MIN_WEIGHT) continue;
    for (const [self, other] of [[from, to], [to, from]]) {
      if (!candidates.has(self)) candidates.set(self, []);
      candidates.get(self).push({ other, weight, key });
    }
  }

  for (const list of candidates.values()) {
    list.sort((a, b) => b.weight - a.weight || cmpId(a.other, b.other));
    for (const edge of list.slice(0, KEEP_PER_NODE)) keep.add(edge.key);
  }

  const out = [];
  for (const key of keep) {
    const [source, target] = key.split(SEP);
    out.push({ source, target, type: "backbone", weight: weights.get(key) });
  }
  out.sort((a, b) => cmpId(a.source, b.source) || cmpId(a.target, b.target));
  return out;
}
