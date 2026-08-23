/**
 * The 3D scene: a thin, opinionated wrapper around the vendored
 * `ForceGraph3D` UMD bundle.
 *
 * The page never opens on 2,700 datasets — it opens on the *backbone*:
 * the sources, modalities, conditions and institutions, linked by how
 * often they appear together. Datasets arrive only when a reader asks for
 * them, by clicking a hub or by searching. That keeps the first frame
 * legible and the frame rate honest.
 *
 * Everything here is presentation; the module owns no catalog knowledge
 * beyond what `graph-index.js` hands it, and reports back through
 * `onSelect` / `onExpand` rather than touching the panel or the URL.
 */

import { CAPS } from "./config.js";
import { deriveBackboneLinks, endpointId } from "./graph-index.js";
import { createLabelLayer } from "./labels.js";
import { escapeHTML, fmtNumber, accessLabel } from "./format.js";
import { prefersReducedMotion } from "./a11y.js";

/** Node radius base per type, before the degree term. */
const BASE_SIZE = {
  source: 8,
  modality: 3,
  condition: 1.6,
  institution: 1.4,
  dataset: 0.9,
};

/** How hard each type pushes its neighbours away. */
const CHARGE = {
  source: -600,
  modality: -120,
  condition: -40,
  institution: -40,
  dataset: -15,
};

const LINK_DISTANCE = { source: 90, backbone: 45, dataset: 22 };

/** Matches the CSS breakpoint where the panel becomes a bottom sheet. */
const MOBILE_QUERY = "(max-width: 899px)";

const NODE_REL_SIZE = 4;
const NODE_RESOLUTION = 12;
const MOBILE_NODE_RESOLUTION = 6;
const NODE_OPACITY = 0.92;
const LINK_OPACITY = 0.22;
const WARMUP_TICKS = 80;
const COOLDOWN_TICKS = 180;
const ALPHA_DECAY = 0.03;
const VELOCITY_DECAY = 0.35;

/** Alpha applied to whatever isn't the point right now. */
const DIM_ALPHA = 0.12; // filtered out by the domain legend
const FADE_ALPHA = 0.15; // outside the ego of a clicked node
const ISOLATE_ALPHA = 0.08; // outside the ego of a search hit

const CAMERA_MS = 800;
const FOCUS_DISTANCE = 120;
const SEED_RADIUS = 30;
const ZOOM_MS = 600;
const ZOOM_PADDING = 40;

/** Human-readable node types, for the hover tooltip. */
const TYPE_LABEL = {
  source: "Source",
  modality: "Modality",
  condition: "Condition",
  institution: "Institution",
  dataset: "Dataset",
};

/**
 * Re-express a CSS color with an alpha channel.
 *
 * three.js multiplies a color's alpha into the material opacity, which is
 * how a single accessor can both color a node and fade it. Design tokens
 * are hex; `rgb()` is handled too, and anything else is passed through
 * unchanged rather than mangled.
 */
