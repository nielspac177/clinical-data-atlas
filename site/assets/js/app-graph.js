/**
 * Entry point for the graph page.
 *
 * Loads the graph and the stats, builds the index, opens the scene on the
 * backbone, and wires the chrome around it: the stats bar, the domain
 * legend, the reset button, the record panel, the search box and the URL.
 *
 * `search.js`, `panel.js` and `url-state.js` are shared with the table
 * page and are loaded *optionally* — the graph, the stats, the legend and
 * a small built-in panel work whether or not they are present, so a
 * missing module degrades the page instead of blanking it.
 */

import { announce, initA11y, onEscape } from "./a11y.js";
import { currentTheme, initTheme, readDomainColors } from "./theme.js";
import { BUILD, REPO_URL } from "./config.js";
import { loadGraph, loadStats, loadIndex, loadRecord } from "./data.js";
import { buildIndex } from "./graph-index.js";
import { createGraph } from "./graph.js";
import { accessLabel, escapeHTML, fmtNumber } from "./format.js";

const stage = document.getElementById("graph");
const aside = document.getElementById("panel");
const statsBar = document.getElementById("stats-bar");
const resetButton = document.getElementById("reset-view");
const searchInput = document.getElementById("search-input");
const chips = [
  ...document.querySelectorAll("#domain-filters button.chip[data-domain]"),
];

initTheme();
initA11y();

// ---------------------------------------------------------------- helpers

/**
 * Import a sibling module, or `null` if it isn't deployed.
 *
 * The three optional modules are owned by the table page and land in the
 * same directory; until they do, the graph page has to come up without
 * them rather than die on a failed import.
 */
async function optionalModule(name) {
  const url = new URL(`./${name}?v=${encodeURIComponent(BUILD)}`, import.meta.url);
  try {
    return await import(url.href);
  } catch (error) {
    // Expected while the module is unbuilt, but a syntax error in a
    // deployed one looks identical from here — so say which and why.
    console.warn(`Optional module ${name} did not load; using the fallback.`, error);
    return null;
  }
}

function setStat(name, value) {
  const dd = statsBar?.querySelector(`dd[data-stat="${name}"]`);
  if (dd) dd.textContent = value;
}

function stageMessage(html) {
  if (!stage) return;
  stage.innerHTML = `<div class="graph-empty">${html}</div>`;
}

/**
 * A harvested URL is only ever followed if it is http(s).
 *
 * Escaping keeps a hostile string inside the attribute; it does not stop
 * `javascript:` from running when the link is clicked.
 */
