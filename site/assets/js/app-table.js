/**
 * Entry point for table.html: load the search index, wire the filters
 * rail, the table, the record panel, CSV export, and URL-synced state.
 *
 * Loads `data/search-index.json` itself via a tiny local `fetchJSON`
 * (append `?v=${BUILD}`, per every other data fetch in the site) rather
 * than importing a shared `data.js` — Task 3.2 owns that module for the
 * graph page; the controller reconciles any duplication at merge time.
 */

import { BASE_URL, BUILD } from "./config.js";
import { announce, initA11y } from "./a11y.js";
import { initTheme } from "./theme.js";
import { fmtNumber } from "./format.js";
import { buildSearch, createSearchBox } from "./search.js";
import { createPanel } from "./panel.js";
import { applyFilters, facetCounts, renderFacetOptions, FACETS } from "./filters.js";
import { createTable, toCSV } from "./table.js";
import { readState, writeState, onChange } from "./url-state.js";

async function fetchJSON(path) {
  const url = `${BASE_URL}${path}?v=${BUILD}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load ${path}: ${res.status}`);
  return res.json();
}

// url-state's `sort` tokens <-> table.js's column names.
const SORT_COLUMN_TO_TOKEN = {
  name: "name",
  source: "source",
  years: "year",
  sample_size: "n",
  access: "access",
};
const TOKEN_TO_SORT_COLUMN = Object.fromEntries(
  Object.entries(SORT_COLUMN_TO_TOKEN).map(([column, token]) => [token, column]),
);

function parseSortToken(token) {
  if (!token) return { column: "name", dir: "asc" };
  const desc = token.startsWith("-");
  const base = desc ? token.slice(1) : token;
  const column = TOKEN_TO_SORT_COLUMN[base] ?? "name";
  return { column, dir: desc ? "desc" : "asc" };
}

function formatSortToken(column, dir) {
  const base = SORT_COLUMN_TO_TOKEN[column] ?? "name";
  return dir === "desc" ? `-${base}` : base;
}

/**
 * `facetCounts()` alone would drop a value's checkbox the moment it hits
 * zero under the current filters, making the list jump around as the
 * reader narrows their search. Union it with the *unfiltered* universe of
 * values so every option stays put, showing 0 rather than disappearing.
 */
function stableFacetCounts(rows, state, facet) {
  const universe = facetCounts(rows, {}, facet);
  const live = facetCounts(rows, state, facet);
  const merged = new Map();
  for (const value of universe.keys()) merged.set(value, live.get(value) ?? 0);
  return merged;
}

/** Focus the checkbox for `value` in `facet`'s rail, if it's on screen. */
function focusFacetCheckbox(facet, value) {
  const container = document.querySelector(`[data-facet-options="${facet}"]`);
  if (!container) return false;
  for (const input of container.querySelectorAll("input")) {
    if (input.value === value) {
      input.focus();
      return true;
    }
  }
  return false;
}