export function toRGBA(color, alpha) {
  const value = String(color ?? "").trim();
  if (!(alpha < 1)) return value;

  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value);
  if (hex) {
    const digits =
      hex[1].length === 3
        ? [...hex[1]].map((ch) => ch + ch).join("")
        : hex[1];
    const n = Number.parseInt(digits, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  const rgb = /^rgba?\(([^)]+)\)$/i.exec(value);
  if (rgb) {
    const [r, g, b] = rgb[1].split(/[\s,/]+/).filter(Boolean);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  return value;
}

/**
 * Build the scene inside `el`.
 *
 * Options: `index` (from `buildIndex`), `colors` (from
 * `theme.readDomainColors()`), `onSelect(node|null)`, `onExpand(hub,
 * shown, total)`, and the additive `onCounts({nodes, datasets})` used to
 * keep the stats bar's "in view" figure honest.
 *
 * Returns the controls the page drives: `showBackbone`, `expand`,
 * `collapse`, `focus`, `highlight`, `setDomainFilter`, `reset`,
 * `setColors`, `pause`, `resume` (plus `counts` and `destroy`).
 */
export function createGraph(
  el,
  { index, colors, onSelect, onExpand, onCounts, tooltip } = {},
) {
  const ForceGraph3D = globalThis.ForceGraph3D;
  if (typeof ForceGraph3D !== "function") {
    throw new Error("3d-force-graph is not loaded");
  }

  const mobile = window.matchMedia(MOBILE_QUERY).matches;
  const expandCap = mobile ? CAPS.mobileExpand : CAPS.expand;
  const tooltipEl = tooltip ?? document.getElementById("scene-tooltip");

  let palette = colors ?? { domains: {}, nodes: {} };

  // The backbone the pipeline shipped, or one derived from co-occurrence.
  const backboneLinks = index.links.some((l) => l.type === "backbone")
    ? index.links.filter((l) => l.type === "backbone")
    : deriveBackboneLinks(index);
  const allLinks = [
    ...backboneLinks,
    ...index.links.filter((l) => l.type !== "backbone"),
  ];

  const visible = new Set();
  const expanded = new Map(); // hub id -> how many of its datasets are shown
  const pinned = new Set(); // brought in by focus(); never collapsed away
  let selectedId = null;
  let hoveredId = null;
  let egoIds = null;
  let egoAlpha = FADE_ALPHA;
  let domainFilter = null;
  let fitted = false;
  let pointer = { x: 0, y: 0 };

  // ------------------------------------------------------------ appearance

  function nodeBaseColor(node) {
    const domain = node.domains?.[0];
    return (
      (domain && palette.domains?.[domain]) ||
      palette.nodes?.[node.type] ||
      "#8b93a1"
    );
  }

  function passesFilter(node) {
    if (!domainFilter) return true;
    const domains = node.domains;
    // A hub with no domain of its own is structure, not subject matter:
    // keep it lit so the filtered map still has a skeleton.
    if (!domains || domains.length === 0) return node.type !== "dataset";
    return domains.some((domain) => domainFilter.has(domain));
  }

  function nodeAlpha(node) {
    let alpha = passesFilter(node) ? 1 : DIM_ALPHA;
    if (egoIds && !egoIds.has(node.id)) alpha = Math.min(alpha, egoAlpha);
    return alpha;
  }

  function nodeColor(node) {
    return toRGBA(nodeBaseColor(node), nodeAlpha(node));
  }

  function nodeVal(node) {
    const base = BASE_SIZE[node.type] ?? 1;
    const degree = Number.isFinite(node.degree)
      ? node.degree
      : index.degreeOf(node.id);
    return base * (1 + Math.log10(1 + Math.max(0, degree)));
  }

  function linkColor(link) {
    const from = index.nodes.get(endpointId(link.source));
    const to = index.nodes.get(endpointId(link.target));
    const alpha =
      from && to ? Math.min(nodeAlpha(from), nodeAlpha(to)) : 1;
    // The backbone is the map; dataset spokes are supporting detail.
    const base =
      link.type === "backbone" ? palette.text || "#d5dee8" : palette.muted || "#8b93a1";
    return toRGBA(base, alpha);
  }

  function linkDistance(link) {
    if (link.type !== "backbone") return LINK_DISTANCE.dataset;
    const from = index.nodes.get(endpointId(link.source));
    const to = index.nodes.get(endpointId(link.target));
    return from?.type === "source" || to?.type === "source"
      ? LINK_DISTANCE.source
      : LINK_DISTANCE.backbone;
  }

  /** The prescribed way to make force-graph re-read its accessors. */
  function refreshColors() {
    g.nodeColor(g.nodeColor()).linkColor(g.linkColor());
  }

  // ------------------------------------------------------------- the graph

  const g = ForceGraph3D({
    controlType: "orbit",
    rendererConfig: { antialias: !mobile, alpha: true },
  })(el)
    .backgroundColor("rgba(0,0,0,0)")
    .showNavInfo(false)
    .enableNodeDrag(false)
    .nodeRelSize(NODE_REL_SIZE)
    .nodeResolution(mobile ? MOBILE_NODE_RESOLUTION : NODE_RESOLUTION)
    .nodeOpacity(NODE_OPACITY)
    .nodeVal(nodeVal)
    .nodeColor(nodeColor)
    .nodeLabel(() => "") // we render our own tooltip into #scene-tooltip
    .linkWidth(0)
    .linkOpacity(LINK_OPACITY)
    .linkColor(linkColor)
    .linkLabel(() => "")
    .warmupTicks(WARMUP_TICKS)
    .cooldownTicks(COOLDOWN_TICKS)
    .d3AlphaDecay(ALPHA_DECAY)
    .d3VelocityDecay(VELOCITY_DECAY)
    .width(el.clientWidth)
    .height(el.clientHeight);

  g.d3Force("charge")?.strength((node) => CHARGE[node.type] ?? -40);
  g.d3Force("link")?.distance(linkDistance);

  // Retina is beautiful and expensive; two device pixels per CSS pixel is
  // as far as this scene benefits.
  const renderer = g.renderer?.();
  renderer?.setPixelRatio?.(
    Math.min(window.devicePixelRatio || 1, mobile ? 1.5 : 2),
  );

  const labels = createLabelLayer(el, {
    graph: g,
    index,
    getVisible: () => visible,
    getFocus: () => ({ hovered: hoveredId, selected: selectedId }),
    onSelect: (node) => selectFromScene(node),
    mobile,
  });

  // --------------------------------------------------------------- tooltip

  function tooltipHTML(node) {
    const degree = Number.isFinite(node.degree)
      ? node.degree
      : index.degreeOf(node.id);
    const meta = [
      TYPE_LABEL[node.type] ?? node.type,
      `${fmtNumber(degree)} ${degree === 1 ? "link" : "links"}`,
    ];
    let html = `<strong>${escapeHTML(node.label ?? node.id)}</strong>`;
    html += `<span class="tooltip-meta">${escapeHTML(meta.join(" · "))}</span>`;
    if (node.access) {
      html +=
        `<span class="badge" data-access="${escapeHTML(node.access)}">` +
        `${escapeHTML(accessLabel(node.access))}</span>`;
    }
    return html;
  }

  function positionTooltip() {
    if (!tooltipEl || tooltipEl.hidden) return;
    const margin = 12;
    const box = tooltipEl.getBoundingClientRect();
    let x = pointer.x + margin;
    let y = pointer.y + margin;
    if (x + box.width > window.innerWidth - margin) {
      x = Math.max(margin, pointer.x - box.width - margin);
    }
    if (y + box.height > window.innerHeight - margin) {
      y = Math.max(margin, pointer.y - box.height - margin);
    }
    tooltipEl.style.left = `${Math.round(x)}px`;
    tooltipEl.style.top = `${Math.round(y)}px`;
  }

  function showTooltip(node) {
    if (!tooltipEl) return;
    if (!node) {
      tooltipEl.hidden = true;
      tooltipEl.innerHTML = "";
      return;
    }
    tooltipEl.innerHTML = tooltipHTML(node);
    tooltipEl.hidden = false;
    positionTooltip();
  }

  // ----------------------------------------------------------- scene state

  function counts() {
    let datasets = 0;
    for (const id of visible) {
      if (index.nodes.get(id)?.type === "dataset") datasets += 1;
    }
    return { nodes: visible.size, datasets };
  }

  function applyScene() {
    const nodes = [];
    for (const id of visible) {
      const node = index.nodes.get(id);
      if (node) nodes.push(node);
    }
    const links = allLinks.filter(
      (link) =>
        visible.has(endpointId(link.source)) &&
        visible.has(endpointId(link.target)),
    );

    // The same node objects every time: d3 keeps their positions, so an
    // expansion grows the scene instead of reshuffling it.
    g.graphData({ nodes, links });
    labels.refresh();
    onCounts?.(counts());
  }

  function seedNear(node, anchor) {
    if (Number.isFinite(node.x)) return;
    const jitter = () => (Math.random() - 0.5) * 2 * SEED_RADIUS;
    node.x = (anchor?.x ?? 0) + jitter();
    node.y = (anchor?.y ?? 0) + jitter();
    node.z = (anchor?.z ?? 0) + jitter();
  }

  /**
   * Run `fn` once the node has real coordinates.
   *
   * A node added this tick has no position until the engine runs, and
   * flying the camera to (0, 0, 0) is a bad first impression.
   */
  function whenPositioned(node, fn, tries = 120) {
    if (Number.isFinite(node.x) || tries <= 0) {
      fn();
      return;
    }
    requestAnimationFrame(() => whenPositioned(node, fn, tries - 1));
  }

  function tweenTo(node) {
    // A deliberate camera move outranks the opening fit: without this, a
    // `?node=` deep link is framed and then yanked back out when the
    // backbone finally settles a few seconds later.
    fitted = true;
    whenPositioned(node, () => {
      const ms = prefersReducedMotion() ? 0 : CAMERA_MS;
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const z = node.z ?? 0;
      const dist = Math.hypot(x, y, z);
      const to =
        dist > 1e-6
          ? {
              x: x * (1 + FOCUS_DISTANCE / dist),
              y: y * (1 + FOCUS_DISTANCE / dist),
              z: z * (1 + FOCUS_DISTANCE / dist),
            }
          : { x: 0, y: 0, z: FOCUS_DISTANCE };
      g.cameraPosition(to, { x, y, z }, ms);
    });
  }

  /** The node plus its neighbours that are actually on screen. */
  function egoOf(id) {
    const ego = new Set([id]);
    for (const other of index.neighbours(id)) {
      if (visible.has(other)) ego.add(other);
    }
    return ego;
  }

  // ------------------------------------------------------------ public API

  function showBackbone() {
    visible.clear();
    expanded.clear();
    pinned.clear();
    for (const id of index.backboneIds) visible.add(id);
    if (visible.size === 0) {
      // Data without backbone flags: everything that isn't a dataset.
      for (const [id, node] of index.nodes) {
        if (node.type !== "dataset") visible.add(id);
      }
    }
    applyScene();
    return api;
  }

  function expand(hubId, { more = false } = {}) {
    const hub = index.nodes.get(hubId);
    if (!hub) return api;

    const neighbours = index.neighbours(hubId, "dataset");
    const total = neighbours.length;
    if (total === 0) {
      onExpand?.(hub, 0, 0);
      return api;
    }

    const already = expanded.get(hubId) ?? 0;
    const shown = Math.min(
      total,
      more ? already + expandCap : Math.max(already, expandCap),
    );
    expanded.set(hubId, shown);

    for (const id of neighbours.slice(0, shown)) {
      const node = index.nodes.get(id);
      if (!node) continue;
      seedNear(node, hub);
      visible.add(id);
    }
    applyScene();
    onExpand?.(hub, shown, total);
    return api;
  }

  function collapse(hubId) {
    if (!expanded.has(hubId)) return api;
    expanded.delete(hubId);

    // A dataset may hang off two expanded hubs; only drop the orphans.
    const keep = new Set(pinned);
    if (selectedId) keep.add(selectedId);
    for (const [otherId, shown] of expanded) {
      for (const id of index.neighbours(otherId, "dataset").slice(0, shown)) {
        keep.add(id);
      }
    }
    for (const id of index.neighbours(hubId, "dataset")) {
      if (!keep.has(id)) visible.delete(id);
    }
    applyScene();
    return api;
  }

  function focus(nodeId, { isolate = false } = {}) {
    const node = index.nodes.get(nodeId);
    if (!node) return api;

    if (node.type === "dataset") {
      // Land the dataset next to whichever of its hubs is already placed,
      // then hang any unplaced hubs off the dataset in turn.
      let anchor = null;
      for (const id of index.neighbours(nodeId)) {
        const hub = index.nodes.get(id);
        if (hub && Number.isFinite(hub.x)) {
          anchor = hub;
          break;
        }
      }
      seedNear(node, anchor);
      visible.add(nodeId);
      pinned.add(nodeId);

      for (const id of index.neighbours(nodeId)) {
        const neighbour = index.nodes.get(id);
        if (!neighbour) continue;
        seedNear(neighbour, node);
        visible.add(id);
        pinned.add(id);
      }
    } else {
      expand(nodeId);
      visible.add(nodeId);
    }

    selectedId = nodeId;
    hoveredId = null;
    showTooltip(null);
    egoIds = egoOf(nodeId);
    egoAlpha = isolate ? ISOLATE_ALPHA : FADE_ALPHA;
    applyScene();
    tweenTo(node);
    return api;
  }

  function highlight(ids) {
    if (ids === null || ids === undefined) egoIds = null;
    else egoIds = ids instanceof Set ? ids : new Set(ids);
    egoAlpha = FADE_ALPHA;
    refreshColors();
    labels.refresh();
    return api;
  }

  function setDomainFilter(domains) {
    domainFilter =
      domains === null || domains === undefined
        ? null
        : domains instanceof Set
          ? domains
          : new Set(domains);
    refreshColors();
    return api;
  }

  function clearSelection() {
    selectedId = null;
    egoIds = null;
    hoveredId = null;
    showTooltip(null);
    refreshColors();
    labels.refresh();
  }

  function reset() {
    domainFilter = null;
    clearSelection();
    showBackbone();
    g.zoomToFit(prefersReducedMotion() ? 0 : ZOOM_MS, ZOOM_PADDING);
    return api;
  }

  function setColors(next) {
    if (next) palette = next;
    refreshColors();
    return api;
  }

  // ---------------------------------------------------------- run/pause

  let pausedByUser = false;
  let pausedByVisibility = false;
  let animating = true;

  function syncRunState() {
    const shouldRun = !pausedByUser && !pausedByVisibility;
    if (shouldRun === animating) return;
    animating = shouldRun;
    if (shouldRun) {
      g.resumeAnimation();
      labels.start();
    } else {
      g.pauseAnimation();
      labels.stop();
    }
  }

  function pause() {
    pausedByUser = true;
    syncRunState();
    return api;
  }

  function resume() {
    pausedByUser = false;
    syncRunState();
    return api;
  }

  const onVisibility = () => {
    pausedByVisibility = document.hidden;
    syncRunState();
  };
  document.addEventListener("visibilitychange", onVisibility);

  // ------------------------------------------------------------- wiring

  function selectFromScene(node) {
    if (!node) return;
    if (node.type === "dataset") {
      selectedId = node.id;
      egoIds = egoOf(node.id);
      egoAlpha = FADE_ALPHA;
      refreshColors();
      labels.refresh();
      tweenTo(node);
      onSelect?.(node);
      return;
    }
    // A hub answers a click by opening up, so it needs no ego highlight —
    // but any highlight left over from the last selection has to go.
    selectedId = node.id;
    egoIds = null;
    refreshColors();
    expand(node.id);
    onSelect?.(node);
  }

  g.onNodeClick((node) => selectFromScene(node));
  g.onNodeHover((node) => {
    hoveredId = node ? node.id : null;
    showTooltip(node);
    labels.refresh();
  });
  g.onBackgroundClick(() => {
    clearSelection();
    onSelect?.(null);
  });
  g.onEngineStop(() => {
    if (!fitted) {
      fitted = true;
      g.zoomToFit(prefersReducedMotion() ? 0 : ZOOM_MS, ZOOM_PADDING);
    }
    labels.refresh();
  });

  const trackPointer = (event) => {
    pointer = { x: event.clientX, y: event.clientY };
    if (hoveredId) positionTooltip();
  };
  // `pointerover`/`down` as well as `move`, so the tooltip is never
  // stranded at the origin when the pointer arrives without moving.
  el.addEventListener("pointermove", trackPointer);
  el.addEventListener("pointerover", trackPointer);
  el.addEventListener("pointerdown", trackPointer);
  el.addEventListener("pointerleave", () => {
    if (hoveredId === null) return;
    hoveredId = null;
    showTooltip(null);
    labels.refresh();
  });

  const observer = new ResizeObserver(() => {
    g.width(el.clientWidth).height(el.clientHeight);
    labels.position();
  });
  observer.observe(el);

  function destroy() {
    observer.disconnect();
    document.removeEventListener("visibilitychange", onVisibility);
    labels.destroy();
    showTooltip(null);
    g._destructor?.();
  }

  labels.start();

  const api = {
    showBackbone,
    expand,
    collapse,
    focus,
    highlight,
    setDomainFilter,
    reset,
    setColors,
    pause,
    resume,
    counts,
    destroy,
    /** Escape hatch for the entry module (zoom, camera, debugging). */
    graph: g,
  };
  return api;
}
