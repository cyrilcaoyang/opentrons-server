"""Plan proposals by pattern (docs/PLAN_REPEAT_DESIGN.md, 2026-10-05).

The expansion is what gets approved and run, so these pin its semantics:
typed substitution, refused unknowns, explicit order, overrides that merge
instead of clobber, single-channel only, and a summary derived from the
expanded steps rather than from the proposer.
"""

import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.plan_patterns import (
    MAX_EXPANDED_STEPS, ForEachWell, PatternError, WellGrid, expand, resolve_wells, summarize,
)

GRID_96 = WellGrid(rows=8, columns=12)


def _grid(name):
    return GRID_96 if name in ("2", "plate") else None


def _spec(**over):
    base = {
        "labware_nickname": "2",
        "wells": "A1:B2",
        "steps": [
            {"id": "asp", "action": "aspirate", "args": {"pipette": "left", "volume_ul": 100,
             "location": {"labware_nickname": "1", "position": "A1"}}},
            {"id": "disp", "action": "dispense", "args": {"pipette": "left", "volume_ul": 100,
             "location": {"labware_nickname": "2", "position": "{well}", "top": -2}}},
            {"action": "comment", "args": {"message": "well {well} is row {row} col {column} #{index}"}},
        ],
    }
    base.update(over)
    return ForEachWell(**base)


def test_a_range_walks_in_column_order_and_substitutes_typed_values():
    steps, pattern = expand(_spec(), grid_for=_grid, channels_for=lambda p: 1)
    assert pattern["wells"] == ["A1", "B1", "A2", "B2"]  # OT-2 traversal
    assert len(steps) == 12
    first_comment = steps[2]["args"]["message"]
    assert first_comment == "well A1 is row A col 1 #1"
    assert steps[1]["args"]["location"] == {"labware_nickname": "2", "position": "A1", "top": -2}
    # A whole-token placeholder keeps its type.
    typed, _ = expand(_spec(steps=[{"action": "comment", "args": {"n": "{index}", "c": "{column}"}}]),
                      grid_for=_grid, channels_for=lambda p: 1)
    assert typed[0]["args"] == {"n": 1, "c": 1} and isinstance(typed[0]["args"]["n"], int)


def test_row_order_and_explicit_lists_keep_their_order():
    assert resolve_wells(_spec(order="row"), GRID_96) == ["A1", "A2", "B1", "B2"]
    assert resolve_wells(_spec(wells=["H12", "A1", "C3"]), GRID_96) == ["H12", "A1", "C3"]


@pytest.mark.parametrize("wells, message", [
    ("A1:H13", "exceeds"), ("B2:A1", "backwards"), (["A1", "A1"], "listed twice"),
    (["I1"], "does not exist"),
])
def test_bad_wells_are_refused_against_the_labware_grid(wells, message):
    with pytest.raises(PatternError, match=message):
        resolve_wells(_spec(wells=wells), GRID_96)


def test_unknown_labware_unknown_placeholder_and_multichannel_are_refused():
    with pytest.raises(PatternError, match="not declared or loaded"):
        expand(_spec(labware_nickname="9"), grid_for=_grid, channels_for=lambda p: 1)
    with pytest.raises(PatternError, match="unknown placeholder {sample}"):
        expand(_spec(steps=[{"action": "comment", "args": {"message": "{sample}"}}]),
               grid_for=_grid, channels_for=lambda p: 1)
    with pytest.raises(PatternError, match="multi-channel"):
        expand(_spec(), grid_for=_grid, channels_for=lambda p: 8)


