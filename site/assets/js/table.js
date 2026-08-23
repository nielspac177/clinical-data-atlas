/**
 * The dataset table: `createTable` is the DOM adapter (renders into
 * `tbody`, wires the sortable headers it finds on its own `<table>` and
 * the well-known `#show-more`/`#show-all` buttons); `toCSV` is a pure,
 * top-level export (no DOM) so it's directly testable under Node.
 */

import { ACCESS } from "./config.js";
import { accessLabel, fmtNumber } from "./format.js";
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

function nameCell(row, onRowSelect) {
  const td = document.createElement("td");
  td.className = "cell-name";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-quiet";
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
  const shown = list.slice(0, limit);
  for (const value of shown) {
    const chip = document.createElement("span");
    chip.className = "chip";
    if (colored) chip.dataset.domain = value;
    chip.textContent = labelFn(value);
    chip.style.marginInlineEnd = "4px";
    chip.style.marginBlockEnd = "2px";
    td.appendChild(chip);
  }
  const extra = list.length - shown.length;
  if (extra > 0) {
    const more = document.createElement("span");
    more.className = "muted text-sm";
    more.textContent = `+${extra}`;
    td.appendChild(more);
  }
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
  if (active) tr.setAttribute("aria-selected", "true");

  tr.append(
    nameCell(row, onRowSelect),
    textCell(sourceLabel(row.source)),
    listCell(row.domains, DOMAINS_SHOWN, domainLabel, true),
    listCell(row.modalities, MODALITIES_SHOWN, (m) => m, false),
    accessCell(row.access),
    textCell(yearsText(row.years)),
    sampleCell(row.sample_size, row.sample_unit),
    textCell((row.countries ?? []).join(", ")),
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
  let sortColumn = "name";
  let sortDir = "asc";
  let visibleCount = CHUNK;
  let activeId = null;

  const headerEls = new Map();
  const tableEl = tbody?.closest?.("table") ?? null;
  if (tableEl) {
    // Workaround for a real layout bug in the inherited shell, not this
    // module's own CSS to fix: `.table-wrap` sets `overflow-x: auto`,
    // and a box is a *scroll container* - the sticky positioning context
    // for its descendants - as soon as either axis is non-`visible`,
    // regardless of the other axis's value (confirmed: forcing
    // `overflow-y` to `clip`/`hidden`/`visible` doesn't change this,
    // since `overflow-x: auto` alone already qualifies). So
    // `thead th { position: sticky; top: var(--header-h) }` sticks
    // `--header-h` (56px) below *`.table-wrap`'s* top edge, not the
    // page's - and since the wrapper's top sits only ~1px above the
    // header's natural position, that 56px minimum-gap rule pushes the
    // header down into the first data row (confirmed via
    // `elementFromPoint`: the header's button, not the row, receives the
    // click there) on every load, with no scrolling required to trigger
    // it. `.table-wrap` has no height/max-height, so it never actually
    // scrolls internally - "sticky" is moot for it regardless of `top` -
    // making `top: 0` a safe, purely corrective override: it removes the
    // artificial push without changing any real scroll behaviour.
    for (const th of tableEl.querySelectorAll("thead th")) {
      th.style.top = "0";
    }

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
    const shown = Math.min(visibleCount, allRows.length);
    const total = allRows.length;
    const text =
      shown >= total
        ? `${fmtNumber(total)} dataset${total === 1 ? "" : "s"}`
        : `${fmtNumber(shown)} of ${fmtNumber(total)} datasets`;
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

  function setRows(newRows) {
    allRows = Array.isArray(newRows) ? newRows : [];
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
      if (tr.dataset.id === activeId) tr.setAttribute("aria-selected", "true");
      else tr.removeAttribute("aria-selected");
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
    getSort: () => ({ column: sortColumn, dir: sortDir }),
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

function csvField(value) {
  const s = value === null || value === undefined ? "" : String(value);
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
