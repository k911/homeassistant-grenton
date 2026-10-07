"""Regression checks for importing individual argument controls safely."""

import pytest

from custom_components.homeassistant_grenton.domain.lua_arguments import (
    imported_lua_argument,
    split_lua_arguments,
)
from custom_components.homeassistant_grenton.domain.scene_arguments import (
    imported_arguments,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('42, "abc", true, nil', ["42", '"abc"', "true", "nil"]),
        ('"a,b", OTHER:get(1, 2), {1, 2}', ['"a,b"', "OTHER:get(1, 2)", "{1, 2}"]),
        (r'"a\",b", "c"', [r'"a\",b"', '"c"']),
        ('[=[a,b]=], state["a,b"]', ["[=[a,b]=]", 'state["a,b"]']),
        ("", []),
        ("1, function(a,b) return a,b end", ["1, function(a,b) return a,b end"]),
        ("1 -- comment, next\n, 2", ["1 -- comment, next\n, 2"]),
        ("1, unfinished(", ["1, unfinished("]),
        ('1, "unfinished', ['1, "unfinished']),
        ("1, ", ["1, "]),
    ],
)
def test_top_level_arguments_are_split_without_changing_nested_lua(value, expected):
    assert split_lua_arguments(value) == expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('"abc"', {"type": "string", "value": "abc"}),
        (r"'it\'s'", {"type": "string", "value": "it's"}),
        (r'"a\n\0279"', {"type": "string", "value": "a\n\x1b9"}),
        ("-1.25e2", {"type": "number", "value": -125.0}),
        ("9007199254740993", {"type": "number", "value": 9007199254740993}),
        ("false", {"type": "boolean", "value": False}),
        ("nil", {"type": "nil", "value": None}),
        ('"a" .. "b"', {"type": "lua", "value": '"a" .. "b"'}),
        (r'"\unknown"', {"type": "lua", "value": r'"\unknown"'}),
        ("OTHER:get(1, 2)", {"type": "lua", "value": "OTHER:get(1, 2)"}),
    ],
)
def test_literals_become_typed_controls_and_expressions_remain_lua(
    expression, expected
):
    assert imported_lua_argument(expression) == expected


def test_imported_scene_has_one_row_per_argument():
    assert imported_arguments("SCRIPT", '0, "bedroom", false, nil') == [
        {"type": "number", "value": 0},
        {"type": "string", "value": "bedroom"},
        {"type": "boolean", "value": False},
        {"type": "nil", "value": None},
    ]
