/**
 * Accessibility plumbing shared by every page: the polite live region,
 * the `/` search shortcut, and a single Escape stack.
 */

const LIVE_ID = "a11y-live";
const ANNOUNCE_DELAY = 500;

let announceTimer = null;
let pending = "";

function liveRegion() {
  return document.getElementById(LIVE_ID);
}

/**
 * Announce `text` in the page's polite live region.
 *
 * Trailing-debounced by 500 ms: filtering a table or expanding a graph
 * node fires many small updates in a row, and a screen reader should hear
 * the final count once rather than every intermediate one.
 */
export function announce(text) {
  pending = String(text);
  if (announceTimer !== null) clearTimeout(announceTimer);
  announceTimer = setTimeout(() => {
    announceTimer = null;
    const region = liveRegion();
    if (!region) return;
    // Re-announce identical text by clearing first; assistive tech
    // ignores a write that doesn't change the node's content.
    region.textContent = "";
    region.textContent = pending;
  }, ANNOUNCE_DELAY);
}

/** Flush any pending announcement immediately (used by tests and teardown). */
export function flushAnnouncements() {
  if (announceTimer === null) return;
  clearTimeout(announceTimer);
  announceTimer = null;
  const region = liveRegion();
  if (region) region.textContent = pending;
}

function isTypingTarget(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/**
 * `/` focuses the page's search box — the convention readers already know
 * from GitHub and friends — unless they are already typing somewhere.
 */
export function initSearchShortcut(
  input = document.getElementById("search-input"),
) {
  if (!input) return;
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    if (isTypingTarget(event.target)) return;
    event.preventDefault();
    input.focus();
    input.select?.();
  });
}

const escapeHandlers = [];

/**
 * Register an Escape handler. Handlers run most-recently-registered
 * first and the first one to return `true` consumes the key, so a search
 * listbox closes before the record panel does. Returns an unregister
 * function.
 */
export function onEscape(handler) {
  escapeHandlers.push(handler);
  return () => {
    const i = escapeHandlers.indexOf(handler);
    if (i !== -1) escapeHandlers.splice(i, 1);
  };
}

export function initEscape() {
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    for (let i = escapeHandlers.length - 1; i >= 0; i -= 1) {
      if (escapeHandlers[i](event) === true) {
        event.preventDefault();
        return;
      }
    }
  });
}

/** True when the reader asked the OS to reduce motion. */
export function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Everything a page needs from this module, in one call. */
export function initA11y() {
  initSearchShortcut();
  initEscape();
}