def test_overrides_deep_merge_and_are_checked():
    spec = _spec(overrides={"B2": {"disp": {"location": {"position": "H12"}, "volume_ul": 50}}})
    steps, _ = expand(spec, grid_for=_grid, channels_for=lambda p: 1)
    b2_dispense = steps[3 * 3 + 1]["args"]
    # The position changed; the location's offset survived; the volume changed.
    assert b2_dispense["location"] == {"labware_nickname": "2", "position": "H12", "top": -2}
    assert b2_dispense["volume_ul"] == 50
    assert steps[1]["args"]["volume_ul"] == 100  # other wells untouched
    with pytest.raises(PatternError, match="not among the wells"):
        expand(_spec(overrides={"H1": {"disp": {}}}), grid_for=_grid, channels_for=lambda p: 1)
    with pytest.raises(PatternError, match="no template step has"):
        expand(_spec(overrides={"A1": {"nope": {}}}), grid_for=_grid, channels_for=lambda p: 1)


def test_the_expansion_is_capped():
    big = _spec(wells="A1:H12", steps=[{"action": "comment", "args": {"message": "x"}}] * 21)
    with pytest.raises(PatternError, match=f"at most {MAX_EXPANDED_STEPS}"):
        expand(big, grid_for=_grid, channels_for=lambda p: 1)


def test_the_summary_is_derived_from_the_expanded_steps():
    spec = _spec(wells="A1:H12", overrides={"H12": {"asp": {"volume_ul": 50}}})
    steps, pattern = expand(spec, grid_for=_grid, channels_for=lambda p: 1,
                            prelude=[{"action": "pick_up_tip", "args": {"pipette": "left"}}],
                            epilogue=[{"action": "drop_tip", "args": {"pipette": "left"}}])
    summary = summarize(pattern, steps)
    assert summary["well_count"] == 96 and summary["order"] == "column"
    assert summary["wells_first"] == ["A1", "B1", "C1", "D1"] and summary["wells_last"] == ["G12", "H12"]
    aspirate = summary["per_well"][0]
    assert aspirate["count"] == 96 and aspirate["total_volume_ul"] == 95 * 100 + 50  # the override counted
    assert summary["per_well"][1]["template_location"] == "2/{well}"
    assert summary["per_well"][1]["first_location"] == "2/A1"
    assert summary["tip_pickups"] == 1 and summary["tip_drops"] == 1  # one tip across all wells, visibly
    assert summary["overrides"] == {"H12": {"asp": {"volume_ul": 50}}}
    assert summary["total_steps"] == 2 + 96 * 3 and summary["pipettes"] == ["left"]


def test_review_fixes_overrides_cannot_swap_in_a_multichannel_head_and_braces_are_strict():
    channels = {"left": 1, "right": 8}
    spec = _spec(overrides={"A1": {"disp": {"pipette": "right"}}})
    with pytest.raises(PatternError, match="multi-channel"):
        expand(spec, grid_for=_grid, channels_for=channels.__getitem__)
    for bad in ("{Well}", "{sample1}", "{{well}}", "well {"):
        with pytest.raises(PatternError, match="placeholder|braces"):
            expand(_spec(steps=[{"action": "comment", "args": {"message": bad}}]),
                   grid_for=_grid, channels_for=lambda p: 1)


def test_a_sparse_custom_labware_checks_exact_wells_and_lists_run_as_listed():
    sparse = WellGrid(rows=2, columns=2, wells=frozenset({"A1", "B2"}))
    assert resolve_wells(_spec(wells=["B2", "A1"]), sparse) == ["B2", "A1"]
    with pytest.raises(PatternError, match="does not exist"):
        resolve_wells(_spec(wells=["A2"]), sparse)
    with pytest.raises(PatternError, match="does not have: B1, A2"):
        resolve_wells(_spec(wells="A1:B2"), sparse)
    # A gigantic range is refused by arithmetic, before anything is built.
    with pytest.raises(PatternError, match="at most"):
        resolve_wells(_spec(wells="A1:A1000000000"), sparse)
    # A range inside the exact well set is fine even past the bounding box.
    sparse_rc = WellGrid(rows=1, columns=2, wells=frozenset({"A1", "B2"}))
    assert resolve_wells(_spec(wells="B2:B2"), sparse_rc) == ["B2"]
    steps, pattern = expand(_spec(wells=["B2", "A1"]), grid_for=_grid, channels_for=lambda p: 1)
    assert summarize(pattern, steps)["order"] == "as listed"


