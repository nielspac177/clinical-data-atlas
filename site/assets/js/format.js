/**
 * Formatting helpers. Every function here takes untrusted catalog text
 * and returns something safe to drop into `innerHTML`.
 */

const HTML_ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

/** Escape the five characters that can break out of HTML text or an attribute. */
export function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
}

// Deliberately conservative: an absolute http(s) URL, stopping at
// whitespace or a character that can't appear unescaped in markup.
const URL_RE = /\bhttps?:\/\/[^\s<>"']+/gi;

// Sentence punctuation that follows a URL far more often than it ends one.
const TRAILING_RE = /[.,;:!?]+$/;

function trimUrl(url) {
  let out = url.replace(TRAILING_RE, "");
  // A closing paren belongs to the URL only if the URL opened one.
  while (out.endsWith(")") && !out.includes("(")) out = out.slice(0, -1);
  return out;
}

/**
 * Turn a plain-text field (access notes, population, ...) into escaped
 * HTML with its http(s) URLs turned into links.
 *
 * The input is treated as text throughout — any markup in it is escaped,
 * never rendered — so this is safe on harvested source strings.
 */
export function autolink(text) {
  const source = String(text ?? "");
  let out = "";
  let last = 0;

  for (const match of source.matchAll(URL_RE)) {
    const raw = match[0];
    const url = trimUrl(raw);
    const start = match.index;

    out += escapeHTML(source.slice(last, start));
    const safe = escapeHTML(url);
    out += `<a href="${safe}" rel="noopener noreferrer" target="_blank">${safe}</a>`;
    // Punctuation trimmed off the match is plain text again.
    out += escapeHTML(raw.slice(url.length));
    last = start + raw.length;
  }

  return out + escapeHTML(source.slice(last));
}

/** Group a number for reading: `1234` -> `1,234`. Nullish -> `"—"`. */
export function fmtNumber(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US");
}

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

/** Human-readable byte size (1024-based). Nullish -> `"—"`. */
export function fmtBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${Math.round(n)} B`;

  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = value < 10 ? 1 : 0;
  return `${value.toFixed(digits)} ${BYTE_UNITS[unit]}`;
}

const ACCESS_LABELS = {
  open: "Open",
  registration: "Registration",
  credentialed: "Credentialed",
  application: "Application",
  purchase: "Purchase",
};

/** Display label for an access tier id; unknown ids pass through. */
export function accessLabel(access) {
  return ACCESS_LABELS[access] ?? String(access ?? "—");
}

let regionNames = null;
try {
  regionNames = new Intl.DisplayNames(["en"], { type: "region" });
} catch {
  regionNames = null; // Ancient engine: fall back to the raw code.
}

/** `"PE"` -> `"Peru"`, falling back to the code itself. */
export function countryName(code) {
  const cc = String(code ?? "").toUpperCase();
  if (!/^[A-Z]{2}$/.test(cc)) return String(code ?? "");
  try {
    return regionNames?.of(cc) ?? cc;
  } catch {
    return cc;
  }
}
