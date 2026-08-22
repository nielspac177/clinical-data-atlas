"""Tests for atlas.io: JSON helpers, atomic writes, JSONL, and text utilities."""

import hashlib
import json

from atlas import io

# ---------------------------------------------------------------------------
# canonical_json / pretty_json
# ---------------------------------------------------------------------------


def test_canonical_json_is_sorted_compact_and_newline_terminated():
    assert io.canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}\n'


def test_canonical_json_keeps_unicode_unescaped():
    text = io.canonical_json({"name": "café"})
    assert "café" in text
    assert "\\u" not in text


def test_canonical_json_stable_regardless_of_input_key_order():
    assert io.canonical_json({"a": 1, "b": 2}) == io.canonical_json({"b": 2, "a": 1})


def test_pretty_json_is_indented_sorted_and_newline_terminated():
    assert io.pretty_json({"b": 2, "a": 1}) == '{\n  "a": 1,\n  "b": 2\n}\n'


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


def test_content_hash_format():
    digest = io.content_hash({"a": 1})
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    int(digest.split(":", 1)[1], 16)  # must be valid hex


def test_content_hash_stable_under_key_order():
    assert io.content_hash({"a": 1, "b": 2}) == io.content_hash({"b": 2, "a": 1})


def test_content_hash_differs_for_different_content():
    assert io.content_hash({"a": 1}) != io.content_hash({"a": 2})


def test_content_hash_matches_documented_convention():
    """Convention (see io.content_hash docstring): sha256 of the compact
    canonical JSON body -- canonical_json(obj) with its trailing newline
    removed, i.e. json.dumps(obj, sort_keys=True, separators=(",", ":"),
    ensure_ascii=False) hashed directly, independently reproduced here.
    """
    obj = {"b": 2, "a": 1, "s": "café"}
    body = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert io.content_hash(obj) == expected


# ---------------------------------------------------------------------------
# write_atomic
# ---------------------------------------------------------------------------


def test_write_atomic_writes_expected_content(tmp_path):
    target = tmp_path / "out.json"
    io.write_atomic(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_write_atomic_leaves_no_tmp_file(tmp_path):
    target = tmp_path / "out.json"
    io.write_atomic(target, "hello\n")
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_atomic_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "deeper" / "out.json"
    io.write_atomic(target, "data\n")
    assert target.read_text(encoding="utf-8") == "data\n"
    assert list((tmp_path / "nested" / "deeper").glob("*.tmp")) == []


def test_write_atomic_overwrites_existing_file(tmp_path):
    target = tmp_path / "out.json"
    io.write_atomic(target, "first\n")
    io.write_atomic(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_atomic_accepts_str_path(tmp_path):
    target = str(tmp_path / "out.json")
    io.write_atomic(target, "hi\n")
    assert (tmp_path / "out.json").read_text(encoding="utf-8") == "hi\n"


# ---------------------------------------------------------------------------
# read_jsonl / write_jsonl
# ---------------------------------------------------------------------------


def test_write_then_read_jsonl_roundtrip(tmp_path):
    target = tmp_path / "rows.jsonl"
    rows = [{"id": "b", "n": 2}, {"id": "a", "n": 1}]
    io.write_jsonl(target, rows)
    assert io.read_jsonl(target) == rows


def test_write_jsonl_writes_one_canonical_line_per_row(tmp_path):
    target = tmp_path / "rows.jsonl"
    io.write_jsonl(target, [{"b": 2, "a": 1}, {"z": 9}])
    assert target.read_text(encoding="utf-8") == '{"a":1,"b":2}\n{"z":9}\n'


def test_write_jsonl_is_atomic_no_tmp_left(tmp_path):
    target = tmp_path / "rows.jsonl"
    io.write_jsonl(target, [{"a": 1}])
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_jsonl_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "rows.jsonl"
    io.write_jsonl(target, [{"a": 1}])
    assert io.read_jsonl(target) == [{"a": 1}]


def test_read_jsonl_skips_blank_lines(tmp_path):
    target = tmp_path / "rows.jsonl"
    target.write_text('{"a":1}\n\n{"a":2}\n', encoding="utf-8")
    assert io.read_jsonl(target) == [{"a": 1}, {"a": 2}]


def test_write_jsonl_empty_rows_writes_empty_file(tmp_path):
    target = tmp_path / "rows.jsonl"
    io.write_jsonl(target, [])
    assert target.read_text(encoding="utf-8") == ""
    assert io.read_jsonl(target) == []


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_brief_example():
    assert io.slugify("QIN Breast DCE-MRI") == "qin-breast-dce-mri"


def test_slugify_lowercases():
    assert io.slugify("HELLO World") == "hello-world"


def test_slugify_strips_accents():
    assert io.slugify("Café Münster") == "cafe-munster"


def test_slugify_strips_leading_and_trailing_dashes():
    assert io.slugify("  ??Weird!! Name??  ") == "weird-name"


def test_slugify_keeps_dots_underscores_and_dashes():
    assert io.slugify("file_name.v2-final") == "file_name.v2-final"


def test_slugify_collapses_runs_of_invalid_chars_to_one_dash():
    assert io.slugify("a///b   c") == "a-b-c"


# ---------------------------------------------------------------------------
# strip_html
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags_and_joins_blocks():
    assert io.strip_html("<p>Hello</p><p>World</p>") == "Hello World"


def test_strip_html_unescapes_entities():
    assert io.strip_html("Tom &amp; Jerry &lt;3") == "Tom & Jerry <3"


def test_strip_html_collapses_whitespace():
    assert io.strip_html("A   B\n\nC") == "A B C"


def test_strip_html_combined():
    html_in = "<p>Hello &amp; welcome</p>\n<p>World</p>"
    assert io.strip_html(html_in) == "Hello & welcome World"


def test_strip_html_plain_text_untouched():
    assert io.strip_html("plain text") == "plain text"


def test_strip_html_strips_leading_trailing_whitespace():
    assert io.strip_html("  <b>hi</b>  ") == "hi"


def test_strip_html_drops_script_contents():
    assert io.strip_html("<script>alert(1);</script>Hello") == "Hello"


def test_strip_html_drops_style_contents():
    assert io.strip_html("<style>.a{color:red}</style>Hello") == "Hello"


def test_strip_html_drops_noscript_contents():
    assert io.strip_html("<noscript>Enable JS</noscript>Hello") == "Hello"


def test_strip_html_drops_script_contents_amid_other_text():
    html_in = "<p>Before</p><script>var x = '<p>fake</p>';</script><p>After</p>"
    assert io.strip_html(html_in) == "Before After"


# ---------------------------------------------------------------------------
# first_words
# ---------------------------------------------------------------------------


def test_first_words_truncates_to_n():
    text = " ".join(f"word{i}" for i in range(50))
    result = io.first_words(text, n=40)
    assert result.split() == [f"word{i}" for i in range(40)]
    assert len(result.split()) <= 40


def test_first_words_no_trailing_ellipsis():
    text = " ".join(f"w{i}" for i in range(45))
    assert not io.first_words(text, n=40).endswith("...")


def test_first_words_shorter_than_n_returned_whole():
    assert io.first_words("only three words", n=40) == "only three words"


def test_first_words_default_n_is_40():
    text = " ".join(f"w{i}" for i in range(100))
    assert len(io.first_words(text).split()) == 40


def test_first_words_collapses_internal_whitespace():
    assert io.first_words("a\n\nb   c", n=40) == "a b c"


def test_first_words_empty_string():
    assert io.first_words("", n=40) == ""
