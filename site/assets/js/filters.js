/**
 * Facet filtering shared by the table page and (for domain dimming) the
 * graph legend: a predicate (`applyFilters`), per-facet counts
 * (`facetCounts`), and the DOM rendering of a facet's checkbox list
 * (`renderFacetOptions`).
 *
 * `applyFilters`/`facetCounts` are pure row-array logic (no DOM) and are
 * exercised directly under Node; `renderFacetOptions` is the DOM adapter.
 * Also exports the small label helpers (`domainLabel`, `sourceLabel`,
 * `capitalize`, `facetValueLabel`) that `table.js` and `panel.js` reuse,
 * so display labels for a given id stay identical everywhere they appear.
 */

import { ACCESS, DOMAINS } from "./config.js";
import { accessLabel, countryName } from "./format.js";

/** The seven filterable facets, in the order the filters rail renders them. */
export const FACETS = Object.freeze([
  "domain",
  "modality",
  "condition",
  "access",
  "source",
  "country",
  "species",
]);

const FACET_ACCESSORS = {
  domain: (row) => row.domains ?? [],
  modality: (row) => row.modalities ?? [],
  condition: (row) => row.conditions ?? [],
  access: (row) => (row.access ? [row.access] : []),
  source: (row) => (row.source ? [row.source] : []),
  country: (row) => row.countries ?? [],
  species: (row) => (row.species ? [row.species] : []),
};

function matchesSet(rowValues, selected) {
  if (!selected || selected.length === 0) return true;
  const values = Array.isArray(rowValues) ? rowValues : [];
  return values.some((v) => selected.includes(v));
}

function matchesScalar(rowValue, selected) {
  if (!selected || selected.length === 0) return true;
  return selected.includes(rowValue);
}

/**
 * Interval-overlap test for a row's `{start,end}` collection years against
 * a filter's `{start,end}` range. Either bound, on either side, may be
 * missing (unbounded in that direction). A row with *no* known years at
 * all cannot be confirmed to overlap a given range, so it's excluded
 * while a year filter is active — but only then; with no filter, every
 * row passes.
 */
function yearsOverlap(rowYears, filterYears) {
  if (!filterYears) return true;
  const qStart = filterYears.start;
  const qEnd = filterYears.end;
  if (qStart == null && qEnd == null) return true;

  const rStart = rowYears?.start ?? null;
  const rEnd = rowYears?.end ?? null;
  if (rStart == null && rEnd == null) return false;

  const effRStart = rStart ?? -Infinity;
  const effREnd = rEnd ?? Infinity;
  const effQStart = qStart ?? -Infinity;
  const effQEnd = qEnd ?? Infinity;

  return effRStart <= effQEnd && effREnd >= effQStart;
}

function matchesText(row, q) {
  const needle = String(q ?? "").trim().toLowerCase();
  if (!needle) return true;
  const tokens = needle.split(/\s+/).filter(Boolean);
  const haystack = [row.id, row.name, row.summary, row.source, ...(row.conditions ?? [])]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return tokens.every((t) => haystack.includes(t));
}

function matchesRow(row, state) {
  if (!matchesSet(row.domains, state.domain)) return false;
  if (!matchesSet(row.modalities, state.modality)) return false;
  if (!matchesSet(row.conditions, state.condition)) return false;
  if (!matchesScalar(row.access, state.access)) return false;
  if (!matchesScalar(row.source, state.source)) return false;
  if (!matchesSet(row.countries, state.country)) return false;
  if (!matchesScalar(row.species, state.species)) return false;
  if (!yearsOverlap(row.years, state.years)) return false;
  if (!matchesText(row, state.q)) return false;
  return true;
}

/**
 * Filter `rows` by `state` (whatever shape `url-state.js`'s `readState()`
 * produces). Semantics: OR between values *within* one facet (e.g.
 * `domain: ["neurology","oncology"]` keeps rows in either), AND *across*
 * facets, plus a `years` overlap test and a `q` substring/token match
 * across name/summary/source/conditions/id.
 */
