from __future__ import annotations

from datetime import date, datetime
from io import StringIO

import pytest
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from tkn_objstorage_imgcatalog.notes import format_existing_note, serialize, split_note


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-07",
        "2026-10-07T09:30:00+09:00",
        "2026-10-07T00:30:00Z",
        "2026-10-07T09:30:00.123456789+09:00",
        "2026-10-07T09:30:00.120000-05:00",
    ],
)
@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_date_lexemes_survive_formatting_and_become_strings(value, quote):
    text = f"---\ntype: image\nassetId: example\ncustomDate: {quote}{value}{quote} # keep\n---\n\n# Image\n"
    before, body = split_note(text)
    result = format_existing_note(text)
    after, new_body = split_note(result)
    assert after["customDate"] == before["customDate"] == value
    assert f'customDate: "{value}" # keep' in result
    assert new_body == body
    assert result == format_existing_note(result)
    # Check the output using an independent default loader, not our constructor.
    for version in [(1, 1), (1, 2)]:
        yaml = YAML(typ="safe")
        yaml.version = version
        loaded = yaml.load(result.removeprefix("---\n").split("\n---\n", 1)[0])
        assert isinstance(loaded["customDate"], str)
        assert loaded["customDate"] == value


def test_native_dates_and_list_dates_serialize_without_mutation():
    data = CommentedMap(
        dates=[date(2026, 10, 7), datetime.fromisoformat("2026-10-07T00:00:00+09:00")]
    )
    data["dates"].append("2026-10-07T00:00:00.123456789Z")
    text = serialize(data, "# Note")
    result, _ = split_note(text)
    assert result["dates"] == [
        "2026-10-07",
        "2026-10-07T00:00:00+09:00",
        "2026-10-07T00:00:00.123456789Z",
    ]
    assert type(data["dates"][0]) is date
    assert isinstance(data["dates"][1], datetime)


def test_other_values_and_default_yaml_constructor_unchanged():
    data = CommentedMap(
        empty=None,
        text="Photo from 2026-10-07",
        url="https://example.com/2026-10-07",
        count=12,
        active=True,
    )
    result, _ = split_note(serialize(data, "# Note"))
    assert result == data
    # A note reader must not change timestamp loading elsewhere in the process.
    assert type(YAML().load("day: 2026-10-07")["day"]) is date
    stream = StringIO()
    YAML().dump(data, stream)
    assert "count: 12" in stream.getvalue()
