/**
 * The narrow-screen "Filters" disclosure for the table page's facets rail.
 *
 * With the real catalog `.facets` is ~1,800 px tall, and below the
 * layout's 900 px breakpoint `.table-main` stacks it *above* the table —
 * so on a 375 px phone the search box was 1,914 px down the page and the
 * first row 2,038 px down, past every one of the seven facet groups.
 * Below that breakpoint the rail is therefore collapsed by default (CSS:
 * `.facets` is `display: none` there unless it carries
 * `[data-expanded="true"]`) and this module inserts the `<button
 * aria-expanded aria-controls>` that opens it. Above the breakpoint the
 * button is `display: none` and the rail renders exactly as before.
 *
 * The button, not a separate badge, carries how many filters are active
 * ("Filters (2)") — collapsed, it is the only thing on screen that can
 * say so, and a reader who scrolled past it still has the status line
 * ("312 of 2,655 datasets") right above the table.
 *
 * Open/closed is deliberately *not* persisted across reloads: a rail
 * that remembered being open would re-create the original problem on the
 * next visit, and it is one tap to reopen. Crossing the breakpoint
 * resets it too — the rail is unconditionally open above 900 px.
 *
 * `activeFilterCount`/`filtersLabel` are pure (no DOM) and tested
 * directly under Node; `createFacetsToggle` is the DOM adapter.
 */

import { announce } from "./a11y.js";
import { FACETS } from "./filters.js";

/** The layout breakpoint below which the rail collapses (layout.css). */
export const NARROW_QUERY = "(max-width: 899px)";

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * How many filters are active in a `url-state.js`-shaped filter state.
 *
 * Every selected facet value counts once ("Filters (3)" for two domains
 * plus one access tier — the number of *choices* the reader made, which
 * is what they have to undo), and a years range counts as one however
 * many of its two bounds are filled.
 *
 * `q` is not counted: the text query lives in the toolbar above the
 * table, in a box the reader can see and clear without opening
 * anything, so folding it in here would attribute a visible filter to a
 * hidden panel.
 */
export function activeFilterCount(state) {
  const s = state ?? {};
  let count = 0;
  for (const facet of FACETS) {
    const selected = s[facet];
    if (Array.isArray(selected)) count += selected.length;
  }
  const years = s.years;
  if (years && (years.start != null || years.end != null)) count += 1;
  return count;
}

/** The disclosure's label for a filter state: `"Filters"` / `"Filters (2)"`. */
export function filtersLabel(state) {
  const count = activeFilterCount(state);
  return count > 0 ? `Filters (${count})` : "Filters";
}

function chevron(doc) {
  const svg = doc.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "facets-toggle-chevron");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const path = doc.createElementNS(SVG_NS, "path");
  path.setAttribute("d", "M4 6.25 L8 10.25 L12 6.25");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.7");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);
  return svg;
}

/**
 * Insert the disclosure button immediately before `facets` and wire it.
 *
 * Immediately before, so the DOM order the button implies *is* the
 * visual order the CSS produces — no `order` shuffling, and therefore
 * nothing for the tab sequence and the screen-reader sequence to
 * disagree about. Focus stays on the button across a toggle rather than
 * jumping into the panel: the panel starts right below it, so the next
 * Tab walks into the first facet either way, and a second Enter closes
 * it again without hunting for the control.
 *
 * `options.media` (a `MediaQueryList`) and `options.announce` exist for
 * tests; the defaults are `NARROW_QUERY` and `a11y.announce`.
 *
 * Returns `{ update(state), focus(), destroy() }` — `update` re-labels the
 * button from the current filter state and is safe to call on every
 * refresh; `focus` is the narrow-screen landing place for a flow whose
 * natural target is inside the collapsed rail, and reports whether it
 * actually took focus.
 */
export function createFacetsToggle(facets, options = {}) {
  if (!facets || !facets.parentNode) {
    return { update() {}, focus: () => false, destroy() {} };
  }

  const doc = facets.ownerDocument;
  const media = options.media ?? window.matchMedia(NARROW_QUERY);
  const say = options.announce ?? announce;

  // `aria-controls` needs a target id; the shell ships one, but don't
  // depend on markup this module can run without.
  if (!facets.id) facets.id = "facets";

  const button = doc.createElement("button");
  button.type = "button";
  button.className = "facets-toggle";
  button.setAttribute("aria-controls", facets.id);

  const label = doc.createElement("span");
  label.className = "facets-toggle-text";
  button.append(label, chevron(doc));
  facets.parentNode.insertBefore(button, facets);

  let state = {};
  let expanded = !media.matches;

  // `update()` runs on every refresh, including for a keystroke in the
  // toolbar's text box that cannot change the count — so each write is
  // guarded rather than repeated.
  function render() {
    const text = filtersLabel(state);
    if (label.textContent !== text) label.textContent = text;
    const open = expanded ? "true" : "false";
    if (button.getAttribute("aria-expanded") !== open) {
      button.setAttribute("aria-expanded", open);
    }
    if (facets.getAttribute("data-expanded") !== open) {
      facets.setAttribute("data-expanded", open);
    }
  }

  function onClick() {
    expanded = !expanded;
    render();
    say(expanded ? "Filters shown." : "Filters hidden.");
  }

  function onMediaChange() {
    expanded = !media.matches;
    render();
  }

  button.addEventListener("click", onClick);
  media.addEventListener("change", onMediaChange);
  render();

  return {
    update(next) {
      state = next ?? {};
      render();
    },
    /**
     * Move focus to the button, if it is on screen. False above the
     * breakpoint, where the button has no box — the caller's own target
     * is visible there and this fallback is not wanted.
     */
    focus() {
      if (!button.getClientRects().length) return false;
      button.focus();
      return true;
    },
    destroy() {
      media.removeEventListener("change", onMediaChange);
      button.remove();
      facets.removeAttribute("data-expanded");
    },
  };
}
