/**
 * Entry point for the content pages (About, What's new).
 *
 * Those pages are server-rendered prose: all they need is the theme
 * toggle, the shared keyboard plumbing, and the "copy" buttons next to
 * the citation snippets.
 */

import { announce, initA11y } from "./a11y.js";
import { initTheme } from "./theme.js";

/**
 * Wire every `[data-copy]` button to the element its value selects.
 * The button's own label is restored after a moment so the control
 * doesn't silently change identity.
 */
function initCopyButtons() {
  for (const button of document.querySelectorAll("[data-copy]")) {
    const target = document.querySelector(button.getAttribute("data-copy"));
    if (!target) continue;

    button.addEventListener("click", async () => {
      const text = target.textContent ?? "";
      const label = button.textContent;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
        announce("Citation copied to the clipboard.");
      } catch {
        // Clipboard blocked (insecure context, denied permission): select
        // the text instead so the reader can copy it themselves.
        const range = document.createRange();
        range.selectNodeContents(target);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
        button.textContent = "Selected — press ⌘/Ctrl+C";
        announce("Citation selected. Press Command or Control C to copy.");
      }
      setTimeout(() => {
        button.textContent = label;
      }, 2500);
    });
  }
}

initTheme();
initA11y();
initCopyButtons();
