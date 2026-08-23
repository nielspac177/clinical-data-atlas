/**
 * Dataset search: a tokenised, dependency-free ranker (`buildSearch`) and
 * an ARIA combobox/listbox adapter (`createSearchBox`) built on top of it.
 *
 * `buildSearch`/the `query` function it returns are pure — no DOM, safe to
 * import and exercise directly under Node. `createSearchBox` is the thin
 * DOM adapter that wires an `<input role="combobox">` + a `role="listbox"`
 * result list to it; it is exercised by hand/e2e, not by `node --test`.
 */

import { announce, onEscape } from "./a11y.js";

function tokenize(text) {
  return String(text ?? "")
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);
}

// Score tiers, spaced far enough apart that the in-tier nudges below (an
// exact-name bonus, a small length penalty) can never cross a boundary —
// so "prefix beats substring beats other-fields" always holds regardless
// of the nudges, and the only remaining tie-break is the name itself.
const TIER_NAME_PREFIX = 3000;
const TIER_NAME_SUBSTRING = 2000;
const TIER_OTHER_FIELDS = 1000;
const EXACT_NAME_BONUS = 500;

function tokensAllPrefixed(nameTokens, needleTokens) {
  return needleTokens.every((qt) => nameTokens.some((nt) => nt.startsWith(qt)));
}

function tokensAllSubstring(haystack, needleTokens) {
  return needleTokens.every((qt) => haystack.includes(qt));
}

function scoreDoc(doc, needle, needleTokens) {
  let score = 0;

  const namePrefix =
    doc.nameTokens.some((t) => t.startsWith(needle)) ||
    tokensAllPrefixed(doc.nameTokens, needleTokens);
  const nameSubstring =
    !namePrefix &&
    (doc.nameLower.includes(needle) || tokensAllSubstring(doc.nameLower, needleTokens));

  if (namePrefix) {
    score = TIER_NAME_PREFIX;
  } else if (nameSubstring) {
    score = TIER_NAME_SUBSTRING;
  } else if (
    tokensAllSubstring(doc.otherLower, needleTokens) ||
    doc.otherLower.includes(needle)
  ) {
    score = TIER_OTHER_FIELDS;
  }

  if (score === 0) return 0;

  if (doc.nameLower === needle) score += EXACT_NAME_BONUS;
  // Tiny, same-tier-only nudge: a tighter (shorter) name match reads as
  // more relevant than a long name that merely contains the same hit.
  score -= Math.min(doc.name.length, 200) * 0.1;

  return score;
}

/**
 * Build a searcher over `rows` (`search-index.json` rows, or anything
 * shaped like `{id, name, summary?, source?, conditions?}`).
 *
 * Returns `query(q, limit=20) -> [{id, name, score}]`, tokenised and
 * case-insensitive: a prefix match on a name token scores highest, then a
 * substring match anywhere in the name, then a hit in conditions/source/
 * summary. Ties break on `score` desc then `name` asc, so ordering is
 * stable across calls. Each result also carries a `meta` string (the
 * dataset's source id) for display — additive, safe to ignore.
 */
export function buildSearch(rows) {
  const docs = (Array.isArray(rows) ? rows : []).map((row) => {
    const name = String(row?.name ?? "");
    const conditions = Array.isArray(row?.conditions) ? row.conditions : [];
    const other = [row?.summary, row?.source, conditions.join(" ")]
      .filter(Boolean)
      .join(" ");
    return {
      row,
      id: row?.id,
      name,
      nameLower: name.toLowerCase(),
      nameTokens: tokenize(name),
      otherLower: other.toLowerCase(),
      source: row?.source,
    };
  });

  return function query(q, limit = 20) {
    const needle = String(q ?? "").trim().toLowerCase();
    if (!needle) return [];
    const needleTokens = tokenize(needle);
    if (needleTokens.length === 0) return [];

    const hits = [];
    for (const doc of docs) {
      if (!doc.id) continue;
      const score = scoreDoc(doc, needle, needleTokens);
      if (score > 0) {
        hits.push({ id: doc.id, name: doc.name, score, meta: doc.source ?? "" });
      }
    }

    hits.sort((a, b) => b.score - a.score || a.name.localeCompare(b.name));
    return hits.slice(0, Math.max(0, limit));
  };
}