export function applyFilters(rows, state = {}) {
  const s = state ?? {};
  return (Array.isArray(rows) ? rows : []).filter((row) => matchesRow(row, s));
}

/**
 * Count how many rows would match each value of `facet` if it were
 * selected, with every *other* facet/years/text filter in `state` still
 * applied (the standard faceted-search "counts assume your other
 * selections" behaviour) — `facet`'s own current selection does not
 * constrain this computation, so an unselected option's count reflects
 * what picking it would do, not zero.
 */
export function facetCounts(rows, state, facet) {
  const counts = new Map();
  const getValues = FACET_ACCESSORS[facet];
  if (!getValues) return counts;

  const stateWithoutThisFacet = { ...(state ?? {}), [facet]: undefined };
  const filtered = applyFilters(rows, stateWithoutThisFacet);

  for (const row of filtered) {
    for (const value of getValues(row)) {
      counts.set(value, (counts.get(value) ?? 0) + 1);
    }
  }
  return counts;
}

// ---- display labels ------------------------------------------------------

const DOMAIN_LABELS = {
  neurology: "Neurology",
  psychiatry: "Psychiatry",
  neuroscience: "Neuroscience",
  cardiology: "Cardiology",
  oncology: "Oncology",
  pulmonology: "Pulmonology",
  critical_care: "Critical care",
  surgery: "Surgery",
  pediatrics: "Pediatrics",
  obstetrics_gynecology: "Obstetrics & gynecology",
  infectious_disease: "Infectious disease",
  endocrinology_metabolism: "Endocrinology & metabolism",
  gastroenterology_hepatology: "Gastroenterology & hepatology",
  nephrology_urology: "Nephrology & urology",
  musculoskeletal: "Musculoskeletal",
  public_health: "Public health",
  other: "Other",
};

const SOURCE_LABELS = {
  openneuro: "OpenNeuro",
  physionet: "PhysioNet",
  gdc: "GDC",
  tcia: "TCIA",
  scientific_data: "Scientific Data",
  data_in_brief: "Data in Brief",
  dhs: "DHS",
  worldbank: "World Bank",
  curated: "Curated",
};

/** `"abc_def"` -> `"Abc def"` — a plain fallback for anything unmapped. */
export function capitalize(value) {
  const str = String(value ?? "");
  return str ? str.charAt(0).toUpperCase() + str.slice(1) : str;
}

// Sentence case ("Critical care", not "Critical Care") to match the vocab
// entries in DOMAIN_LABELS/SOURCE_LABELS above, for anything unmapped.
function humanize(id) {
  return capitalize(
    String(id ?? "")
      .split("_")
      .filter(Boolean)
      .join(" ")
      .toLowerCase(),
  );
}

/** Display label for a domain id, e.g. `"critical_care"` -> `"Critical care"`. */
export function domainLabel(id) {
  return DOMAIN_LABELS[id] ?? humanize(id);
}

/** Display label for a source id, e.g. `"physionet"` -> `"PhysioNet"`. */
export function sourceLabel(id) {
  return SOURCE_LABELS[id] ?? humanize(id);
}

/** Display label for any facet value, dispatching by facet name. */
export function facetValueLabel(facet, value) {
  switch (facet) {
    case "domain":
      return domainLabel(value);
    case "source":
      return sourceLabel(value);
    case "access":
      return accessLabel(value);
    case "country":
      return countryName(value);
    case "species":
    case "condition":
      return capitalize(value);
    default:
      return String(value ?? "");
  }
}

// Facets with a fixed, meaningful vocabulary order (from config.js) render
// in that order rather than by count, matching the graph legend/badges.
const VOCAB_ORDER = { domain: DOMAINS, access: ACCESS };