def test_labware_grid_prefers_the_recipe_nickname_and_reads_definition_wells():
    from types import SimpleNamespace

    from opentrons_server.gateway.service import OT2Service

    service = OT2Service.__new__(OT2Service)
    plate = SimpleNamespace(rows=2, columns=2, definition={"wells": {"A1": {}, "B2": {}}})
    other = SimpleNamespace(rows=8, columns=12, definition=None)
    deck = SimpleNamespace(slots={"2": SimpleNamespace(labware=other), "3": SimpleNamespace(labware=plate)})
    service._build_deck_state = lambda: deck
    service._nickname_to_slot = lambda: {"2": "3"}  # a recipe nickname "2" lives in slot 3
    grid = service.labware_grid("2")
    assert (grid.rows, grid.columns, grid.wells) == (2, 2, frozenset({"A1", "B2"}))
    service._nickname_to_slot = lambda: {}
    assert service.labware_grid("2").wells is None and service.labware_grid("2").rows == 8
    assert service.labware_grid("9") is None
    # A loaded custom labware (no definition from the run engine) takes its
    # exact wells from the matching declaration.
    loaded = SimpleNamespace(rows=2, columns=2, definition=None, load_name="custom_sparse")
    declared = SimpleNamespace(rows=2, columns=2, definition={"wells": {"A1": {}, "B2": {}}},
                               load_name="custom_sparse")
    service._build_deck_state = lambda: SimpleNamespace(
        slots={"4": SimpleNamespace(labware=loaded, declared=declared)})
    assert service.labware_grid("4").wells == frozenset({"A1", "B2"})
    # ...and its dimensions too, when the run engine reports none.
    dimless = SimpleNamespace(rows=None, columns=None, definition=None, load_name="custom_sparse")
    service._build_deck_state = lambda: SimpleNamespace(
        slots={"4": SimpleNamespace(labware=dimless, declared=declared)})
    assert (service.labware_grid("4").rows, service.labware_grid("4").wells) == (2, frozenset({"A1", "B2"}))
    other_decl = SimpleNamespace(rows=2, columns=2, definition={"wells": {"A1": {}}}, load_name="different")
    service._build_deck_state = lambda: SimpleNamespace(
        slots={"4": SimpleNamespace(labware=loaded, declared=other_decl)})
    assert service.labware_grid("4").wells is None  # a different declaration is not borrowed


# ── through the API and the assistant ──────────────────────────────────


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("OT2_DECK_STATE_PATH", str(tmp_path / "deck.json"))
    app = create_app(dry_run=True, enforce_claims=False, ui=False)
    service = app.state.service
    monkeypatch.setattr(service, "labware_grid", lambda ref: GRID_96 if ref == "2" else None)
    return TestClient(app), app


def _pattern_body(**over):
    body = {
        "created_by": "agent",
        "prelude": [{"action": "lights.set", "args": {"on": True}}],
        "for_each_well": {
            "labware_nickname": "2", "wells": "A1:B1",
            "steps": [{"action": "comment", "args": {"message": "well {well}"}},
                      {"action": "platebalance.read", "args": {}}],
        },
        "epilogue": [{"action": "lights.set", "args": {"on": False}}],
    }
    body.update(over)
    return body


def test_post_plans_expands_a_pattern_and_shows_its_summary(gateway):
    client, _app = gateway
    r = client.post("/plans", json=_pattern_body())
    assert r.status_code == 201, r.text
    plan = r.json()
    assert [s["action"] for s in plan["steps"]] == [
        "lights.set", "comment", "platebalance.read", "comment", "platebalance.read", "lights.set"]
    assert plan["steps"][1]["args"]["message"] == "well A1"
    assert plan["pattern_summary"]["well_count"] == 2
    assert plan["pattern_summary"]["balance_reads"] == 2
    assert plan["pattern"]["wells"] == ["A1", "B1"]
    # A revision is a hand-edited list: the pattern no longer describes it.
    r2 = client.put(f"/plans/{plan['plan_id']}/steps",
                    json={"steps": [{"action": "lights.set", "args": {"on": True}}]})
    assert r2.status_code == 200 and r2.json()["pattern"] is None
    assert r2.json()["pattern_summary"] is None


