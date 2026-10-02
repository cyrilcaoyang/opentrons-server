"""Plate report: balance readings from plan records mapped onto a 96-well grid.

The fixture is the live Complexation run of 2026-10-02: 32 wells on slot 9,
split across two plans because the first halted at the approval window.
"""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.plans import Plan
from opentrons_server.gateway.plate_report import (
    build_plate_report,
    render_plate_report_html,
    render_plate_report_xlsx,
    summarize_for_agent,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "plans_balance_run_split.json"


def _plans():
    return json.loads(FIXTURE.read_text())


def test_split_run_reads_as_one_plate():
    report = build_plate_report(_plans())
    assert report["labware"] == "9"
    assert report["stats"]["n"] == 31
    assert report["stats"]["cv_pct"] == pytest.approx(2.236, abs=0.01)
    assert report["wells"]["C4"]["mass_g"] == pytest.approx(0.1007)  # 0.1008 minus the 0.0001 tare baseline
    assert report["wells"]["C4"]["reference"] == "tare"
    assert report["wells"]["E12"]["status"] == "not_run"
    assert report["wells"]["E12"]["mass_g"] is None
    assert report["unattributed_readings"] == []


def test_a_cycle_split_across_plans_is_attributed_and_flagged():
    """F7 was dispensed in the first plan; its blow-out and read ran in the second."""
    f7 = build_plate_report(_plans())["wells"]["F7"]
    assert f7["status"] == "weighed"
    assert f7["mass_g"] == pytest.approx(0.0897)
    assert f7["volume_ul"] == 100
    assert f7["reference"] == "earlier_plan"


def test_no_density_means_no_implied_volume():
    report = build_plate_report(_plans())
    assert all("implied_volume_ul" not in c for c in report["wells"].values())
    with_density = build_plate_report(_plans(), density_g_per_ml=1.0)
    assert with_density["wells"]["C4"]["implied_volume_ul"] == pytest.approx(100.7)
    assert with_density["stats"]["nominal_volume_ul"] == 100
    with pytest.raises(ValueError):
        build_plate_report(_plans(), density_g_per_ml=0)


def test_incremental_reads_without_a_tare_use_the_previous_reading():
    plan = {"plan_id": "p", "status": "executed", "steps": [
        {"action": "platebalance.tare", "args": {}},
        {"action": "dispense", "args": {"volume_ul": 50, "location": {"labware_nickname": "9", "position": "A1"}}},
        {"action": "platebalance.read", "args": {}},
        {"action": "dispense", "args": {"volume_ul": 50, "location": {"labware_nickname": "9", "position": "A2"}}},
        {"action": "platebalance.read", "args": {}},
        {"action": "platebalance.read", "args": {}},
    ], "results": [
        {"action": "platebalance.tare", "outcome": "ok", "reading": {"value": 0.0, "stable": True}},
        {"action": "dispense", "outcome": "ok"},
        {"action": "platebalance.read", "outcome": "ok", "reading": {"value": 0.05, "stable": True}},
        {"action": "dispense", "outcome": "ok"},
        {"action": "platebalance.read", "outcome": "ok", "reading": {"value": 0.101, "stable": True}},
        {"action": "platebalance.read", "outcome": "ok", "reading": {"value": 0.1012, "stable": False}},
    ]}
    report = build_plate_report([plan])
    assert report["wells"]["A2"]["mass_g"] == pytest.approx(0.051)
    assert report["wells"]["A2"]["reference"] == "previous_read"
    assert len(report["unattributed_readings"]) == 1  # a second read with no new delivery


def test_html_is_self_contained_and_escapes_record_text():
    plans = _plans()
    plans[0]["halt_reason"] = "</script><script>alert(1)</script>"
    page = render_plate_report_html(build_plate_report(plans), title="<b>x</b>")
    assert "<script>alert(1)" not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page
    assert "http://" not in page and "https://" not in page  # no external requests
    assert 'id="grid"' in page and "Download CSV" in page


def test_agent_summary_is_compact_and_points_at_the_button():
    summary = summarize_for_agent(build_plate_report(_plans()))
    assert summary["stats"]["n"] == 31
    assert summary["wells"]["E12"]["status"] == "not_run"
    assert "mass_g" not in summary["wells"]["E12"]
    assert "Plate report" in summary["interactive_report"]


def test_routes_serve_json_and_html_for_stored_plans():
    app = create_app(dry_run=True, auto_reconnect=False, enforce_claims=False)
    plans = [Plan.model_validate(p) for p in _plans()]
    with TestClient(app) as client:
        for plan in plans:
            app.state.plans._plans[plan.plan_id] = plan
        ids = "&".join(f"plan_id={p.plan_id}" for p in plans)
        body = client.get(f"/plans/plate-report?{ids}").json()
        assert body["stats"]["n"] == 31
        page = client.get(f"/plans/plate-report.html?{ids}")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert client.get("/plans/plate-report").status_code == 422
        assert client.get("/plans/plate-report?plan_id=nope").status_code == 404
        assert client.get(f"/plans/plate-report?{ids}&density_g_per_ml=-1").status_code == 422



def test_xlsx_is_a_valid_workbook_with_plate_heatmaps():
    import io
    import zipfile
    from xml.etree import ElementTree as ET

    data = render_plate_report_xlsx(build_plate_report(_plans(), density_g_per_ml=1.0))
    z = zipfile.ZipFile(io.BytesIO(data))
    for name in z.namelist():
        if name.endswith((".xml", ".rels")):
            ET.fromstring(z.read(name))  # every part is well-formed
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    book = ET.fromstring(z.read("xl/workbook.xml"))
    assert [s.get("name") for s in book.find("m:sheets", ns)] == ["Summary", "Mass", "Deviation", "Wells", "Weighings"]
    mass = ET.fromstring(z.read("xl/worksheets/sheet2.xml"))
    cells = {c.get("r"): c for c in mass.iter(f"{{{ns['m']}}}c")}
    assert cells["E4"].find("m:v", ns).text == "0.1007"  # C4: row C is sheet row 4, column 4 is E
    assert cells["M8"].find("m:v", ns) is None             # E12 never ran
    assert mass.find("m:conditionalFormatting", ns).get("sqref") == "B2:M9"
    weighings = ET.fromstring(z.read("xl/worksheets/sheet5.xml"))
    assert len(weighings.findall(".//m:row", ns)) == 1 + 31


def test_xlsx_route_is_an_attachment():
    app = create_app(dry_run=True, auto_reconnect=False, enforce_claims=False)
    plans = [Plan.model_validate(p) for p in _plans()]
    with TestClient(app) as client:
        for plan in plans:
            app.state.plans._plans[plan.plan_id] = plan
        resp = client.get("/plans/plate-report.xlsx?" + "&".join(f"plan_id={p.plan_id}" for p in plans))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert resp.headers["content-disposition"].startswith('attachment; filename="')
        assert resp.content[:2] == b"PK"
