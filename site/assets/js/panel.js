/**
 * The record panel: a desktop drawer / mobile bottom sheet (the CSS in
 * `components.css`/`layout.css` handles which, from `#panel`'s markup —
 * this module only ever sets `hidden` and renders into `#panel-body`).
 *
 * Shared by both pages (`mode: "graph"|"table"`): the graph page shows a
 * dataset (`showDataset`) or a hub entity's expansion state
 * (`showEntity`); the table page only ever calls `showDataset`.
 *
 * Progressive render: `showDataset` paints immediately from whatever
 * `node`/`row` the caller already has in memory, then fetches the full
 * catalog record (`data/records/<source>/<native>.json`) in the
 * background and repaints with it. A 404/network failure leaves the
 * row-level card up with a "full record unavailable" note rather than
 * blocking or erroring.
 *
 * Focus is only moved to `#panel-title` on an actual hidden -> visible
 * transition, or when the caller passes `{focus: true}` (a deliberate new
 * selection, e.g. from search, while the panel is already open on
 * something else). Any other call while the panel is already open - a
 * graph hub's repeated "Show more", or the same dataset repainting once
 * its full record arrives - is treated as an in-place update: the body
 * re-renders, focus stays exactly where the reader left it.
 *
 * This module is not part of the pure/Node-tested set (it is inherently
 * DOM + network); nothing at module scope touches `document`/`window`,
 * so importing it is still harmless without a DOM.
 */

import { BASE_URL, BUILD, REPO_URL } from "./config.js";
import { escapeHTML, autolink, fmtNumber, fmtBytes, accessLabel, countryName } from "./format.js";
import { onEscape } from "./a11y.js";
import { domainLabel, sourceLabel, capitalize } from "./filters.js";

async function loadRecord(id) {
  const [source, ...rest] = String(id ?? "").split(":");
  const native = rest.join(":");
  if (!source || !native) return null;
  const url = `${BASE_URL}data/records/${encodeURIComponent(source)}/${encodeURIComponent(native)}.json?v=${BUILD}`;
  const res = await fetch(url);
  if (!res.ok) return null;
  return res.json();
}

function conditionLabels(list) {
  return (Array.isArray(list) ? list : [])
    .map((c) => (typeof c === "string" ? c : c?.label))
    .filter(Boolean);
}

// Every href this module renders from harvested/record data goes through
// this scheme allowlist, in addition to escaping the attribute value -
// escaping alone stops attribute breakout, not a `javascript:`/`data:`
// href that's otherwise well-formed. Mirrors app-graph's `safeHref`.
const SAFE_HREF_RE = /^https?:\/\//i;

function safeHref(url) {
  const s = String(url ?? "").trim();
  return SAFE_HREF_RE.test(s) ? s : null;
}

function doiUrl(doi) {
  if (!doi) return null;
  const clean = String(doi).replace(/^doi:/i, "").trim();
  return clean ? safeHref(`https://doi.org/${clean}`) : null;
}

function yearsFactText(years) {
  const start = years?.start;
  const end = years?.end;
  if (start == null && end == null) return null;
  if (start != null && end != null) return start === end ? String(start) : `${start}–${end}`;
  if (start != null) return `${start}–`;
  return `–${end}`;
}

/** Merge whatever's known so far (node/row/full record) into one shape. */
function normalizeRecord({ id, node, row, record }) {
  return {
    id,
    name: record?.name ?? row?.name ?? node?.label ?? id,
    summary: record?.summary ?? row?.summary ?? "",
    source: record?.source ?? row?.source ?? node?.source,
    access: record?.access ?? row?.access ?? node?.access,
    species: record?.species ?? row?.species,
    population: record?.population,
    domains: record?.domains ?? row?.domains ?? node?.domains ?? [],
    modalities: record?.modalities ?? row?.modalities ?? [],
    conditions: conditionLabels(record?.conditions ?? row?.conditions),
    countries: record?.countries ?? row?.countries ?? [],
    years: record?.years ?? row?.years,
    sample_size: record?.sample_size ?? row?.sample_size,
    sample_unit: record?.sample_unit ?? row?.sample_unit,
    size_bytes: record?.size_bytes,
    license: record?.license,
    version: record?.version,
    published: record?.published,
    dataset_doi: record?.dataset_doi,
    url: record?.url ?? row?.url,
    institutions: record?.institutions,
    authors: record?.authors,
    papers: record?.papers,
    access_notes: record?.access_notes,
    access_howto: record?.access_howto,
    provenance: record?.provenance,
    full: Boolean(record),
  };
}

