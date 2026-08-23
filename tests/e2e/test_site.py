"""The site, in a real browser.

Nothing here hard-codes a dataset name or a row count. Every expectation
is read from the JSON the server is actually handing out (the `catalog`
fixture), so the same suite is meaningful against the six-row fixture
catalog it builds by default *and* against the real one CI points it at
with `--base-url`. When a test needs a specific dataset it takes the
first row of `search-index.json`; when it needs a specific node it takes
one that is on screen.

`tests/e2e/conftest.py` fails any test whose page logged a
`console.error` or threw — so "the page still works" is asserted by every
test in the file, not only the ones that say so.

House rules for anything added here: one behaviour per test, no
dependence on another test having run, `expect(...)` for every wait
(never a bare sleep), and no assertion on 3D pixels — the canvas is
checked for *existence and a GL context*, and the reader-facing
consequences of clicking it are checked through the DOM around it.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import Locator, Page, expect

pytestmark = pytest.mark.e2e

#: Every page the site ships, as linked from the header and the sitemap.
PAGES = ("index.html", "table.html", "about.html", "whats-new.html", "404.html")

#: The pages that carry the site header; 404.html deliberately has none.
NAV_PAGES = tuple(name for name in PAGES if name != "404.html")

#: `table.js` renders this many rows before "Show more".
CHUNK = 100

MOBILE = {"width": 375, "height": 812}
DESKTOP = {"width": 1280, "height": 800}

DIGITS = re.compile(r"\d+")


def fmt(value: int) -> str:
    """`1234 -> "1,234"` — `format.js` uses `toLocaleString("en-US")`."""
    return f"{value:,}"


def records(catalog: dict) -> list[dict]:
    """Every row of the served `search-index.json`."""
    return catalog["rows"]


def with_access(catalog: dict, tier: str) -> list[dict]:
    """The served rows whose access tier is `tier`."""
    return [row for row in records(catalog) if row.get("access") == tier]


def rendered(matches: int) -> int:
    """How many `<tr>`s `table.js` puts on screen for `matches` rows."""
    return min(matches, CHUNK)


def stat(page: Page, name: str) -> int:
    """The `#stats-bar` figure called `name`, as an integer."""
    text = page.locator(f'#stats-bar dd[data-stat="{name}"]').inner_text()
    return int("".join(DIGITS.findall(text)) or 0)


def open_graph(page: Page) -> None:
    """Load the graph page and wait for the scene to be up."""
    page.goto("index.html")
    expect(page.locator("#graph canvas")).to_be_visible()


@contextmanager
def search_index_loaded(page: Page) -> Iterator[None]:
    """Wrap whatever focuses the search box; return once it can answer.

    The graph page fetches `search-index.json` the first time the box is
    focused, and `search.js` debounces the query without re-running it
    when the index later arrives — so a query typed into a box that was
    focused a moment ago is answered "no results" and never revisited.
    Waiting for that response, body and all, is what makes the next
    keystroke deterministic. (That the app cannot recover on its own is
    a real bug, filed against `app-graph.js`; when it is fixed this wait
    becomes belt and braces rather than load-bearing.)
    """
    with page.expect_response(re.compile(r"search-index\.json")) as response:
        yield
    response.value.finished()


def open_dataset_link(panel: Locator) -> Locator:
    return panel.get_by_role("link", name=re.compile("Open dataset"))


# ---------------------------------------------------------------------------
# Every page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PAGES)
def test_page_loads_without_console_errors(page: Page, name: str) -> None:
    response = page.goto(name)
    assert response is not None and response.ok, name
    expect(page).to_have_title(re.compile("Clinical Data Atlas"))
    # Also asserted at build time (tests/test_sitebuild.py), but worth
    # re-asserting in the browser: `whats-new.html`'s single <h1> depends
    # on `sitebuild.demote_headings` shifting each embedded changelog --
    # whose markdown is a standalone `#`-titled document -- under the
    # page's own heading.
    expect(page.locator("h1")).to_have_count(1)
    # The console guard in conftest.py asserts the "0 errors" half.


@pytest.mark.parametrize("name", PAGES)
def test_no_horizontal_scroll_on_a_small_phone(page: Page, name: str) -> None:
    page.set_viewport_size(MOBILE)
    page.goto(name)
    expect(page.locator("h1").first).to_be_attached()
    overflow = page.evaluate(
        """() => ({
             scroll: document.documentElement.scrollWidth,
             inner: window.innerWidth,
           })"""
    )
    assert overflow["scroll"] <= overflow["inner"], f"{name} overflows: {overflow}"


@pytest.mark.parametrize("name", NAV_PAGES)
def test_the_whole_nav_is_on_screen_on_a_phone(page: Page, name: str) -> None:
    """Every primary link is reachable *and* visible at 375px.

    Q-G: the nav used to be a 121px scroller holding 252px of links, so
    "What's new" and "About" sat off its end behind a 14px fade nobody
    reads as an affordance. It now takes a row of its own. Asserted as
    "no link is clipped by the nav's own box or by the viewport" rather
    than by pinning a header height, so a future header that solves it
    some other way still passes.
    """
    page.set_viewport_size(MOBILE)
    page.goto(name)
    nav = page.locator(".site-nav")
    expect(nav).to_be_visible()

    geometry = nav.evaluate(
        """(nav) => {
             const clip = nav.getBoundingClientRect();
             return {
               scrollWidth: nav.scrollWidth,
               clientWidth: nav.clientWidth,
               links: [...nav.querySelectorAll('a')].map((a) => {
                 const r = a.getBoundingClientRect();
                 return {
                   text: a.textContent.trim(),
                   hidden:
                     r.left < clip.left - 0.5 ||
                     r.right > clip.right + 0.5 ||
                     r.left < -0.5 ||
                     r.right > window.innerWidth + 0.5,
                 };
               }),
             };
           }"""
    )
    assert geometry["links"], f"{name} has no nav links"
    clipped = [link["text"] for link in geometry["links"] if link["hidden"]]
    assert not clipped, f"{name}: {clipped} clipped out of the nav ({geometry})"
    assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, geometry


@pytest.mark.parametrize("name", NAV_PAGES)
def test_the_header_reserves_exactly_its_own_height_on_a_phone(
    page: Page, name: str
) -> None:
    """`--header-h` is the fixed header's height *and* the body's padding.

    The phone header is two rows tall (see above), which only works
    because every frame offset on the site reads the same token. If one
    of them stops tracking it, the header starts covering the first
    screenful of content -- a failure that is invisible to a smoke test
    but not to a reader.
    """
    page.set_viewport_size(MOBILE)
    page.goto(name)
    expect(page.locator(".site-header")).to_be_visible()
    offsets = page.evaluate(
        """() => ({
             header: document.querySelector('.site-header').getBoundingClientRect().height,
             padding: parseFloat(getComputedStyle(document.body).paddingTop),
           })"""
    )
    assert offsets["header"] == pytest.approx(offsets["padding"], abs=1), (
        name,
        offsets,
    )


# ---------------------------------------------------------------------------
# The graph page
# ---------------------------------------------------------------------------


def test_graph_has_a_webgl_canvas(page: Page, console_guard: list[str]) -> None:
    page.goto("index.html")
    # Wait for whichever of the two outcomes arrives: waiting for the
    # canvas alone would make the fallback branch below unreachable.
    expect(page.locator("#graph canvas, #graph .graph-empty").first).to_be_visible()
    canvas = page.locator("#graph canvas")

    has_gl = page.evaluate(
        """() => {
             const canvas = document.querySelector('#graph canvas');
             if (!canvas) return false;
             try {
               return !!(canvas.getContext('webgl2') || canvas.getContext('webgl'));
             } catch {
               return false;
             }
           }"""
    )
    if has_gl:
        box = canvas.bounding_box()
        assert box and box["width"] > 0 and box["height"] > 0
        return

    # No GL on this machine: the page owes the reader a way out, not a
    # blank stage. `app-graph.js` renders that into `.graph-empty`.
    expect(page.locator("#graph .graph-empty")).to_be_visible()
    expect(page.locator('#graph .graph-empty a[href="table.html"]')).to_be_visible()
    # That path is reached through `main().catch()`, which reports the
    # failure it just recovered from; the guard is right to fail every
    # other test on a console error, and this is the one place where one
    # is the expected behaviour.
    console_guard.clear()


def test_stats_bar_counts_the_catalog(page: Page, catalog: dict) -> None:
    open_graph(page)
    assert catalog["stats"]["record_count"] > 0
    expect(page.locator('#stats-bar dd[data-stat="record_count"]')).to_have_text(
        fmt(catalog["stats"]["record_count"])
    )
    assert stat(page, "sources") == len(catalog["stats"]["per_source"])
    assert stat(page, "modalities") == len(catalog["stats"]["per_modality"])

    # "In view" is written by the scene itself once the backbone is up.
    expect(page.locator('#stats-bar dd[data-stat="visible"]')).not_to_have_text("—")
    assert stat(page, "visible") > 0


def test_search_opens_the_record_panel(page: Page, catalog: dict) -> None:
    wanted = records(catalog)[0]
    open_graph(page)
    with search_index_loaded(page):
        page.locator("#search-input").click()  # focus is what loads the index
    page.locator("#search-input").fill(wanted["name"])

    results = page.locator("#search-results li")
    expect(results.first).to_be_visible()
    expect(results.filter(has_text=wanted["name"])).not_to_have_count(0)

    # Enter takes the *active* option, which is the first one; asserting
    # on what the list actually says beats assuming the ranking.
    chosen = results.first.locator(".result-name").inner_text()
    page.locator("#search-input").press("Enter")

    panel = page.locator("#panel")
    expect(panel).to_be_visible()
    expect(page.locator("#panel-title")).to_have_text(chosen)
    expect(open_dataset_link(panel)).to_have_attribute(
        "href", re.compile(r"^https?://")
    )
    expect(panel.locator(".badge[data-access]")).to_have_count(1)
    assert re.search(r"[?&]node=", page.url), page.url


def test_expanding_a_hub_adds_nodes_and_announces_it(page: Page) -> None:
    open_graph(page)
    before = stat(page, "visible")
    assert before > 0

    # Whichever modality label the scene decided to draw: which one it is
    # depends on the catalog, that there is one does not. Pin it by id
    # before clicking — the label layer reshuffles its pool as the
    # camera moves, so "the first visible one" is not stable across two
    # round trips, and a modality node's own id is.
    drawn = page.locator('.node-label[data-id^="modality:"]:visible').first
    expect(drawn).to_be_visible()
    label = page.locator(f'.node-label[data-id="{drawn.get_attribute("data-id")}"]')
    name = label.inner_text()
    label.click()

    visible = page.locator('#stats-bar dd[data-stat="visible"]')
    expect(visible).not_to_have_text(fmt(before))
    assert stat(page, "visible") > before

    expect(page.locator("#a11y-live")).to_contain_text(f"Expanded {name}")
    expect(page.locator("#panel-title")).to_have_text(name)


def test_node_query_parameter_deep_links_to_a_dataset(
    page: Page, catalog: dict
) -> None:
    wanted = records(catalog)[0]
    page.goto(f"index.html?node={quote(wanted['id'], safe='')}")
    expect(page.locator("#graph canvas")).to_be_visible()

    expect(page.locator("#panel")).to_be_visible()
    expect(page.locator("#panel-title")).to_have_text(wanted["name"])
    expect(open_dataset_link(page.locator("#panel"))).to_be_visible()


def test_keyboard_reaches_search_then_the_panel_close_button(
    page: Page, catalog: dict
) -> None:
    open_graph(page)

    # Tab from the top of the document rather than clicking: the count is
    # not the contract, "you can get there with Tab alone" is. Tabbing
    # into the box is itself what starts the index download.
    with search_index_loaded(page):
        for _ in range(12):
            page.keyboard.press("Tab")
            if page.evaluate("() => document.activeElement?.id") == "search-input":
                break
        else:  # pragma: no cover - only reached when the header regresses
            pytest.fail("Tab never reached #search-input")

    page.keyboard.type(records(catalog)[0]["name"][:24])
    expect(page.locator("#search-results li").first).to_be_visible()

    # The listbox is a combobox popup: "reaching" a result means the
    # input points at one via aria-activedescendant, not that the option
    # itself takes focus.
    expect(page.locator("#search-input")).to_have_attribute(
        "aria-activedescendant", "search-results-option-0"
    )
    expect(page.locator('#search-results li[aria-selected="true"]')).to_have_count(1)

    page.keyboard.press("Enter")
    expect(page.locator("#panel")).to_be_visible()
    assert page.evaluate("() => document.activeElement?.id") == "panel-title"

    page.keyboard.press("Tab")
    close = page.locator("#panel [data-panel-close]")
    assert close.evaluate("(el) => el === document.activeElement")

    page.keyboard.press("Enter")
    expect(page.locator("#panel")).to_be_hidden()


def test_panel_is_a_bottom_sheet_on_a_phone(page: Page, catalog: dict) -> None:
    wanted = records(catalog)[0]
    page.set_viewport_size(MOBILE)
    page.goto(f"index.html?node={quote(wanted['id'], safe='')}")

    panel = page.locator("#panel")
    expect(panel).to_be_visible()
    box = panel.bounding_box()
    assert box is not None

    # Full-width, anchored to the bottom edge, not covering the header:
    # the drawer at >= 900px is none of those things.
    assert box["x"] == pytest.approx(0, abs=1)
    assert box["width"] == pytest.approx(MOBILE["width"], abs=1)
    assert box["y"] + box["height"] == pytest.approx(MOBILE["height"], abs=1)
    assert box["y"] > 0


# ---------------------------------------------------------------------------
# The table page
# ---------------------------------------------------------------------------


def test_access_filter_reduces_the_rows_and_the_url(page: Page, catalog: dict) -> None:
    total = len(records(catalog))
    opens = with_access(catalog, "open")
    if not 0 < len(opens) < total:
        pytest.skip("this catalog is entirely open (or has no open datasets)")

    page.goto("table.html")
    rows = page.locator("#table-body tr")
    expect(rows).to_have_count(rendered(total))
    expect(page.locator("#table-status")).to_contain_text(
        f"{fmt(total)} of {fmt(total)} datasets"
    )

    page.locator('[data-facet-options="access"] input[value="open"]').check()

    # Past CHUNK rows the reduction shows in the status line rather than
    # in the row count -- assert both, each in the form it can take.
    expect(page.locator("#table-status")).to_contain_text(
        f"{fmt(len(opens))} of {fmt(total)} datasets"
    )
    expect(rows).to_have_count(rendered(len(opens)))
    assert "access=open" in page.url


def test_sorting_a_column_toggles_aria_sort(page: Page, catalog: dict) -> None:
    page.goto("table.html")
    rows = page.locator("#table-body tr")
    expect(rows).to_have_count(rendered(len(records(catalog))))

    top_name = rows.first.locator(".cell-name button")
    header = page.locator('th[data-column="name"]')
    expect(header).to_have_attribute("aria-sort", "ascending")
    first_ascending = top_name.inner_text()

    header.locator("button").click()
    expect(header).to_have_attribute("aria-sort", "descending")
    assert top_name.inner_text() != first_ascending
    assert "sort=-name" in page.url

    # Back to where it started: the toggle is a toggle, not a one-way trip.
    header.locator("button").click()
    expect(header).to_have_attribute("aria-sort", "ascending")
    expect(top_name).to_have_text(first_ascending)


def test_csv_export_downloads_the_filtered_rows(page: Page, catalog: dict) -> None:
    opens = with_access(catalog, "open")
    if not opens:
        pytest.skip("this catalog has no open datasets")

    page.goto("table.html")
    expect(page.locator("#table-body tr")).to_have_count(
        rendered(len(records(catalog)))
    )
    page.locator('[data-facet-options="access"] input[value="open"]').check()
    expect(page.locator("#table-status")).to_contain_text(f"{fmt(len(opens))} of ")

    with page.expect_download() as download_info:
        page.locator("#download-csv").click()
    download = download_info.value

    assert download.suggested_filename == "clinical-data-atlas.csv"
    path = download.path()
    assert path is not None
    text = Path(path).read_text(encoding="utf-8-sig")  # utf-8-sig eats the BOM
    # Parsed rather than split on newlines: dataset names legitimately
    # contain line breaks, and RFC 4180 quoting is exactly what protects
    # them -- a naive `splitlines()` would count those twice.
    exported = list(csv.reader(io.StringIO(text)))

    assert exported[0][:3] == ["ID", "Name", "Source"]
    # The whole filtered set, not just the rendered page of it.
    assert len(exported) == len(opens) + 1
    assert {row[0] for row in exported[1:]} == {row["id"] for row in opens}


def test_a_table_row_opens_the_panel(page: Page, catalog: dict) -> None:
    page.goto("table.html")
    rows = page.locator("#table-body tr")
    expect(rows).to_have_count(rendered(len(records(catalog))))

    opener = rows.first.locator(".cell-name button")
    name = opener.inner_text()
    opener.click()

    expect(page.locator("#panel")).to_be_visible()
    expect(page.locator("#panel-title")).to_have_text(name)


def test_the_table_is_at_the_top_of_the_page_on_a_phone(page: Page) -> None:
    """Q-F: below 900px the rail is collapsed, so the data leads.

    Measured against the real 2,655-record catalog the rail is ~1,818px
    tall; stacked above `.table-panel` it put the search box 1,914px and
    the first row 2,038px down a 375x812 phone. The whole
    disclosure/toolbar/count/table stack now has to fit inside the first
    screenful, with the table itself well inside it.
    """
    page.set_viewport_size(MOBILE)
    page.goto("table.html")
    expect(page.locator("#table-body tr").first).to_be_visible()

    toggle = page.locator(".facets-toggle")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "false")

    tops = {}
    for selector in (".facets-toggle", ".toolbar", "#table-status", ".table-wrap"):
        box = page.locator(selector).bounding_box()
        assert box is not None, selector
        tops[selector] = box["y"]
    assert max(tops.values()) < MOBILE["height"], tops
    # Not merely on screen: near the top of it, above the fold on any
    # phone this site claims to support.
    assert tops[".table-wrap"] < 400, tops

    # Collapsed means collapsed, not merely scrolled past.
    panel = page.locator(f"#{toggle.get_attribute('aria-controls')}")
    expect(panel).to_be_hidden()


def test_the_filters_disclosure_opens_the_rail_from_the_keyboard(page: Page) -> None:
    """Focus it, press Enter, and the rail it names appears."""
    page.set_viewport_size(MOBILE)
    page.goto("table.html")
    expect(page.locator("#table-body tr").first).to_be_visible()

    toggle = page.locator(".facets-toggle")
    panel = page.locator(f"#{toggle.get_attribute('aria-controls')}")
    toggle.focus()
    assert toggle.evaluate("(el) => el === document.activeElement")

    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(panel).to_be_visible()
    expect(page.locator("#a11y-live")).to_contain_text("Filters shown")
    # Focus stays put, so a second press is a close rather than a hunt.
    assert toggle.evaluate("(el) => el === document.activeElement")

    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(panel).to_be_hidden()


def test_filtering_from_the_phone_rail_updates_the_table_and_the_count(
    page: Page, catalog: dict
) -> None:
    """A facet ticked on a phone filters the table and re-labels the button."""
    total = len(records(catalog))
    opens = with_access(catalog, "open")
    if not 0 < len(opens) < total:
        pytest.skip("this catalog is entirely open (or has no open datasets)")

    page.set_viewport_size(MOBILE)
    page.goto("table.html")
    expect(page.locator("#table-body tr").first).to_be_visible()

    toggle = page.locator(".facets-toggle")
    expect(toggle).to_have_text("Filters")
    toggle.click()
    page.locator('[data-facet-options="access"] input[value="open"]').check()

    expect(page.locator("#table-status")).to_contain_text(
        f"{fmt(len(opens))} of {fmt(total)} datasets"
    )
    expect(page.locator("#table-body tr")).to_have_count(rendered(len(opens)))
    # Collapsed, the button is the only thing that can still say a filter
    # is on -- so it has to say it.
    expect(toggle).to_have_text("Filters (1)")
    # And the count line stays where the reader can read it.
    status = page.locator("#table-status").bounding_box()
    assert status is not None and 0 < status["y"] < MOBILE["height"], status


def test_a_panel_chip_on_a_phone_lands_focus_on_the_filters_button(
    page: Page,
) -> None:
    """Filtering from a record chip must not drop focus onto `<body>`.

    The chip's natural landing place is the checkbox it just ticked, and
    below 900px that checkbox is inside the collapsed rail, where
    `.focus()` is a silent no-op. WCAG 2.4.3 is Level A, and the fallout
    is concrete: a keyboard reader returned to the top of the document
    has to tab past the skip link, the wordmark, four nav links, the
    header search and the theme toggle to get back to the table.
    """
    page.set_viewport_size(MOBILE)
    page.goto("table.html")
    expect(page.locator("#table-body tr").first).to_be_visible()

    page.locator("#table-body tr").first.locator(".cell-name button").click()
    panel = page.locator("#panel")
    expect(panel).to_be_visible()

    # Whichever facet chip this record happens to carry; that it has one
    # is what the test needs, which one it is is the catalog's business.
    chip = panel.locator("[data-chip-kind]").first
    expect(chip).to_be_visible()
    chip.click()

    toggle = page.locator(".facets-toggle")
    expect(toggle).to_be_visible()
    assert toggle.evaluate("(el) => el === document.activeElement"), page.evaluate(
        "() => document.activeElement?.tagName + '.' + document.activeElement?.className"
    )
    # And it says what just happened, rather than only catching the focus.
    expect(toggle).to_have_text(re.compile(r"^Filters \(\d+\)$"))


def test_the_desktop_table_page_keeps_its_open_rail(page: Page) -> None:
    """Above the breakpoint nothing about the table page changed."""
    page.set_viewport_size(DESKTOP)
    page.goto("table.html")
    expect(page.locator("#table-body tr").first).to_be_visible()

    expect(page.locator(".facets-toggle")).to_be_hidden()
    facets = page.locator(".facets")
    expect(facets).to_be_visible()
    expect(page.locator('[data-facet-options="domain"] input').first).to_be_visible()

    # The rail is the left column and the table its neighbour, not a
    # stack -- the two-column grid, unchanged.
    rail = facets.bounding_box()
    wrap = page.locator(".table-wrap").bounding_box()
    assert rail is not None and wrap is not None
    assert rail["x"] + rail["width"] <= wrap["x"] + 1, (rail, wrap)


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


def test_theme_choice_survives_a_reload(page: Page) -> None:
    page.goto("about.html")
    toggle = page.locator("#theme-toggle")
    expect(toggle).to_be_visible()

    toggle.click()
    chosen = page.locator("html").get_attribute("data-theme")
    assert chosen in {"dark", "light"}
    assert page.evaluate("() => localStorage.getItem('cda-theme')") == chosen

    page.reload()
    # Set by the pre-paint script in <head>, i.e. before the first frame.
    expect(page.locator("html")).to_have_attribute("data-theme", chosen)

    toggle.click()
    flipped = page.locator("html").get_attribute("data-theme")
    assert flipped != chosen
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", flipped)


# ---------------------------------------------------------------------------
# Screenshots (kept as artifacts; never diffed)
# ---------------------------------------------------------------------------


def _shoot(page: Page, artifacts_dir: Path, name: str) -> None:
    """Screenshot `page` in both themes at the current viewport."""
    size = page.viewport_size or DESKTOP
    for _ in range(2):
        page.locator("#theme-toggle").click()
        theme = page.locator("html").get_attribute("data-theme")
        page.screenshot(
            path=str(
                artifacts_dir / f"{name}-{size['width']}x{size['height']}-{theme}.png"
            )
        )


@pytest.mark.parametrize("name", ["index.html", "table.html"])
def test_screenshots(page: Page, artifacts_dir: Path, name: str) -> None:
    """Save desktop and phone captures of a page in both themes.

    No pixel diffing: these are for a human looking at a CI failure, and
    a 3D canvas cannot be compared byte-for-byte anyway.
    """
    page.set_viewport_size(DESKTOP)
    page.goto(name)
    if name == "index.html":
        expect(page.locator("#graph canvas")).to_be_visible()
    else:
        expect(page.locator("#table-body tr").first).to_be_visible()

    stem = "graph" if name == "index.html" else "table"
    _shoot(page, artifacts_dir, stem)
    page.set_viewport_size(MOBILE)
    _shoot(page, artifacts_dir, stem)


# ---------------------------------------------------------------------------
# The guards themselves
# ---------------------------------------------------------------------------


def test_a_data_less_build_only_skips_without_a_catalog(
    missing_data_guard, tmp_path: Path
) -> None:
    """The "no data yet" skip must not be able to hide a broken build.

    `conftest.catalog` skips the whole suite when the served site has no
    `data/`, which is right in the window before the first data commit
    and dangerous after it. This pins the sentinel that separates the
    two: no committed catalog -> skip, committed catalog -> fail.
    """
    assert not missing_data_guard(tmp_path)

    catalog_file = tmp_path / "data" / "catalog" / "catalog.jsonl"
    catalog_file.parent.mkdir(parents=True)
    catalog_file.write_text('{"id": "src:one"}\n', encoding="utf-8")

    assert missing_data_guard(tmp_path)


# ---------------------------------------------------------------------------
# The real catalog (skipped wherever `data/` hasn't been harvested)
# ---------------------------------------------------------------------------


def test_real_data_smoke(page: Page, real_base_url: str) -> None:
    """The pages come up against the actual pipeline output too.

    The rest of the file runs against whatever is served — by default
    the fixtures, because they are small and fast. This one insists on
    the real `data/` whenever the checkout has it.
    """
    page.goto(f"{real_base_url}index.html")
    expect(page.locator("#graph canvas")).to_be_visible()
    assert stat(page, "record_count") > 0
    assert stat(page, "visible") > 0

    page.goto(f"{real_base_url}table.html")
    expect(page.locator("#table-body tr").first).to_be_visible()
    expect(page.locator("#table-status")).to_contain_text("datasets")