/**
 * Order `facet`'s entries for display: a fixed vocab order where one
 * exists (domain, access), else alphabetical by display label.
 *
 * Deliberately *not* sorted by count: counts shift on every filter
 * change (that's the whole point of `facetCounts`), and a facet whose
 * option order also shifted with them would visibly reshuffle under the
 * reader on every click — disorienting on its own, and it also defeats
 * `renderFacetOptions`'s in-place update (which relies on the value set
 * *and order* being stable so it never has to recreate, and thereby
 * un-focus, a checkbox the reader just activated). Pure and exported for
 * direct testing.
 */
export function sortFacetEntries(facet, counts) {
  const entries = [...counts.entries()];
  const order = VOCAB_ORDER[facet];
  if (order) {
    const rank = new Map(order.map((v, i) => [v, i]));
    entries.sort((a, b) => (rank.get(a[0]) ?? order.length) - (rank.get(b[0]) ?? order.length));
  } else {
    entries.sort(
      (a, b) =>
        facetValueLabel(facet, a[0]).localeCompare(facetValueLabel(facet, b[0])) ||
        String(a[0]).localeCompare(String(b[0])),
    );
  }
  return entries;
}

function buildOptionLabel(facet, value, count, checked, onToggle) {
  const label = document.createElement("label");

  const input = document.createElement("input");
  input.type = "checkbox";
  input.value = value;
  input.checked = checked;
  input.addEventListener("change", () => onToggle?.(value, input.checked));

  const text = document.createElement("span");
  text.className = "facet-option-label";
  text.textContent = facetValueLabel(facet, value);

  const countEl = document.createElement("span");
  countEl.className = "facet-count";
  countEl.textContent = String(count ?? 0);

  label.append(input, text, countEl);
  return label;
}

/**
 * Render `facet`'s checkbox list into `container` (a
 * `div[data-facet-options]` from table.html) from a `facetCounts()` map,
 * checking whichever values are in `selected`. `onToggle(value, checked)`
 * fires on every change. Native checkboxes, matching the shell's own
 * `.facet-options label > input + .facet-option-label + .facet-count`
 * markup (see `components.css`).
 *
 * Never destroys a checkbox the reader just activated: when the value
 * set/order already on screen matches what's about to be rendered (the
 * normal case — see `sortFacetEntries`), existing `<input>`s are updated
 * in place (checked state, count text) rather than torn down and
 * recreated, so a checkbox mid-`change`-event keeps focus. Only an
 * actual change in the value set rebuilds the list from scratch, and
 * even then the previously-focused value (if it's still present) is
 * refocused afterwards.
 */
export function renderFacetOptions(container, facet, counts, selected, onToggle) {
  if (!container) return;

  const selectedSet = new Set(selected ?? []);
  const entries = sortFacetEntries(facet, counts ?? new Map());

  const existingLabels = [...container.children];
  const existingValues = existingLabels.map((label) => label.querySelector("input")?.value);
  const desiredValues = entries.map(([value]) => value);
  const sameShape =
    existingLabels.length === desiredValues.length &&
    existingValues.every((v, i) => v === desiredValues[i]);

  if (sameShape) {
    entries.forEach(([value, count], i) => {
      const label = existingLabels[i];
      const input = label.querySelector("input");
      const checked = selectedSet.has(value);
      if (input && input.checked !== checked) input.checked = checked;
      const countEl = label.querySelector(".facet-count");
      if (countEl) {
        const countText = String(count ?? 0);
        if (countEl.textContent !== countText) countEl.textContent = countText;
      }
    });
    return;
  }

  // The value set genuinely changed: rebuild, but re-focus whichever
  // value was focused before, if it still exists, rather than dropping
  // focus to <body>.
  const active = document.activeElement;
  const activeValue = active && container.contains(active) ? active.value : null;

  container.textContent = "";
  const frag = document.createDocumentFragment();
  let toFocus = null;
  for (const [value, count] of entries) {
    const label = buildOptionLabel(facet, value, count, selectedSet.has(value), onToggle);
    if (value === activeValue) toFocus = label.querySelector("input");
    frag.appendChild(label);
  }
  container.appendChild(frag);
  toFocus?.focus();
}