function safeHref(url) {
  try {
    const parsed = new URL(String(url), window.location.href);
    return parsed.protocol === "https:" || parsed.protocol === "http:"
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------- URL state (thin)

/**
 * The subset of `url-state.js` this page needs, for when that module
 * isn't there. Same shapes, so swapping in the real one changes nothing.
 */
function builtinUrlState() {
  const list = (params, key) =>
    (params.get(key) ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);

  return {
    readState() {
      const params = new URLSearchParams(window.location.search);
      return {
        node: params.get("node") ?? null,
        q: params.get("q") ?? "",
        domain: list(params, "domain"),
      };
    },
    writeState(partial, { push = false } = {}) {
      const params = new URLSearchParams(window.location.search);
      for (const [key, value] of Object.entries(partial)) {
        const empty =
          value === null ||
          value === undefined ||
          value === "" ||
          (Array.isArray(value) && value.length === 0);
        if (empty) params.delete(key);
        else params.set(key, Array.isArray(value) ? value.join(",") : value);
      }
      const query = params.toString();
      const url = `${window.location.pathname}${query ? `?${query}` : ""}`;
      if (push) window.history.pushState({}, "", url);
      else window.history.replaceState({}, "", url);
    },
    onChange(callback) {
      window.addEventListener("popstate", () => callback(this.readState()));
    },
  };
}

// ----------------------------------------------------------- panel (thin)

/**
 * A minimal stand-in for `panel.js`: name, source, access and a link out
 * for a dataset; the expansion controls for a hub. It shares the panel
 * markup and the focus behaviour, so the page stays usable — it just
 * shows less than the real panel does.
 */
function builtinPanel(root) {
  if (!root) return { showDataset() {}, showEntity() {}, close() {} };

  const title = root.querySelector("#panel-title");
  const body = root.querySelector("#panel-body");
  const closeButton = root.querySelector("[data-panel-close]");
  let returnFocus = null;

  title?.setAttribute("tabindex", "-1");

  function open(heading) {
    // Only a hidden -> visible transition moves focus. "Show more"
    // re-renders the same open panel, and yanking focus back to the title
    // on every click would strand the reader mid-list.
    const opening = root.hidden;
    if (opening) returnFocus = document.activeElement;
    if (title) title.textContent = heading;
    root.hidden = false;
    if (opening) title?.focus();
  }

  function closePanel() {
    if (root.hidden) return;
    root.hidden = true;
    body.innerHTML = "";
    const target = returnFocus;
    returnFocus = null;
    if (target && document.contains(target)) target.focus();
  }

  closeButton?.addEventListener("click", closePanel);
  onEscape(() => {
    if (root.hidden) return false;
    closePanel();
    return true;
  });

  function badges(node, record) {
    const source = node?.source ?? record?.source;
    const access = node?.access ?? record?.access;
    const out = [];
    if (source) out.push(`<span class="badge">${escapeHTML(source)}</span>`);
    if (access) {
      out.push(
        `<span class="badge" data-access="${escapeHTML(access)}">` +
          `${escapeHTML(accessLabel(access))}</span>`,
      );
    }
    return out.join(" ");
  }

  let showing = null;

  function render(id, node, record) {
    const name = record?.name ?? node?.label ?? id;
    open(name);
    const url = safeHref(record?.url);
    body.innerHTML = [
      `<div class="panel-section"><p>${badges(node, record)}</p></div>`,
      record?.summary
        ? `<div class="panel-section"><p>${escapeHTML(record.summary)}</p></div>`
        : "",
      url
        ? `<div class="panel-section"><p><a class="btn btn-primary" href="${escapeHTML(url)}"` +
          ` rel="noopener" target="_blank">Open dataset &#8599;</a></p></div>`
        : "",
      `<p class="panel-fineprint">${escapeHTML(id)}</p>`,
    ].join("");
  }

  return {
    showDataset(id, { node, row } = {}) {
      showing = id;
      render(id, node, row);
      if (row) return;
      // No search index loaded yet: the record file has the same fields.
      loadRecord(id)
        .then((record) => {
          // The reader may have moved on while that was in flight.
          if (!root.hidden && showing === id) render(id, node, record);
        })
        .catch(() => {
          /* The node's own fields are already on screen. */
        });
    },

    showEntity(node, { shown = 0, total = 0, onMore, onCollapse } = {}) {
      showing = node.id;
      open(node.label ?? node.id);
      body.innerHTML = `<div class="panel-section"><p>${escapeHTML(
        total === 0
          ? "No datasets link to this node."
          : `Showing ${fmtNumber(shown)} of ${fmtNumber(total)} datasets.`,
      )}</p><p class="panel-actions"></p></div>`;

      const actions = body.querySelector(".panel-actions");
      if (onMore && shown < total) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "btn";
        more.textContent = "Show more";
        more.addEventListener("click", onMore);
        actions.append(more);
      }
      if (onCollapse) {
        const collapse = document.createElement("button");
        collapse.type = "button";
        collapse.className = "btn btn-quiet";
        collapse.textContent = "Collapse";
        collapse.addEventListener("click", onCollapse);
        actions.append(collapse);
      }
    },

    close() {
      showing = null;
      closePanel();
    },
  };
}

// -------------------------------------------------------------------- main

async function main() {
  if (!stage) return;

  const [modules, graphData, stats] = await Promise.all([
    Promise.all([
      optionalModule("search.js"),
      optionalModule("panel.js"),
      optionalModule("url-state.js"),
    ]),
    loadGraph(),
    loadStats().catch(() => null),
  ]);
  const [searchModule, panelModule, urlModule] = modules;

  const index = buildIndex(graphData);
  const urlState =
    urlModule && typeof urlModule.readState === "function"
      ? urlModule
      : builtinUrlState();

  // Chip values are labels ("Epilepsy"); the graph speaks ids.
  const hubByLabel = new Map();
  for (const [id, node] of index.nodes) {
    if (node.type === "dataset") continue;
    hubByLabel.set(`${node.type}\u0000${String(node.label).toLowerCase()}`, id);
  }

  let rowsById = null; // search-index rows, once something needs them
  const rowFor = (id) => rowsById?.get(id);

  const panel = panelModule?.createPanel
    ? panelModule.createPanel(aside, {
        mode: "graph",
        onChip: handleChip,
        onNavigate: (id) => selectDataset(id),
      })
    : builtinPanel(aside);

  const graph = createGraph(stage, {
    index,
    colors: readDomainColors(),
    onSelect: handleSelect,
    onExpand: handleExpand,
    onCounts: ({ nodes }) => setStat("visible", fmtNumber(nodes)),
  });
  graph.showBackbone();

  // ---------------------------------------------------------- stats bar

  if (stats) {
    setStat("record_count", fmtNumber(stats.record_count));
    setStat("sources", fmtNumber(Object.keys(stats.per_source ?? {}).length));
    setStat(
      "modalities",
      fmtNumber(Object.keys(stats.per_modality ?? {}).length),
    );
  }

  // ------------------------------------------------------------- legend

  function activeDomains() {
    return chips
      .filter((chip) => chip.getAttribute("aria-pressed") !== "false")
      .map((chip) => chip.dataset.domain);
  }

  function applyLegend({ silent = false } = {}) {
    let active = activeDomains();

    // Turning the last chip off would dim every dataset at once, with no
    // affordance saying why. Treat it as "show everything" instead of
    // parking the map in a state nobody can read.
    if (active.length === 0) {
      for (const chip of chips) chip.setAttribute("aria-pressed", "true");
      active = activeDomains();
      graph.setDomainFilter(null);
      urlState.writeState({ domain: [] });
      announce("All domains shown.");
      return;
    }

    const all = active.length === chips.length;
    graph.setDomainFilter(all ? null : new Set(active));
    urlState.writeState({ domain: all ? [] : active });
    if (silent) return;
    announce(
      all
        ? "All domains shown."
        : `Showing ${fmtNumber(active.length)} of ${fmtNumber(chips.length)} domains.`,
    );
  }

  function setLegend(domains, options) {
    for (const chip of chips) {
      const on = !domains || domains.size === 0 || domains.has(chip.dataset.domain);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    }
    applyLegend(options);
  }

  for (const chip of chips) {
    chip.addEventListener("click", () => {
      const pressed = chip.getAttribute("aria-pressed") !== "false";
      chip.setAttribute("aria-pressed", pressed ? "false" : "true");
      applyLegend();
    });
  }

  // ------------------------------------------------------------ selection

  function handleChip({ kind, value } = {}) {
    if (kind === "domain") {
      setLegend(new Set([value]));
      return;
    }
    const id = hubByLabel.get(`${kind}\u0000${String(value).toLowerCase()}`);
    if (id) {
      graph.focus(id);
      return;
    }
    // Not a node on this page (a country, a species): the table owns it.
    window.location.href =
      `table.html?${encodeURIComponent(kind)}=${encodeURIComponent(value)}`;
  }

  function selectDataset(id, { isolate = false, push = true } = {}) {
    const node = index.nodes.get(id);
    if (node) graph.focus(id, { isolate });
    panel.showDataset(id, { node, row: rowFor(id) });
    urlState.writeState({ node: id }, { push });
    announce(`Selected ${node?.label ?? id}.`);
  }

  function handleSelect(node) {
    if (!node) {
      panel.close();
      urlState.writeState({ node: null });
      return;
    }
    if (node.type === "dataset") {
      panel.showDataset(node.id, { node, row: rowFor(node.id) });
      announce(`Selected ${node.label ?? node.id}.`);
    }
    // A hub's panel is opened by handleExpand, which knows the counts.
    urlState.writeState({ node: node.id }, { push: true });
  }

  function handleExpand(hub, shown, total) {
    panel.showEntity(hub, {
      shown,
      total,
      onMore: shown < total ? () => graph.expand(hub.id, { more: true }) : null,
      onCollapse: () => {
        // graph.collapse() drops the selection with the datasets it
        // removes; leaving `?node=` behind would reopen this on reload.
        graph.collapse(hub.id);
        panel.close();
        urlState.writeState({ node: null });
        announce(`Collapsed ${hub.label ?? hub.id}.`);
      },
    });
    announce(
      total === 0
        ? `${hub.label ?? hub.id} has no datasets.`
        : `Expanded ${hub.label ?? hub.id}: ${fmtNumber(shown)} of ${fmtNumber(total)} datasets added.`,
    );
  }

  // ------------------------------------------------------------ controls

  resetButton?.addEventListener("click", () => {
    graph.reset();
    panel.close();
    setLegend(null, { silent: true });
    if (searchInput) searchInput.value = "";
    urlState.writeState({ node: null, q: null, domain: [] });
    announce("View reset to the backbone.");
  });

  // -------------------------------------------------------------- search

  let searchBox = null;
  let searchReady = null;

  function ensureSearch() {
    if (!searchModule?.buildSearch || searchReady) return searchReady;
    searchReady = loadIndex()
      .then((loaded) => {
        rowsById = new Map(loaded.map((row) => [row.id, row]));
        searchBox = searchModule.buildSearch(loaded);
        return searchBox;
      })
      .catch((error) => {
        searchReady = null;
        announce("Search is unavailable right now.");
        throw error;
      });
    return searchReady;
  }

  if (searchModule?.createSearchBox && searchInput) {
    searchModule.createSearchBox(searchInput, {
      getSearch: () => searchBox,
      // Called only when a typed query finds no index yet — so it still
      // starts the fetch lazily, and the box re-runs that query once the
      // index lands instead of leaving it on a "no results" it never got.
      ready: () => ensureSearch(),
      onSelect: (id) => selectDataset(id, { isolate: true }),
    });
    // The index is a second download: don't pay for it until it's wanted.
    searchInput.addEventListener("focus", () => {
      ensureSearch()?.catch(() => {});
    });
  }

  // ----------------------------------------------------------- URL state

  function applyState(state) {
    if (state.domain?.length) setLegend(new Set(state.domain), { silent: true });
    if (state.q && searchInput) {
      searchInput.value = state.q;
      ensureSearch()
        ?.then(() => {
          // Let the search box react to a query it didn't type itself.
          searchInput.dispatchEvent(new Event("input", { bubbles: true }));
        })
        .catch(() => {});
    }
    if (state.node) {
      const node = index.nodes.get(state.node);
      if (node) {
        graph.focus(state.node, { isolate: node.type === "dataset" });
        if (node.type === "dataset") {
          panel.showDataset(state.node, { node, row: rowFor(state.node) });
          announce(`Selected ${node.label ?? state.node}.`);
        }
      } else {
        // A stale or hand-edited link: say so rather than sit there
        // looking like the page failed to load.
        console.warn(`No node in the graph for id ${state.node}.`);
        announce(`No dataset with id ${state.node}.`);
      }
    } else {
      // No node in the URL means no selection: going back to a clean
      // entry has to leave a clean scene, not a dimmed one.
      graph.clearSelection();
      panel.close();
    }
  }

  applyState(urlState.readState());
  urlState.onChange?.((state) => applyState(state));

  // ------------------------------------------------------------- theming

  document.addEventListener("themechange", (event) => {
    graph.setColors(readDomainColors());
    const theme = event.detail?.theme ?? currentTheme();
    announce(theme === "light" ? "Light theme" : "Dark theme");
  });
}

main().catch((error) => {
  console.error(error);
  stageMessage(
    `<p>The graph could not be loaded. Everything it shows is also in the ` +
      `<a href="table.html">Table view</a>, and the catalog is downloadable ` +
      `from <a href="${escapeHTML(REPO_URL)}" rel="noopener">the repository</a>.</p>`,
  );
  announce("The graph could not be loaded. Try the table view instead.");
});