function renderBadges(data) {
  const badges = [];
  if (data.source) badges.push(`<span class="badge">${escapeHTML(sourceLabel(data.source))}</span>`);
  if (data.access) {
    badges.push(
      `<span class="badge" data-access="${escapeHTML(data.access)}">${escapeHTML(accessLabel(data.access))}</span>`,
    );
  }
  const line2 = [];
  if (data.species) line2.push(escapeHTML(capitalize(data.species)));
  if (data.population) line2.push(escapeHTML(data.population));

  return `
    ${badges.length ? `<div class="chip-list">${badges.join("")}</div>` : ""}
    ${line2.length ? `<p class="muted text-sm">${line2.join(" · ")}</p>` : ""}
  `;
}

function renderPrimaryLinks(data) {
  const parts = [];
  const openHref = safeHref(data.url);
  if (openHref) {
    parts.push(
      `<p><a class="btn btn-primary" href="${escapeHTML(openHref)}" target="_blank" rel="noopener">Open dataset ↗</a></p>`,
    );
  }
  const doiHref = doiUrl(data.dataset_doi);
  if (doiHref) {
    parts.push(
      `<p class="text-sm"><a href="${escapeHTML(doiHref)}" target="_blank" rel="noopener">${escapeHTML(data.dataset_doi)}</a></p>`,
    );
  }
  return parts.join("");
}

function renderChips(data) {
  const groups = [
    { kind: "domain", values: data.domains, labelFor: domainLabel, colored: true },
    { kind: "modality", values: data.modalities, labelFor: (v) => v, colored: false },
    { kind: "condition", values: data.conditions, labelFor: capitalize, colored: false },
  ];
  const chips = [];
  for (const group of groups) {
    for (const value of group.values ?? []) {
      const domainAttr = group.colored ? ` data-domain="${escapeHTML(value)}"` : "";
      chips.push(
        `<button type="button" class="chip"${domainAttr} data-chip-kind="${group.kind}" data-chip-value="${escapeHTML(value)}">${escapeHTML(group.labelFor(value))}</button>`,
      );
    }
  }
  if (chips.length === 0) return "";
  return `<div class="chip-list panel-section">${chips.join("")}</div>`;
}

function renderFacts(data) {
  const rows = [];
  if (data.sample_size != null) {
    const unit = data.sample_unit ? ` ${escapeHTML(data.sample_unit)}` : "";
    rows.push(["Sample size", `${escapeHTML(fmtNumber(data.sample_size))}${unit}`]);
  }
  const years = yearsFactText(data.years);
  if (years) rows.push(["Years", escapeHTML(years)]);
  if (data.countries?.length) {
    rows.push(["Countries", data.countries.map((c) => escapeHTML(countryName(c))).join(", ")]);
  }
  if (data.license) rows.push(["License", escapeHTML(data.license)]);
  if (data.size_bytes != null) rows.push(["Size", escapeHTML(fmtBytes(data.size_bytes))]);
  if (data.version) rows.push(["Version", escapeHTML(data.version)]);
  if (data.published) rows.push(["Published", escapeHTML(data.published)]);

  if (rows.length === 0) return "";
  const dl = rows.map(([dt, dd]) => `<dt>${escapeHTML(dt)}</dt><dd>${dd}</dd>`).join("");
  return `<div class="panel-section"><h3>Facts</h3><dl class="facts">${dl}</dl></div>`;
}

function renderInstitutions(data) {
  const items = data.institutions ?? [];
  if (items.length === 0) return "";
  const lis = items
    .map((inst) => {
      const country = inst.country ? ` · ${escapeHTML(countryName(inst.country))}` : "";
      return `<li>${escapeHTML(inst.name ?? "")}${country}</li>`;
    })
    .join("");
  return `<div class="panel-section"><h3>Institutions</h3><ul>${lis}</ul></div>`;
}

function renderAuthors(data, { authorsExpanded }) {
  const items = data.authors ?? [];
  if (items.length === 0) return "";
  const shown = authorsExpanded ? items : items.slice(0, 5);
  const lis = shown.map((a) => `<li>${escapeHTML(a.name ?? "")}</li>`).join("");
  const remaining = items.length - shown.length;
  const more =
    remaining > 0
      ? `<button type="button" class="btn-quiet text-sm" data-show-all-authors>Show all ${items.length}</button>`
      : "";
  return `<div class="panel-section"><h3>Authors</h3><ul>${lis}</ul>${more}</div>`;
}

