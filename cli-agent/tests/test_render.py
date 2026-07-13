"""Render laws: files are code, values are data; one inert pass; dot-walk rules."""
import pytest

from medulla.v2.render import RenderError, render


def test_var_substitution_and_default(tmp_path):
    out = render("x={{var:A}} y={{var:B:-def}} z={{var:C:-}}", tmp_path, {"A": "1"})
    assert out == "x=1 y=def z="


def test_empty_var_takes_default(tmp_path):
    assert render("{{var:A:-d}}", tmp_path, {"A": ""}) == "d"


def test_values_are_inert(tmp_path):
    # a value containing template syntax is inserted literally, never re-expanded
    out = render("{{var:A}}", tmp_path, {"A": "{{var:SECRET}}", "SECRET": "x"})
    assert out == "{{var:SECRET}}"


def test_value_cannot_include_files(tmp_path):
    (tmp_path / "f.md").write_text("content", encoding="utf-8")
    out = render("{{var:A}}", tmp_path, {"A": "{{file:f.md}}"})
    assert out == "{{file:f.md}}"


def test_file_inclusion_resolves_vars_inside(tmp_path):
    (tmp_path / "inc.md").write_text("hello {{var:NAME}}", encoding="utf-8")
    out = render("{{file:inc.md}}!", tmp_path, {"NAME": "world"})
    assert out == "hello world!"


def test_file_inclusion_relative_to_includer(tmp_path):
    sub = tmp_path / "prompts"
    sub.mkdir()
    (sub / "base.md").write_text("A + {{file:_style.md}}", encoding="utf-8")
    (sub / "_style.md").write_text("B", encoding="utf-8")
    assert render("{{file:prompts/base.md}}", tmp_path, {}) == "A + B"


def test_missing_file_raises(tmp_path):
    with pytest.raises(RenderError, match="not found"):
        render("{{file:nope.md}}", tmp_path, {})


def test_inclusion_depth_limit(tmp_path):
    # a.md includes itself -> depth explosion -> error with the chain
    (tmp_path / "a.md").write_text("{{file:a.md}}", encoding="utf-8")
    with pytest.raises(RenderError, match="deeper than"):
        render("{{file:a.md}}", tmp_path, {})


def test_input_scalar(tmp_path):
    assert render("<{{input}}>", tmp_path, {}, input_value="t1", has_input=True) == "<t1>"


def test_input_object_renders_compact_json(tmp_path):
    out = render("{{input}}", tmp_path, {}, input_value={"a": 1}, has_input=True)
    assert out == '{"a":1}'


def test_input_dot_walk_nested(tmp_path):
    out = render("{{input.a.b}}", tmp_path, {},
                 input_value={"a": {"b": "deep"}}, has_input=True)
    assert out == "deep"


def test_input_missing_field_raises(tmp_path):
    with pytest.raises(RenderError, match="missing"):
        render("{{input.slug}}", tmp_path, {}, input_value={"a": 1}, has_input=True)


def test_input_missing_field_with_default(tmp_path):
    out = render("{{input.slug:-}}", tmp_path, {}, input_value={"a": 1}, has_input=True)
    assert out == ""


def test_input_outside_pool_raises(tmp_path):
    with pytest.raises(RenderError, match="no inputs"):
        render("{{input}}", tmp_path, {})


def test_input_index_and_count(tmp_path):
    out = render("{{input_index}}/{{input_count}}", tmp_path, {},
                 input_value="x", has_input=True, input_index=2, input_count=5)
    assert out == "2/5"


def test_last_tokens(tmp_path):
    out = render("{{last.node}}:{{last.signal}}:{{last.rc}}", tmp_path, {},
                 last={"node": "plan", "signal": "planned", "rc": 0})
    assert out == "plan:planned:0"


def test_last_empty_when_no_history(tmp_path):
    assert render("[{{last.message}}]", tmp_path, {}) == "[]"
