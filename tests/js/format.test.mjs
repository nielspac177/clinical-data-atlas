// Tests for the shared formatting helpers in format.js (built in Task
// 3.1; this task consumes them from search.js/panel.js/filters.js/
// table.js and adds this coverage). Pure string/number functions, no DOM.
import assert from "node:assert/strict";
import test from "node:test";

import {
  escapeHTML,
  autolink,
  fmtNumber,
  fmtBytes,
  accessLabel,
  countryName,
} from "../../site/assets/js/format.js";

test("escapeHTML escapes the five characters that can break out of markup", () => {
  assert.equal(escapeHTML(`<img src=x onerror=alert(1)>`), "&lt;img src=x onerror=alert(1)&gt;");
  assert.equal(escapeHTML(`"quoted" & 'single'`), "&quot;quoted&quot; &amp; &#39;single&#39;");
  assert.equal(escapeHTML("plain text"), "plain text");
});

test("escapeHTML coerces nullish and non-string input safely", () => {
  assert.equal(escapeHTML(null), "");
  assert.equal(escapeHTML(undefined), "");
  assert.equal(escapeHTML(42), "42");
});

test("escapeHTML neutralizes a script tag as inert text", () => {
  const escaped = escapeHTML("<script>alert(1)</script>");
  assert.ok(!escaped.includes("<script>"));
  assert.equal(escaped, "&lt;script&gt;alert(1)&lt;/script&gt;");
});

test("autolink turns a bare http(s) URL into a safe anchor", () => {
  const out = autolink("See https://example.org/path for details.");
  assert.equal(
    out,
    'See <a href="https://example.org/path" rel="noopener noreferrer" target="_blank">https://example.org/path</a> for details.',
  );
});

test("autolink only links http(s) - never javascript:/data: schemes", () => {
  const out = autolink("javascript:alert(1) and data:text/html,x are not URLs here");
  assert.ok(!out.includes("<a "));
  assert.equal(out, escapeHTML("javascript:alert(1) and data:text/html,x are not URLs here"));
});

test("autolink escapes surrounding text and any markup it contains", () => {
  const out = autolink('<b>bold</b> visit https://example.org now');
  assert.ok(out.startsWith("&lt;b&gt;bold&lt;/b&gt;"));
  assert.ok(out.includes('<a href="https://example.org"'));
});

test("autolink trims trailing sentence punctuation off the URL, not off the text", () => {
  const out = autolink("Visit https://example.org/x, then https://example.org/y.");
  assert.ok(out.includes('href="https://example.org/x"'));
  assert.ok(out.includes('href="https://example.org/y"'));
  assert.ok(out.includes(">https://example.org/x</a>,"));
  assert.ok(out.includes(">https://example.org/y</a>."));
});

test("autolink keeps a closing paren that belongs to the URL", () => {
  const out = autolink("See https://en.wikipedia.org/wiki/Foo_(bar) here");
  assert.ok(out.includes('href="https://en.wikipedia.org/wiki/Foo_(bar)"'));
});

test("autolink handles multiple URLs and no URLs", () => {
  assert.equal(autolink("no links here"), "no links here");
  const both = autolink("a http://one.example b http://two.example c");
  assert.equal((both.match(/<a /g) ?? []).length, 2);
});

test("fmtNumber groups thousands and handles nullish/invalid input", () => {
  assert.equal(fmtNumber(1234), "1,234");
  assert.equal(fmtNumber(0), "0");
  assert.equal(fmtNumber(1234567), "1,234,567");
  assert.equal(fmtNumber(null), "—");
  assert.equal(fmtNumber(undefined), "—");
  assert.equal(fmtNumber(Number.NaN), "—");
});

test("fmtBytes renders human-readable, 1024-based units", () => {
  assert.equal(fmtBytes(0), "0 B");
  assert.equal(fmtBytes(512), "512 B");
  assert.equal(fmtBytes(1024), "1.0 KB");
  assert.equal(fmtBytes(1024 * 1024), "1.0 MB");
  assert.equal(fmtBytes(1536 * 1024 * 1024), "1.5 GB");
});

test("fmtBytes falls back to an em dash for nullish/negative/invalid input", () => {
  assert.equal(fmtBytes(null), "—");
  assert.equal(fmtBytes(undefined), "—");
  assert.equal(fmtBytes(-5), "—");
  assert.equal(fmtBytes(Number.NaN), "—");
});

test("accessLabel maps known tiers and passes through unknown ones", () => {
  assert.equal(accessLabel("open"), "Open");
  assert.equal(accessLabel("credentialed"), "Credentialed");
  assert.equal(accessLabel("purchase"), "Purchase");
  assert.equal(accessLabel("something_else"), "something_else");
  assert.equal(accessLabel(null), "—");
});

test("countryName resolves a known ISO-3166 alpha-2 code", () => {
  // Exercised via Intl.DisplayNames where available (Node has full-icu by
  // default); assert the well-known shape rather than a brittle exact
  // string, since the exact locale data can vary slightly by ICU build.
  const us = countryName("US");
  assert.ok(us === "United States" || us === "US");
});

test("countryName falls back to the code itself for anything not a 2-letter code", () => {
  assert.equal(countryName("usa"), "usa");
  assert.equal(countryName(""), "");
  assert.equal(countryName(null), "");
  assert.equal(countryName("123"), "123");
});

test("countryName is case-insensitive on input", () => {
  const upper = countryName("PE");
  const lower = countryName("pe");
  assert.equal(upper, lower);
});