function renderPapers(data) {
  const items = data.papers ?? [];
  if (items.length === 0) return "";
  const lis = items
    .map((p) => {
      const href = doiUrl(p.doi);
      const label = escapeHTML(p.title || p.doi || "");
      const relation = p.relation ? ` <span class="muted text-sm">(${escapeHTML(p.relation)})</span>` : "";
      return href
        ? `<li><a href="${escapeHTML(href)}" target="_blank" rel="noopener">${label}</a>${relation}</li>`
        : `<li>${label}${relation}</li>`;
    })
    .join("");
  return `<div class="panel-section"><h3>Papers</h3><ul>${lis}</ul></div>`;
}

function renderAccessNotes(data) {
  const parts = [];
  if (data.access_notes) parts.push(["Access notes", data.access_notes]);
  if (data.access_howto) parts.push(["How to get access", data.access_howto]);
  if (parts.length === 0) return "";
  const body = parts
    .map(
      ([label, text]) =>
        `<p><strong>${escapeHTML(label)}:</strong><br>${autolink(text).replace(/\n/g, "<br>")}</p>`,
    )
    .join("");
  return `<div class="panel-section"><h3>Access</h3>${body}</div>`;
}

function renderProvenance(data) {
  const prov = data.provenance;
  if (!prov) return "";
  const bits = [];
  if (data.source) bits.push(`Source: ${escapeHTML(sourceLabel(data.source))}`);
  if (prov.harvested_via) bits.push(`harvested via ${escapeHTML(prov.harvested_via)}`);
  if (prov.harvested_at) bits.push(`first seen ${escapeHTML(prov.harvested_at)}`);
  if (prov.last_verified) bits.push(`last verified ${escapeHTML(prov.last_verified)}`);
  const enrichment = prov.enrichment;
  if (enrichment?.method) {
    const model = enrichment.model ? ` (${escapeHTML(enrichment.model)})` : "";
    bits.push(`enrichment: ${escapeHTML(enrichment.method)}${model}`);
  }
  if (bits.length === 0) return "";
  return `<p class="panel-fineprint">${bits.join(" · ")}</p>`;
}

function renderFooter(data, { mode, id }) {
  const editHref = safeHref(
    `${REPO_URL}/issues/new?template=dataset-edit.yml&title=${encodeURIComponent(`[edit] ${id}`)}`,
  );
  const crossLabel = mode === "graph" ? "View in table" : "View in graph";
  const editLink = editHref
    ? `<a href="${escapeHTML(editHref)}" target="_blank" rel="noopener">Suggest an edit</a> · `
    : "";
  return `
    <p class="panel-fineprint">
      ${editLink}<button type="button" class="btn-quiet text-sm" data-navigate-id="${escapeHTML(id)}">${crossLabel}</button>
    </p>
  `;
}

function renderDatasetBody(data, ctx, { authorsExpanded }) {
  const parts = [
    renderBadges(data),
    renderPrimaryLinks(data),
    data.summary ? `<p>${escapeHTML(data.summary)}</p>` : "",
    renderChips(data),
    renderFacts(data),
  ];

  if (data.full) {
    parts.push(
      renderInstitutions(data),
      renderAuthors(data, { authorsExpanded }),
      renderPapers(data),
      renderAccessNotes(data),
      renderProvenance(data),
    );
  } else if (data.fetchFailed) {
    parts.push('<p class="muted text-sm">Full record unavailable.</p>');
  } else {
    parts.push('<p class="muted text-sm" data-pending>Loading full record…</p>');
  }

  parts.push(renderFooter(data, ctx));
  return parts.filter(Boolean).join("\n");
}

function renderEntityBody(node, { shown, total }) {
  const parts = [];
  if (node?.type) parts.push(`<p class="muted text-sm">${escapeHTML(capitalize(node.type))}</p>`);
  if (typeof node?.degree === "number") {
    parts.push(
      `<dl class="facts"><dt>Connections</dt><dd>${escapeHTML(fmtNumber(node.degree))}</dd></dl>`,
    );
  }
  if (typeof shown === "number" && typeof total === "number") {
    parts.push(`<p>Showing ${escapeHTML(fmtNumber(shown))} of ${escapeHTML(fmtNumber(total))}</p>`);
    const controls = [];
    if (shown < total) controls.push('<button type="button" class="btn" data-entity-more>Show more</button>');
    controls.push('<button type="button" class="btn btn-quiet" data-entity-collapse>Collapse</button>');
    parts.push(`<p>${controls.join(" ")}</p>`);
  }
  return parts.join("\n");
}