def test_post_plans_refuses_mixed_forms_and_bad_patterns(gateway):
    client, _app = gateway
    both = _pattern_body(steps=[{"action": "lights.set", "args": {"on": True}}])
    assert client.post("/plans", json=both).status_code == 422
    assert client.post("/plans", json={"created_by": "agent"}).status_code == 422
    bad = _pattern_body()
    bad["for_each_well"]["steps"] = [{"action": "aspirate", "args": {"pipette": "left", "volume_ul": -1,
                                      "location": {"labware_nickname": "2", "position": "{well}"}}}]
    r = client.post("/plans", json=bad)
    assert r.status_code == 422 and "expanded step 2 (aspirate)" in r.text
    unknown = _pattern_body()
    unknown["for_each_well"]["labware_nickname"] = "7"
    assert "not declared" in client.post("/plans", json=unknown).text


def test_the_assistant_tool_proposes_a_pattern(gateway):
    from opentrons_server.gateway.assistant import Assistant

    _client, app = gateway
    assistant = Assistant(app.state.service, app.state.plans, config=None)
    out = assistant._tools()["propose_plan"]({
        "for_each_well": _pattern_body()["for_each_well"],
        "prelude": _pattern_body()["prelude"],
    })
    assert "error" not in out, out
    assert out["step_count"] == 5 and out["steps"] is None
    assert out["pattern_summary"]["well_count"] == 2
    refused = assistant._tools()["propose_plan"]({"for_each_well": {
        "labware_nickname": "2", "wells": "A1:A1",
        "steps": [{"action": "comment", "args": {"message": "{nope}"}}]}})
    assert "unknown placeholder" in refused["error"]


def test_the_claude_reply_parser_forwards_the_pattern(monkeypatch):
    import json
    from types import SimpleNamespace

    from opentrons_server.gateway import assistant_claude as ac

    def fake_run(*_a, **_k):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": {
            "reply": "ok", "steps": [],
            "for_each_well": {"labware_nickname": "2", "wells": "A1:A2", "steps": []},
            "epilogue": [{"action": "drop_tip", "args": {"pipette": "left"}}],
        }}), stderr="")

    monkeypatch.setattr(ac.subprocess, "run", fake_run)
    out = ac.run_claude_code("claude", "sys", {"messages": []}, 5, None)
    assert out["for_each_well"]["wells"] == "A1:A2" and out["epilogue"][0]["action"] == "drop_tip"
    assert out["steps"] == []


def test_a_draft_names_the_verified_identity_it_was_created_for():
    app = create_app(dry_run=True, enforce_claims=False, ui=False, edge_secret="s3cret")
    client = TestClient(app)
    body = {"steps": [{"action": "lights.set", "args": {"on": True}}],
            "created_by": "assistant (claude-sonnet-5-5) for ada@lab"}
    # Through the edge as ada: the label already names her, nothing is appended.
    r = client.post("/plans", json=body, headers={"X-Edge-Key": "s3cret", "X-Auth-User": "ada@lab"})
    assert r.json()["created_by"] == "assistant (claude-sonnet-5-5) for ada@lab"
    # A label that does not name the verified identity gets it appended.
    r = client.post("/plans", json={**body, "created_by": "agent:hermes"},
                    headers={"X-Edge-Key": "s3cret", "X-Auth-User": "bob@lab"})
    assert r.json()["created_by"] == "agent:hermes (bob@lab)"
    # No identity (open deployment): the label stands as given.
    assert client.post("/plans", json={**body, "created_by": "agent"}).json()["created_by"] == "agent"