const DEBOUNCE_MS = 120;

/**
 * Wire an `<input role="combobox">` + its `role="listbox"` result list
 * (named by the input's `aria-controls`) into a live search box.
 *
 * `getSearch()` must return the current `query(q, limit)` function (so the
 * caller can swap it once the real index has loaded); `onSelect(id)` fires
 * on Enter or a result click. Handles arrow/Home/End navigation,
 * `aria-activedescendant`, a 120 ms debounce, and announces result counts
 * via `a11y.announce()`. The page-wide `/` shortcut is `a11y.js`'s
 * `initSearchShortcut()`, not duplicated here.
 */
export function createSearchBox(input, { getSearch, onSelect, limit = 20 } = {}) {
  const noop = { close() {}, destroy() {} };
  if (!input) return noop;
  const listId = input.getAttribute("aria-controls");
  const list = listId ? document.getElementById(listId) : null;
  if (!list) return noop;

  let items = [];
  let activeIndex = -1;
  let debounceTimer = null;

  const optionId = (index) => `${list.id}-option-${index}`;

  function close() {
    items = [];
    activeIndex = -1;
    list.textContent = "";
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function render() {
    list.textContent = "";

    if (items.length === 0) {
      // No results (whether there's no query, or a query that matched
      // nothing) collapses the listbox entirely rather than showing an
      // "expanded" popup with nothing selectable in it - the "no
      // matches" message is carried by the live-region announcement in
      // runSearch(), not by a presentation-only list item here.
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      return;
    }

    const frag = document.createDocumentFragment();
    items.forEach((item, index) => {
      const li = document.createElement("li");
      li.id = optionId(index);
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", index === activeIndex ? "true" : "false");

      const name = document.createElement("span");
      name.className = "result-name";
      name.textContent = item.name;

      const meta = document.createElement("span");
      meta.className = "result-meta";
      meta.textContent = item.meta ?? "";

      li.append(name, meta);
      li.addEventListener("mousedown", (event) => {
        // mousedown fires before the input's blur; prevent losing focus
        // (and the selection racing the close-on-blur handler below).
        event.preventDefault();
        select(index);
      });
      frag.appendChild(li);
    });
    list.appendChild(frag);
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    if (activeIndex >= 0) input.setAttribute("aria-activedescendant", optionId(activeIndex));
    else input.removeAttribute("aria-activedescendant");
  }

  function select(index) {
    const item = items[index];
    if (!item) return;
    close();
    input.value = "";
    onSelect?.(item.id);
  }

  function runSearch() {
    const q = input.value;
    const trimmed = q.trim();
    const search = getSearch?.();
    items = trimmed && typeof search === "function" ? search(trimmed, limit) : [];
    activeIndex = items.length ? 0 : -1;
    render();
    if (trimmed) {
      announce(
        items.length === 0
          ? `No results for "${trimmed}"`
          : `${items.length} result${items.length === 1 ? "" : "s"} for "${trimmed}"`,
      );
    }
  }

  input.addEventListener("input", () => {
    if (debounceTimer !== null) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      runSearch();
    }, DEBOUNCE_MS);
  });

  input.addEventListener("keydown", (event) => {
    if (items.length > 0 && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      const delta = event.key === "ArrowDown" ? 1 : -1;
      activeIndex = (activeIndex + delta + items.length) % items.length;
      render();
    } else if (items.length > 0 && event.key === "Home") {
      event.preventDefault();
      activeIndex = 0;
      render();
    } else if (items.length > 0 && event.key === "End") {
      event.preventDefault();
      activeIndex = items.length - 1;
      render();
    } else if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      select(activeIndex);
    }
  });

  input.addEventListener("blur", () => {
    // Deferred so a mousedown-triggered select() above still runs first.
    setTimeout(close, 0);
  });

  const unregisterEscape = onEscape(() => {
    if (document.activeElement !== input) return false;
    const hadSomethingOpen = !list.hidden || input.value !== "";
    close();
    input.value = "";
    input.blur();
    return hadSomethingOpen;
  });

  close();

  return {
    close,
    destroy() {
      close();
      unregisterEscape();
    },
  };
}
