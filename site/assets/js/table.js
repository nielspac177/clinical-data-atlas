/**
 * The dataset table: `createTable` is the DOM adapter (renders into
 * `tbody`, wires the sortable headers it finds on its own `<table>` and
 * the well-known `#show-more`/`#show-all` buttons); `toCSV` is a pure,
 * top-level export (no DOM) so it's directly testable under Node.
 */

import { ACCESS } from "./config.js";
import { accessLabel, countryName, fmtNumber } from "./format.js";
import { announce } from "./a11y.js";
import { capitalize, domainLabel, sourceLabel } from "./filters.js";

const CHUNK = 100;
const DOMAINS_SHOWN = 2;
const MODALITIES_SHOWN = 3;

function accessRank(access) {
  const i = ACCESS.indexOf(access);
  return i === -1 ? ACCESS.length : i;
}

// One sort key function per sortable column. Only columns listed here are
// wired to their header's click-to-sort behaviour.
const SORTERS = {
  name: (row) => String(row.name ?? "").toLowerCase(),
  source: (row) => String(row.source ?? "").toLowerCase(),
  access: (row) => accessRank(row.access),
  years: (row) => (row.years?.start ?? row.years?.end ?? null),
  sample_size: (row) => (typeof row.sample_size === "number" ? row.sample_size : null),
};

function compareRows(a, b, column, dir) {
  const getKey = SORTERS[column] ?? SORTERS.name;
  const av = getKey(a);
  const bv = getKey(b);
  const aMissing = av === null || av === undefined;
  const bMissing = bv === null || bv === undefined;

  if (aMissing && bMissing) return secondaryCompare(a, b);
  if (aMissing) return 1; // unknown values always sort last, either direction
  if (bMissing) return -1;

  let cmp;
  if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
  else cmp = String(av).localeCompare(String(bv));

  if (cmp === 0) return secondaryCompare(a, b);
  return dir === "desc" ? -cmp : cmp;
}

function secondaryCompare(a, b) {
  return String(a.name ?? "").localeCompare(String(b.name ?? ""));
}

function yearsText(years) {
  const start = years?.start;
  const end = years?.end;
  if (start == null && end == null) return "—";
  if (start != null && end != null) return start === end ? String(start) : `${start}–${end}`;
  if (start != null) return `${start}–`;
  return `–${end}`;
}

function textCell(text) {
  const td = document.createElement("td");
  td.textContent = text || "—";
  return td;
}

function nameCell(row, onRowSelect, active) {
  const td = document.createElement("td");
  td.className = "cell-name";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-quiet";
  if (active) button.setAttribute("aria-current", "true");
  button.textContent = row.name || row.id;
  button.addEventListener("click", () => onRowSelect?.(row.id));
  td.appendChild(button);
  return td;
}

function listCell(values, limit, labelFn, colored) {
  const td = document.createElement("td");
  const list = Array.isArray(values) ? values : [];
  if (list.length === 0) {
    td.textContent = "—";
    return td;
  }
  const wrap = document.createElement("span");
  wrap.className = "cell-chips";
  const shown = list.slice(0, limit);
  for (const value of shown) {
    const chip = document.createElement("span");
    chip.className = "chip";
    if (colored) chip.dataset.domain = value;
    chip.textContent = labelFn(value);
    wrap.appendChild(chip);
  }
  const extra = list.length - shown.length;
  if (extra > 0) {
    const more = document.createElement("span");
    more.className = "muted text-sm";
    more.textContent = `+${extra}`;
    wrap.appendChild(more);
  }
  td.appendChild(wrap);
  return td;
}

function accessCell(access) {
  const td = document.createElement("td");
  if (!access) {
    td.textContent = "—";
    return td;
  }
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.dataset.access = access;
  badge.textContent = accessLabel(access);
  td.appendChild(badge);
  return td;
}

function sampleCell(size, unit) {
  const td = document.createElement("td");
  td.className = "cell-num";
  if (size === null || size === undefined) {
    td.textContent = "—";
    return td;
  }
  td.textContent = unit ? `${fmtNumber(size)} ${unit}` : fmtNumber(size);
  return td;
}

