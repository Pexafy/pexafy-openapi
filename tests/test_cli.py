from __future__ import annotations

import json

import pytest

from conftest import make_spec
from pexafy_openapi.cli import main


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def test_diff_exits_one_when_something_breaks(tmp_path, capsys):
    after = make_spec()
    after["components"]["schemas"]["Thing"]["properties"]["kind"]["enum"].append("gamma")
    code = main([
        "diff", _write(tmp_path, "a.json", make_spec()), _write(tmp_path, "b.json", after)
    ])
    assert code == 1
    assert "enum.value.added" in capsys.readouterr().out


def test_diff_exits_zero_on_an_additive_release(tmp_path, capsys):
    after = make_spec()
    after["components"]["schemas"]["Thing"]["properties"]["extra"] = {"type": "string"}
    code = main([
        "diff", _write(tmp_path, "a.json", make_spec()), _write(tmp_path, "b.json", after)
    ])
    assert code == 0
    assert "property.added.optional" in capsys.readouterr().out


def test_a_file_that_will_not_load_exits_two_not_one(tmp_path, capsys):
    """CI has to be able to tell 'the API changed' from 'the check is broken'."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert main(["lint", str(bad)]) == 2
    assert "pexafy-openapi" in capsys.readouterr().err


def test_check_passes_when_a_waiver_covers_the_break(tmp_path, capsys):
    after = make_spec()
    after["components"]["schemas"]["Thing"]["properties"]["kind"]["enum"].append("gamma")
    waivers = tmp_path / "w.json"
    waivers.write_text(json.dumps({
        "waivers": [{
            "code": "enum.value.added",
            "location": "/components/schemas/*",
            "reason": "gamma was announced two releases ahead",
            "expires": "2099-01-01",
        }]
    }))
    code = main([
        "check", _write(tmp_path, "b.json", after),
        "--baseline", _write(tmp_path, "a.json", make_spec()),
        "--waivers", str(waivers),
    ])
    assert code == 0
    assert "waived" in capsys.readouterr().out


def test_check_fails_without_the_waiver(tmp_path):
    after = make_spec()
    after["components"]["schemas"]["Thing"]["properties"]["kind"]["enum"].append("gamma")
    code = main([
        "check", _write(tmp_path, "b.json", after),
        "--baseline", _write(tmp_path, "a.json", make_spec()),
    ])
    assert code == 1


def test_markdown_output_leads_with_the_breaking_section(tmp_path, capsys):
    after = make_spec()
    after["components"]["schemas"]["Thing"]["properties"]["kind"]["enum"].append("gamma")
    after["components"]["schemas"]["Thing"]["properties"]["extra"] = {"type": "string"}
    main([
        "diff", _write(tmp_path, "a.json", make_spec()), _write(tmp_path, "b.json", after),
        "--format", "markdown",
    ])
    out = capsys.readouterr().out
    assert out.index("#### breaking") < out.index("#### additive")


@pytest.mark.parametrize("command", ["lint", "reach", "stats"])
def test_json_output_parses(tmp_path, capsys, command):
    main([command, _write(tmp_path, "a.json", make_spec()), "--format", "json"])
    assert json.loads(capsys.readouterr().out)


def test_the_rule_table_prints_both_columns(capsys):
    assert main(["rules"]) == 0
    out = capsys.readouterr().out
    assert "enum.value.added" in out
    assert out.count("breaking") > 10


def test_the_rule_table_is_machine_readable(capsys):
    main(["rules", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    entry = next(r for r in payload["schema_rules"] if r["code"] == "enum.value.added")
    assert entry["request"] == "additive"
    assert entry["response"] == "breaking"


def test_lint_can_be_narrowed_to_one_rule(tmp_path, capsys):
    spec = make_spec()
    spec["paths"]["/thing/{thing_id}"]["get"].pop("operationId")
    assert main(["lint", _write(tmp_path, "a.json", spec), "--only", "operation-id-unique"]) == 0
    assert main(["lint", _write(tmp_path, "a.json", spec), "--only", "operation-id-present"]) == 1
    capsys.readouterr()


def test_stats_reports_the_shape_of_the_published_description(spec_dir, capsys):
    assert main(["stats", str(spec_dir / "v1.json"), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operations"] > 10
    assert payload["version"] == "1.3.0"