/**
 * Wire the panel `<aside>` (`#panel`, with `.panel-head` containing
 * `#panel-title` + `button[data-panel-close]`, and an empty
 * `#panel-body`). Returns `{ showDataset, showEntity, close }`.
 */
export function createPanel(aside, { mode = "table", onChip, onNavigate, onClose } = {}) {
  const titleEl = aside?.querySelector("#panel-title") ?? aside?.querySelector(".panel-title");
  const bodyEl = aside?.querySelector("#panel-body") ?? aside?.querySelector(".panel-body");
  const closeBtn = aside?.querySelector("[data-panel-close]");

  let renderToken = 0;
  let previouslyFocused = null;
  let currentId = null;
  let authorsExpanded = false;
  let entityCallbacks = {};
  let lastData = null;
  let lastCtx = null;

  function isOpen() {
    return Boolean(aside) && !aside.hidden;
  }

  function paintDataset(data, ctx) {
    lastData = data;
    lastCtx = ctx;
    if (titleEl) titleEl.textContent = data.name || data.id || "";
    if (bodyEl) bodyEl.innerHTML = renderDatasetBody(data, ctx, { authorsExpanded });
  }

  /**
   * Make the panel visible, focusing `#panel-title` only on an actual
   * hidden -> visible transition (opening the panel) or when the caller
   * explicitly asks (`forceFocus`, for a deliberate new selection - e.g.
   * search - while the panel is already open on something else). A call
   * that arrives while the panel is already open and `forceFocus` is
   * falsy is an in-place UPDATE (a graph hub's "Show more", or the same
   * dataset repainting once the full record loads): the body still
   * re-renders, but focus is left exactly where the reader put it.
   */
  function reveal(forceFocus) {
    if (!aside) return;
    const wasOpen = isOpen();
    aside.hidden = false;
    if (!wasOpen || forceFocus) {
      previouslyFocused = document.activeElement;
      if (titleEl) {
        titleEl.tabIndex = -1;
        titleEl.focus();
      }
    }
  }

  function close() {
    if (!aside) return;
    renderToken += 1; // invalidate any in-flight fetch's repaint
    aside.hidden = true;
    currentId = null;
    lastData = null;
    lastCtx = null;
    onClose?.();
    const opener = previouslyFocused;
    previouslyFocused = null;
    if (opener && document.contains(opener) && typeof opener.focus === "function") {
      opener.focus();
    }
  }

  function showDataset(id, { node, row, focus } = {}) {
    const token = ++renderToken;
    currentId = id;
    authorsExpanded = false;

    const initial = normalizeRecord({ id, node, row });
    paintDataset(initial, { mode, id });
    reveal(focus === true);

    loadRecord(id)
      .then((record) => {
        if (token !== renderToken) return; // superseded by a later selection
        const data = record
          ? normalizeRecord({ id, node, row, record })
          : { ...initial, fetchFailed: true };
        paintDataset(data, { mode, id });
      })
      .catch(() => {
        if (token !== renderToken) return;
        paintDataset({ ...initial, fetchFailed: true }, { mode, id });
      });
  }

  function showEntity(node, { shown, total, onMore, onCollapse, focus } = {}) {
    renderToken += 1; // no async fetch here, but keep any prior one from landing
    currentId = null;
    entityCallbacks = { onMore, onCollapse };
    lastData = null;
    lastCtx = null;

    if (titleEl) titleEl.textContent = node?.label ?? node?.id ?? "";
    if (bodyEl) bodyEl.innerHTML = renderEntityBody(node, { shown, total });
    reveal(focus === true);
  }

  closeBtn?.addEventListener("click", () => close());

  bodyEl?.addEventListener("click", (event) => {
    const chipEl = event.target.closest("[data-chip-kind]");
    if (chipEl) {
      onChip?.({ kind: chipEl.dataset.chipKind, value: chipEl.dataset.chipValue });
      return;
    }
    if (event.target.closest("[data-show-all-authors]")) {
      authorsExpanded = true;
      if (lastData) paintDataset(lastData, lastCtx);
      return;
    }
    if (event.target.closest("[data-entity-more]")) {
      entityCallbacks.onMore?.();
      return;
    }
    if (event.target.closest("[data-entity-collapse]")) {
      entityCallbacks.onCollapse?.();
      return;
    }
    const navEl = event.target.closest("[data-navigate-id]");
    if (navEl) {
      onNavigate?.(navEl.dataset.navigateId || currentId);
    }
  });

  onEscape(() => {
    if (!isOpen()) return false;
    close();
    return true;
  });

  return { showDataset, showEntity, close };
}