function buildRow(row, { onRowSelect, active }) {
  const tr = document.createElement("tr");
  tr.dataset.id = row.id;
  // `aria-selected` isn't a valid state on a plain <tr> (no grid/row
  // role here) — assistive tech would just ignore it. `data-active` is a
  // styling hook only; the real "this is the open record" semantic lives
  // on the Name button's `aria-current="true"` (see nameCell).
  if (active) tr.dataset.active = "true";

  tr.append(
    nameCell(row, onRowSelect, active),
    textCell(sourceLabel(row.source)),
    listCell(row.domains, DOMAINS_SHOWN, domainLabel, true),
    listCell(row.modalities, MODALITIES_SHOWN, (m) => m, false),
    accessCell(row.access),
    textCell(yearsText(row.years)),
    sampleCell(row.sample_size, row.sample_unit),
    textCell((row.countries ?? []).map((c) => countryName(c)).join(", ")),
    textCell(capitalize(row.species)),
  );
  return tr;
}

/**
 * Create the table controller bound to `tbody` (`#table-body`) and
 * `statusEl` (`#table-status`). Owns its own sort state and pagination;
 * finds its `<table>` via `tbody.closest("table")` to wire the sortable
 * `th[data-column]` headers, and the show-more/show-all buttons by their
 * well-known ids (`#show-more`/`#show-all`) unless overridden.
 *
 * `onRowSelect(id)` fires when a row's Name button is activated;
 * `onSort(column, dir)` fires when a *header click* changes the sort (not
 * when `sortBy()` is called programmatically), so the caller can persist
 * it (e.g. to the URL) without an echo loop.
 */
export function createTable(
  tbody,
  statusEl,
  { rows = [], onRowSelect, onSort, showMoreBtn, showAllBtn } = {},
) {
  let allRows = [];
  let catalogTotal = 0;
  let sortColumn = "name";
  let sortDir = "asc";
  let visibleCount = CHUNK;
  let activeId = null;

  const headerEls = new Map();
  const tableEl = tbody?.closest?.("table") ?? null;
  if (tableEl) {
    for (const th of tableEl.querySelectorAll("thead th[data-column]")) {
      const column = th.getAttribute("data-column");
      headerEls.set(column, th);
      const button = th.querySelector("button");
      if (!button || !SORTERS[column]) continue;
      button.addEventListener("click", () => {
        const nextDir = sortColumn === column && sortDir === "asc" ? "desc" : "asc";
        applySort(column, nextDir);
        onSort?.(column, nextDir);
      });
    }
  }

  const moreBtn = showMoreBtn ?? document.getElementById("show-more");
  const allBtn = showAllBtn ?? document.getElementById("show-all");
  moreBtn?.addEventListener("click", () => showMore());
  allBtn?.addEventListener("click", () => showAll());

  function updateHeaderSort() {
    for (const [column, th] of headerEls) {
      if (!th.hasAttribute("aria-sort")) continue;
      th.setAttribute(
        "aria-sort",
        column === sortColumn ? (sortDir === "desc" ? "descending" : "ascending") : "none",
      );
    }
  }

  function updateMoreButtons() {
    const hasMore = visibleCount < allRows.length;
    if (moreBtn) moreBtn.hidden = !hasMore;
    if (allBtn) allBtn.hidden = !hasMore;
  }

  function updateStatus() {
    // Per the plan's own example ("312 of 2,704 datasets"): the two
    // numbers are the filtered count and the *catalog* total, not the
    // rendered-so-far count and the filtered count — pagination gets its
    // own trailing clause instead, so "everything currently loaded
    // matches, and here's how much of that is actually on screen" stays
    // two distinct, unambiguous facts.
    const filtered = allRows.length;
    const shown = Math.min(visibleCount, filtered);
    let text = `${fmtNumber(filtered)} of ${fmtNumber(catalogTotal)} datasets`;
    if (shown < filtered) text += ` · showing ${fmtNumber(shown)}`;
    if (statusEl) statusEl.textContent = text;
    announce(text);
  }

  function render() {
    const sorted = [...allRows].sort((a, b) => compareRows(a, b, sortColumn, sortDir));
    const visible = sorted.slice(0, visibleCount);
    if (tbody) {
      tbody.textContent = "";
      const frag = document.createDocumentFragment();
      for (const row of visible) {
        frag.appendChild(buildRow(row, { onRowSelect, active: row.id === activeId }));
      }
      tbody.appendChild(frag);
    }
    updateMoreButtons();
    updateHeaderSort();
    updateStatus();
  }

  function applySort(column, dir) {
    sortColumn = SORTERS[column] ? column : "name";
    sortDir = dir === "desc" ? "desc" : "asc";
    visibleCount = CHUNK;
    render();
  }

  function setRows(newRows, { total } = {}) {
    allRows = Array.isArray(newRows) ? newRows : [];
    // `total` is the catalog-wide count (independent of any filtering);
    // defaults to the given rows' own length so a caller that never
    // passes it still gets a sane (if filter-blind) status line.
    catalogTotal = typeof total === "number" ? total : allRows.length;
    visibleCount = CHUNK;
    render();
  }

  function showMore() {
    visibleCount = Math.min(allRows.length, visibleCount + CHUNK);
    render();
  }

  function showAll() {
    visibleCount = allRows.length;
    render();
  }

  function setActiveId(id) {
    activeId = id ?? null;
    if (!tbody) return;
    for (const tr of tbody.querySelectorAll("tr[data-id]")) {
      const isActive = tr.dataset.id === activeId;
      if (isActive) tr.dataset.active = "true";
      else delete tr.dataset.active;
      const button = tr.querySelector(".cell-name button");
      if (!button) continue;
      if (isActive) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    }
  }

  setRows(rows);

  return {
    setRows,
    sortBy: applySort,
    showMore,
    showAll,
    setActiveId,
    toCSV,
  };
}

