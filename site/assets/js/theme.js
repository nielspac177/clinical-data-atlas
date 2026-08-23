/**
 * Theme: dark by default, follows the OS unless the reader chose one.
 *
 * State lives in exactly two places — `localStorage['cda-theme']` (the
 * explicit choice, or absent for "follow the OS") and the
 * `data-theme` attribute on `<html>` (what CSS reads). The inline
 * pre-paint script in every page's `<head>` sets the attribute before
 * first paint; this module keeps it in sync afterwards and tells the rest
 * of the app when it changes, via a `themechange` CustomEvent on
 * `document` whose `detail` is `{ theme }`.
 */

import { DOMAINS, NODE_TYPES } from "./config.js";

const STORAGE_KEY = "cda-theme";
const root = document.documentElement;
const media = window.matchMedia("(prefers-color-scheme: light)");

/** The stored choice, or `null` when the reader hasn't chosen. */
function storedTheme() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "dark" || value === "light" ? value : null;
  } catch {
    return null; // Private mode / storage disabled: follow the OS.
  }
}

/** The theme actually in effect right now. */
export function currentTheme() {
  const explicit = root.getAttribute("data-theme");
  if (explicit === "dark" || explicit === "light") return explicit;
  return media.matches ? "light" : "dark";
}

function apply(theme, { persist }) {
  root.setAttribute("data-theme", theme);
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Nothing to do: the attribute still holds for this page view.
    }
  }
  document.dispatchEvent(
    new CustomEvent("themechange", { detail: { theme } }),
  );
}

/** Switch to `theme` ("dark" | "light") and remember the choice. */
export function setTheme(theme) {
  apply(theme === "light" ? "light" : "dark", { persist: true });
}

/** Flip between dark and light. */
export function toggleTheme() {
  setTheme(currentTheme() === "light" ? "dark" : "light");
}

/**
 * Read the resolved value of every themed graph color from the cascade,
 * so the 3D scene and the CSS never disagree about what "oncology" is.
 * Returns `{ domains: {id: css color}, nodes: {type: css color},
 * text, muted, canvas }`.
 */
export function readDomainColors() {
  const styles = getComputedStyle(root);
  const read = (name) => styles.getPropertyValue(name).trim();

  const domains = {};
  for (const id of DOMAINS) domains[id] = read(`--dom-${id}`);

  const nodes = {};
  for (const type of NODE_TYPES) nodes[type] = read(`--node-${type}`);

  return {
    domains,
    nodes,
    text: read("--text"),
    muted: read("--muted"),
    canvas: read("--canvas"),
    accent: read("--accent"),
  };
}

/**
 * Wire up the header's theme toggle and start following the OS.
 *
 * `aria-pressed` means "light theme is on", and the button keeps a single
 * stable accessible name ("Light theme") so screen readers announce a
 * state change rather than a renamed control.
 */
export function initTheme(button = document.getElementById("theme-toggle")) {
  const sync = () => {
    if (button) {
      button.setAttribute(
        "aria-pressed",
        currentTheme() === "light" ? "true" : "false",
      );
    }
  };

  if (button) {
    button.addEventListener("click", () => {
      toggleTheme();
      sync();
    });
  }

  // No explicit choice? Follow the OS as it changes. The CSS already
  // does this on its own; the event keeps canvas colors in step.
  media.addEventListener("change", () => {
    if (storedTheme() === null) {
      root.removeAttribute("data-theme");
      document.dispatchEvent(
        new CustomEvent("themechange", { detail: { theme: currentTheme() } }),
      );
      sync();
    }
  });

  sync();
  return { currentTheme, setTheme, toggleTheme };
}