function main() {
  initTheme();
  initA11y();

  const statusEl = document.getElementById("table-status");
  const tbody = document.getElementById("table-body");
  const tableSearchInput = document.getElementById("table-search");
  const yearFromInput = document.getElementById("year-from");
  const yearToInput = document.getElementById("year-to");
  const clearFiltersBtn = document.getElementById("clear-filters");
  const downloadBtn = document.getElementById("download-csv");
  const panelEl = document.getElementById("panel");
  const searchInput = document.getElementById("search-input");

  let allRows = [];
  let rowsById = new Map();
  let filterState = {};
  let searchFn = () => [];

  function selectRow(id) {
    const row = rowsById.get(id);
    if (!row) return;
    table?.setActiveId(id);
    // Every call here is a deliberate pick (a row click or a search
    // selection), never a passive re-render, so it's always fine to move
    // focus to the panel - unlike panel.js's default "update in place"
    // behaviour for a panel that's already open on something else.
    panel.showDataset(id, { row, focus: true });
  }

  const panel = createPanel(panelEl, {
    mode: "table",
    onChip({ kind, value }) {
      if (!FACETS.includes(kind)) return;
      const current = new Set(filterState[kind] ?? []);
      current.add(value);
      filterState = { ...filterState, [kind]: [...current] };
      // Close before refresh(): refresh() rebuilds the table (the panel's
      // "opener" row button may no longer exist once the new filter is
      // applied), so closing first, while the opener is still attached,
      // is what lets panel.close()'s own focus-return actually land.
      panel.close();
      refresh();
      writeState({ [kind]: [...current] });
      // Either way, land somewhere concrete rather than <body>: the
      // checkbox that now reflects the filter just added is guaranteed
      // to exist (it's the value the chip itself came from) and is
      // exactly what the reader's attention should confirm next.
      focusFacetCheckbox(kind, value);
    },
    onNavigate(id) {
      // "View in graph" - the table page has no in-page graph, so this
      // is a real cross-page navigation.
      window.location.href = `index.html?node=${encodeURIComponent(id)}`;
    },
    onClose() {
      table?.setActiveId(null);
    },
  });

  // Created after the panel so its Escape handler is the more-recently
  // registered one: a single Escape closes the combobox's open results
  // first, and only a second Escape (once the combobox has nothing left
  // open) reaches the panel's handler. See a11y.js's `onEscape`.
  createSearchBox(searchInput, {
    getSearch: () => searchFn,
    onSelect(id) {
      selectRow(id);
    },
  });

  const table = createTable(tbody, statusEl, {
    rows: [],
    onRowSelect(id) {
      selectRow(id);
    },
    onSort(column, dir) {
      writeState({ sort: formatSortToken(column, dir) });
    },
  });

  function refresh() {
    const filtered = applyFilters(allRows, filterState);
    table.setRows(filtered, { total: allRows.length });
    renderAllFacets();
  }

  function renderAllFacets() {
    for (const facet of FACETS) {
      const container = document.querySelector(`[data-facet-options="${facet}"]`);
      if (!container) continue;
      const counts = stableFacetCounts(allRows, filterState, facet);
      renderFacetOptions(container, facet, counts, filterState[facet] ?? [], (value, checked) => {
        const current = new Set(filterState[facet] ?? []);
        if (checked) current.add(value);
        else current.delete(value);
        filterState = { ...filterState, [facet]: [...current] };
        refresh();
        writeState({ [facet]: [...current] });
      });
    }
  }

  function applyUrlState(state) {
    filterState = {
      domain: state.domain ?? [],
      modality: state.modality ?? [],
      condition: state.condition ?? [],
      access: state.access ?? [],
      source: state.source ?? [],
      country: state.country ?? [],
      species: state.species ?? [],
      years: state.years,
      q: state.q ?? "",
    };
    if (tableSearchInput) tableSearchInput.value = filterState.q;
    if (yearFromInput) yearFromInput.value = filterState.years?.start ?? "";
    if (yearToInput) yearToInput.value = filterState.years?.end ?? "";

    const { column, dir } = parseSortToken(state.sort);
    table.sortBy(column, dir);
    refresh();
  }

  let queryDebounce = null;
  tableSearchInput?.addEventListener("input", () => {
    if (queryDebounce !== null) clearTimeout(queryDebounce);
    queryDebounce = setTimeout(() => {
      queryDebounce = null;
      filterState = { ...filterState, q: tableSearchInput.value };
      refresh();
      writeState({ q: tableSearchInput.value });
    }, 150);
  });

  function onYearsInput() {
    const fromRaw = yearFromInput?.value.trim();
    const toRaw = yearToInput?.value.trim();
    const start = fromRaw ? Number(fromRaw) : undefined;
    const end = toRaw ? Number(toRaw) : undefined;
    const validStart = Number.isFinite(start) ? start : undefined;
    const validEnd = Number.isFinite(end) ? end : undefined;
    const years =
      validStart === undefined && validEnd === undefined ? undefined : { start: validStart, end: validEnd };

    filterState = { ...filterState, years };
    refresh();
    // url-state.js round-trips open-ended ranges ("2018-"/"-2020") as
    // well as complete ones, so whatever's in the inputs - even a single
    // filled bound - can go straight to the URL.
    writeState({ years });
  }
  yearFromInput?.addEventListener("input", onYearsInput);
  yearToInput?.addEventListener("input", onYearsInput);

  clearFiltersBtn?.addEventListener("click", () => {
    filterState = {};
    if (tableSearchInput) tableSearchInput.value = "";
    if (yearFromInput) yearFromInput.value = "";
    if (yearToInput) yearToInput.value = "";
    refresh();
    writeState({
      domain: [],
      modality: [],
      condition: [],
      access: [],
      source: [],
      country: [],
      species: [],
      years: undefined,
      q: "",
    });
    announce("Filters cleared.");
  });

  downloadBtn?.addEventListener("click", () => {
    const filtered = applyFilters(allRows, filterState);
    const csv = toCSV(filtered);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "clinical-data-atlas.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    announce(`Downloaded ${fmtNumber(filtered.length)} datasets as CSV.`);
  });

  onChange((state) => applyUrlState(state));

  fetchJSON("data/search-index.json")
    .then((rows) => {
      allRows = Array.isArray(rows) ? rows : [];
      rowsById = new Map(allRows.map((r) => [r.id, r]));
      searchFn = buildSearch(allRows);
      applyUrlState(readState());
    })
    .catch((err) => {
      if (statusEl) statusEl.textContent = "Couldn't load the catalog. Try reloading the page.";
      console.error(err);
    });
}

main();