// ---- CSV export (pure) ----------------------------------------------------

const CSV_COLUMNS = [
  { key: "id", label: "ID" },
  { key: "name", label: "Name" },
  { key: "source", label: "Source" },
  { key: "domains", label: "Domains" },
  { key: "modalities", label: "Modalities" },
  { key: "conditions", label: "Conditions" },
  { key: "access", label: "Access" },
  { key: "year_start", label: "Year start" },
  { key: "year_end", label: "Year end" },
  { key: "sample_size", label: "Sample size" },
  { key: "sample_unit", label: "Sample unit" },
  { key: "countries", label: "Countries" },
  { key: "species", label: "Species" },
  { key: "url", label: "URL" },
];

const LIST_JOIN = "; ";

// CSV/formula injection guard: a cell opening a spreadsheet formula
// (=, +, -, @ — Excel/Sheets/LibreOffice all treat these as live
// formulas on open) gets a leading `'`, which every one of them already
// treats as "force this cell to plain text" and strips from the display.
// Checked after trimming (so " =SUM(...)" doesn't sneak past on leading
// whitespace) but applied to the original value, so the visible content
// is otherwise untouched.
const FORMULA_PREFIX_RE = /^[=+\-@]/;

function csvField(value) {
  const raw = value === null || value === undefined ? "" : String(value);
  const s = FORMULA_PREFIX_RE.test(raw.trim()) ? `'${raw}` : raw;
  return `"${s.replace(/"/g, '""')}"`;
}

function rowToCsvFields(row) {
  return {
    id: row.id ?? "",
    name: row.name ?? "",
    source: row.source ?? "",
    domains: (row.domains ?? []).join(LIST_JOIN),
    modalities: (row.modalities ?? []).join(LIST_JOIN),
    conditions: (row.conditions ?? []).join(LIST_JOIN),
    access: row.access ?? "",
    year_start: row.years?.start ?? "",
    year_end: row.years?.end ?? "",
    sample_size: row.sample_size ?? "",
    sample_unit: row.sample_unit ?? "",
    countries: (row.countries ?? []).join(LIST_JOIN),
    species: row.species ?? "",
    url: row.url ?? "",
  };
}

/**
 * Build a CSV document from `rows` (the filtered set): UTF-8 BOM (so
 * Excel picks the encoding up correctly), every field quoted (embedded
 * quotes doubled per RFC 4180), list-valued fields joined with `"; "`,
 * CRLF line endings. Pure — no DOM, no dependency on `createTable`.
 */
export function toCSV(rows) {
  // Written via fromCharCode rather than a literal in the source, so the
  // BOM can't silently be stripped/mangled by an editor or diff tool.
  const BOM = String.fromCharCode(0xfeff);
  const lines = [CSV_COLUMNS.map((c) => csvField(c.label)).join(",")];
  for (const row of Array.isArray(rows) ? rows : []) {
    const fields = rowToCsvFields(row);
    lines.push(CSV_COLUMNS.map((c) => csvField(fields[c.key])).join(","));
  }
  return `${BOM}${lines.join("\r\n")}\r\n`;
}
