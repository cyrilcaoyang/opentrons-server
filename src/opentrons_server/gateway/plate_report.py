"""Per-well plate report from executed plan records: balance readings mapped
onto a 96-well grid, as JSON and as a self-contained interactive HTML page.

Only what the gateway recorded is reported. A reading is attributed to the
most recent well liquid was delivered into — the target of the last
``dispense`` or located ``blow_out`` before it — and its mass is the reading
minus the balance reference at that moment: the stable baseline the last
``platebalance.tare`` observed, else the previous reading in the same plan
(an incremental weighing), else zero (the reference was set before this
plan; flagged). Nothing here infers a density or a target: an implied volume
is computed only when the caller supplies a density.
"""

from __future__ import annotations

import html
import io
import json
import zipfile
import math
import re
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence
from xml.sax.saxutils import escape as _xml_escape

ROWS = "ABCDEFGH"
COLUMNS = list(range(1, 13))
_WELL = re.compile(r"^([A-Ha-h])([1-9]|1[0-2])$")

_DELIVERY_ACTIONS = {"dispense", "blow_out"}


def _well_name(position: Any) -> Optional[str]:
    if not isinstance(position, str):
        return None
    m = _WELL.match(position.strip())
    return f"{m[1].upper()}{int(m[2])}" if m else None


def _location(args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    loc = args.get("location")
    return loc if isinstance(loc, dict) and loc.get("position") else None


def build_plate_report(
    plans: Iterable[Any],
    *,
    labware: Optional[str] = None,
    density_g_per_ml: Optional[float] = None,
) -> Dict[str, Any]:
    """Combine one or more plans (in the order given) into one plate.

    ``plans`` are :class:`~.plans.Plan` objects or their JSON dumps. A well
    weighed more than once keeps every weighing; the latest is the one shown.
    ``labware`` restricts attribution to one labware nickname/slot; by default
    the labware of the first attributed delivery is used, and deliveries to any
    other labware are listed as excluded.
    """
    if density_g_per_ml is not None and not (math.isfinite(density_g_per_ml) and density_g_per_ml > 0):
        raise ValueError("density_g_per_ml must be a positive number")

    plan_summaries: List[Dict[str, Any]] = []
    weighings: List[Dict[str, Any]] = []
    planned: Dict[str, Dict[str, Any]] = {}
    excluded: List[Dict[str, Any]] = []
    chosen = labware

    for raw in plans:
        plan = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else raw
        steps = plan.get("steps") or []
        results = plan.get("results") or []
        plan_id = plan.get("plan_id")
        plan_summaries.append({
            "plan_id": plan_id, "status": plan.get("status"),
            "halt_reason": plan.get("halt_reason"), "steps": len(steps),
            "steps_ok": sum(1 for r in results if r.get("outcome") == "ok"),
        })
        last_target: Optional[Dict[str, Any]] = None
        reference: Optional[float] = None
        reference_kind = "earlier_plan"
        for index, step in enumerate(steps):
            result = results[index] if index < len(results) else {}
            action = step.get("action")
            args = step.get("args") or {}
            outcome = result.get("outcome", "pending")
            loc = _location(args) if action in _DELIVERY_ACTIONS else None
            if loc is not None:
                well = _well_name(loc.get("position"))
                lw = str(loc.get("labware_nickname"))
                if well is not None:
                    if chosen is None:
                        chosen = lw
                    if lw == str(chosen):
                        entry = planned.setdefault(well, {"well": well, "outcomes": []})
                        entry["outcomes"].append({"plan_id": plan_id, "step": index + 1,
                                                  "action": action, "outcome": outcome})
                        if action == "dispense" and outcome == "ok":
                            entry["volume_ul"] = args.get("volume_ul")
                    elif outcome == "ok":
                        excluded.append({"plan_id": plan_id, "step": index + 1,
                                         "labware": lw, "well": well})
                if outcome == "ok":
                    if action == "dispense":
                        last_target = {"labware": lw, "well": well, "volume_ul": args.get("volume_ul")}
                    elif not (last_target and last_target["well"] == well and last_target["labware"] == lw):
                        # A blow-out into a different well than the last dispense
                        # (e.g. a plan that resumes mid-cycle): that well is where
                        # the next reading's liquid went; its volume is unknown.
                        last_target = {"labware": lw, "well": well, "volume_ul": None}
            if outcome != "ok":
                continue
            reading = result.get("reading")
            if action == "platebalance.tare":
                reference = float(reading["value"]) if reading else 0.0
                reference_kind = "tare"
                continue
            if action == "platebalance.zero":
                reference, reference_kind = 0.0, "zero_unconfirmed"
                continue
            if action != "platebalance.read" or not reading:
                continue
            value = float(reading["value"])
            ref = reference if reference is not None else 0.0
            target = last_target
            record = {
                "plan_id": plan_id, "step": index + 1,
                "reading_g": value, "reference_g": ref,
                "reference": reference_kind if reference is not None else "earlier_plan",
                "mass_g": round(value - ref, 6), "stable": bool(reading.get("stable")),
                "observed_at": reading.get("observed_at"),
                "well": target.get("well") if target else None,
                "labware": target.get("labware") if target else None,
                "volume_ul": target.get("volume_ul") if target else None,
            }
            weighings.append(record)
            reference, reference_kind = value, "previous_read"
            last_target = None  # one reading per delivery; a second read is unattributed

    wells: Dict[str, Dict[str, Any]] = {}
    unattributed: List[Dict[str, Any]] = []
    for w in weighings:
        if w["well"] is None or (chosen is not None and w["labware"] != str(chosen)):
            unattributed.append(w)
            continue
        cell = wells.setdefault(w["well"], {"well": w["well"], "weighings": []})
        cell["weighings"].append(w)

    for name, entry in planned.items():
        cell = wells.setdefault(name, {"well": name, "weighings": []})
        cell["volume_ul"] = entry.get("volume_ul", cell.get("volume_ul"))
        cell["steps"] = entry["outcomes"]

    for cell in wells.values():
        latest = cell["weighings"][-1] if cell["weighings"] else None
        cell["mass_g"] = latest["mass_g"] if latest else None
        cell["stable"] = latest["stable"] if latest else None
        cell["observed_at"] = latest["observed_at"] if latest else None
        cell["reference"] = latest["reference"] if latest else None
        if cell.get("volume_ul") is None and latest:
            cell["volume_ul"] = latest.get("volume_ul")
        cell["repeat_weighings"] = max(0, len(cell["weighings"]) - 1)
        outcomes = [s["outcome"] for s in cell.get("steps", [])]
        cell["status"] = ("weighed" if latest else
                          "failed" if "failed" in outcomes else
                          "not_run" if outcomes and all(o in {"skipped", "pending"} for o in outcomes) else
                          "dispensed_unweighed" if "ok" in outcomes else "not_run")
        if density_g_per_ml is not None and cell["mass_g"] is not None:
            cell["implied_volume_ul"] = round(cell["mass_g"] / density_g_per_ml * 1000, 3)

    masses = [c["mass_g"] for c in wells.values() if c["mass_g"] is not None]
    stats: Dict[str, Any] = {"n": len(masses)}
    if masses:
        mean = statistics.fmean(masses)
        stats.update(mean_g=round(mean, 6), median_g=round(statistics.median(masses), 6),
                     min_g=min(masses), max_g=max(masses))
        if len(masses) > 1:
            sd = statistics.stdev(masses)
            stats.update(sd_g=round(sd, 6), cv_pct=round(sd / mean * 100, 3) if mean else None)
        for c in wells.values():
            if c["mass_g"] is not None and mean:
                c["deviation_pct"] = round((c["mass_g"] - mean) / mean * 100, 3)
    vols = {c.get("volume_ul") for c in wells.values() if c.get("volume_ul") is not None}
    if len(vols) == 1 and density_g_per_ml is not None and masses:
        nominal = vols.pop()
        stats["nominal_volume_ul"] = nominal
        stats["mean_implied_volume_ul"] = round(stats["mean_g"] / density_g_per_ml * 1000, 3)

    return {
        "labware": chosen,
        "grid": {"rows": list(ROWS), "columns": COLUMNS},
        "density_g_per_ml": density_g_per_ml,
        "plans": plan_summaries,
        "stats": stats,
        "wells": dict(sorted(wells.items(), key=lambda kv: (kv[0][0], int(kv[0][1:])))),
        "unattributed_readings": unattributed,
        "excluded_deliveries": excluded,
        "attribution": ("reading minus the balance reference, attributed to the last dispense "
                        "or blow_out target before it; latest weighing per well is shown"),
    }


def summarize_for_agent(report: Dict[str, Any]) -> Dict[str, Any]:
    """Compact form for the assistant: stats, per-well mass, and what is missing."""
    return {
        "labware": report["labware"],
        "plans": report["plans"],
        "stats": report["stats"],
        "density_g_per_ml": report["density_g_per_ml"],
        "wells": {name: {k: c.get(k) for k in ("mass_g", "deviation_pct", "implied_volume_ul",
                                                "volume_ul", "stable", "status", "repeat_weighings")
                         if c.get(k) is not None}
                  for name, c in report["wells"].items()},
        "unattributed_readings": len(report["unattributed_readings"]),
        "excluded_deliveries": len(report["excluded_deliveries"]),
        "interactive_report": ("The operator opens the interactive heatmap from the plan card's Plate report "
                               "button and downloads the Excel workbook from its Spreadsheet button."),
    }


def _col(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _cell(ref: str, value: Any, *, style: int = 0) -> str:
    st = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{st}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"{st}><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)) and math.isfinite(value):
        return f'<c r="{ref}"{st}><v>{value!r}</v></c>'
    text = _xml_escape(str(value))
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet(rows: Sequence[Sequence[Any]], *, header_rows: int = 1, header_cols: int = 0,
           heatmap: Optional[str] = None, diverging: bool = False, widths: Sequence[float] = ()) -> str:
    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(_cell(f"{_col(c)}{r}", v, style=1 if (r <= header_rows or c <= header_cols) else 0)
                        for c, v in enumerate(row, start=1))
        body.append(f'<row r="{r}">{cells}</row>')
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths, start=1))
    cf = ""
    if heatmap:
        scale = ('<cfvo type="min"/><cfvo type="num" val="0"/><cfvo type="max"/>'
                 '<color rgb="FF2563EB"/><color rgb="FFF1F5F9"/><color rgb="FFDC2626"/>') if diverging else (
                 '<cfvo type="min"/><cfvo type="max"/><color rgb="FFEDE9FE"/><color rgb="FF6D28D9"/>')
        cf = (f'<conditionalFormatting sqref="{heatmap}"><cfRule type="colorScale" priority="1">'
              f'<colorScale>{scale}</colorScale></cfRule></conditionalFormatting>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + (f"<cols>{cols}</cols>" if cols else "")
            + f'<sheetData>{"".join(body)}</sheetData>{cf}</worksheet>')


def render_plate_report_xlsx(report: Dict[str, Any]) -> bytes:
    """The report as an Excel workbook, standard library only.

    Sheets: Summary, Mass (plate grid, colour-scale heatmap), Deviation (plate
    grid, diverging), Wells (one row per well), Weighings (every reading, with
    its reference). Opens in Excel, LibreOffice and Google Sheets.
    """
    wells, stats = report["wells"], report["stats"]
    rows_l, cols_n = report["grid"]["rows"], report["grid"]["columns"]

    def grid(key: str) -> List[List[Any]]:
        out: List[List[Any]] = [[""] + list(cols_n)]
        for r in rows_l:
            out.append([r] + [wells.get(f"{r}{c}", {}).get(key) for c in cols_n])
        return out

    grid_ref = f"B2:{_col(len(cols_n) + 1)}{len(rows_l) + 1}"
    summary: List[List[Any]] = [["Plate report", ""],
                                ["Labware", report["labware"]],
                                ["Density (g/mL)", report["density_g_per_ml"] if report["density_g_per_ml"] is not None else "not given"],
                                ["", ""], ["Statistic", "Value"]]
    for key, label in (("n", "Wells weighed"), ("mean_g", "Mean (g)"), ("median_g", "Median (g)"),
                       ("sd_g", "SD (g)"), ("cv_pct", "CV (%)"), ("min_g", "Min (g)"), ("max_g", "Max (g)"),
                       ("nominal_volume_ul", "Nominal volume (µL)"), ("mean_implied_volume_ul", "Mean implied volume (µL)")):
        if stats.get(key) is not None:
            summary.append([label, stats[key]])
    summary += [["", ""], ["Plan", "Status"]]
    for plan in report["plans"]:
        summary.append([plan["plan_id"], f'{plan["status"]}, {plan["steps_ok"]}/{plan["steps"]} steps ok'
                        + (f' — {plan["halt_reason"]}' if plan.get("halt_reason") else "")])
    summary += [["", ""], ["Attribution", report["attribution"]],
                ["Unattributed readings", len(report["unattributed_readings"])],
                ["Deliveries to other labware", len(report["excluded_deliveries"])]]

    well_cols = ["well", "status", "mass_g", "deviation_pct", "implied_volume_ul", "volume_ul",
                 "stable", "reference", "repeat_weighings", "observed_at"]
    well_rows: List[List[Any]] = [well_cols] + [[c.get(k) for k in well_cols] for c in wells.values()]
    log_cols = ["plan_id", "step", "well", "labware", "reading_g", "reference_g", "reference",
                "mass_g", "stable", "volume_ul", "observed_at"]
    log_rows: List[List[Any]] = [log_cols]
    for c in wells.values():
        log_rows += [[w.get(k) for k in log_cols] for w in c["weighings"]]
    log_rows += [[w.get(k) for k in log_cols] for w in report["unattributed_readings"]]

    sheets = [
        ("Summary", _sheet(summary, header_rows=1, header_cols=1, widths=(28, 60))),
        ("Mass", _sheet(grid("mass_g"), header_cols=1, heatmap=grid_ref, widths=[5] + [9] * len(cols_n))),
        ("Deviation", _sheet(grid("deviation_pct"), header_cols=1, heatmap=grid_ref, diverging=True,
                             widths=[5] + [9] * len(cols_n))),
        ("Wells", _sheet(well_rows, widths=(7, 20, 10, 12, 16, 11, 8, 14, 16, 30))),
        ("Weighings", _sheet(log_rows, widths=(20, 6, 6, 8, 10, 11, 14, 10, 8, 10, 30))),
    ]
    ns = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + "</Types>")
        z.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships {ns}>'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   "</Relationships>")
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, (n, _x) in enumerate(sheets, start=1))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships {ns}>'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        # Style 0: default. Style 1: bold header.
        z.writestr("xl/styles.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
                   '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                   "</styleSheet>")
        for i, (_n, xml) in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
    return buf.getvalue()


def render_plate_report_html(report: Dict[str, Any], *, title: str = "Plate report") -> str:
    """Self-contained page: heatmap with metric switch, hover/pin details,
    stats, a sortable well table and CSV download. No external requests."""
    data = json.dumps(report, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    safe_title = html.escape(title)
    return _TEMPLATE.replace("__TITLE__", safe_title).replace("__DATA__", data)


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f8fafc;--card:#fff;--ink:#0f172a;--sub:#64748b;--line:#e2e8f0;--empty:#f1f5f9;--accent:#7c3aed}
@media (prefers-color-scheme:dark){:root{--bg:#0b1120;--card:#111827;--ink:#e2e8f0;--sub:#94a3b8;--line:#1f2937;--empty:#1e293b;--accent:#a78bfa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:18px;margin:0 0 2px}.sub{color:var(--sub);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-top:12px}
.row{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.stat{border:1px solid var(--line);border-radius:8px;padding:8px}.stat b{display:block;font-size:16px;font-variant-numeric:tabular-nums}.stat span{color:var(--sub);font-size:11px}
select,button,.btn{font:inherit;text-decoration:none;padding:4px 8px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.plate{overflow-x:auto}table.grid{border-collapse:separate;border-spacing:4px;margin:0 auto}
.grid th{color:var(--sub);font-weight:500;font-size:11px;width:44px}
.grid td{width:52px;height:40px;border-radius:50%;text-align:center;font-size:10px;font-variant-numeric:tabular-nums;cursor:pointer;border:2px solid transparent;transition:transform .08s}
.grid td:hover{transform:scale(1.08)}.grid td.pin{border-color:var(--accent)}
.grid td.none{background:var(--empty);color:var(--sub)}.grid td.failed{outline:2px dashed #e11d48;outline-offset:-4px}
.legend{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--sub)}.bar{width:220px;height:10px;border-radius:5px}
#tip{position:fixed;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 6px 20px rgba(0,0,0,.15);display:none;max-width:260px;z-index:5}
#detail{font-size:13px}#detail code{font-size:12px}
table.list{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
.list th,.list td{padding:5px 6px;border-bottom:1px solid var(--line);text-align:right}.list th:first-child,.list td:first-child{text-align:left}
.list th{cursor:pointer;color:var(--sub);font-weight:500}.warn{color:#b45309}
</style></head><body><main>
<h1>__TITLE__</h1><div class="sub" id="meta"></div>
<div class="card stats" id="stats"></div>
<div class="card">
 <div class="row" style="justify-content:space-between">
  <div class="row"><label>Heatmap <select id="metric"></select></label><button id="csv">Download CSV</button><a id="xlsx" class="btn">Download spreadsheet</a></div>
  <div class="legend"><span id="lo"></span><div class="bar" id="bar"></div><span id="hi"></span></div>
 </div>
 <div class="plate"><table class="grid" id="grid" aria-label="96-well plate heatmap"></table></div>
 <div class="sub">Hover a well for its values; click to pin details. Dashed outline: a step for that well failed. Grey: no weighing recorded.</div>
</div>
<div class="card" id="detail">Click a well to see every weighing recorded for it.</div>
<div class="card"><table class="list" id="list"></table></div>
<div class="card sub" id="notes"></div>
</main><div id="tip"></div>
<script>
const R=__DATA__;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const W=R.wells,rows=R.grid.rows,cols=R.grid.columns,S=R.stats;
const fmt=(v,d)=>v==null?"—":Number(v).toFixed(d);
const METRICS=[
 {k:"mass_g",label:"Mass (g)",d:4,div:false},
 {k:"deviation_pct",label:"Deviation from mean (%)",d:2,div:true},
 {k:"volume_ul",label:"Dispensed volume (µL)",d:1,div:false}];
if(R.density_g_per_ml)METRICS.splice(1,0,{k:"implied_volume_ul",label:"Implied volume (µL) at "+R.density_g_per_ml+" g/mL",d:2,div:false});
const sel=document.getElementById("metric");METRICS.forEach((m,i)=>{const o=document.createElement("option");o.value=i;o.textContent=m.label;sel.appendChild(o)});
function seq(t){t=Math.max(0,Math.min(1,t));const a=[[237,233,254],[167,139,250],[109,40,217],[59,7,100]];const s=t*3,i=Math.min(2,Math.floor(s)),f=s-i;return a[i].map((c,j)=>Math.round(c+(a[i+1][j]-c)*f))}
function dvg(t){t=Math.max(-1,Math.min(1,t));const n=[37,99,235],z=[241,245,249],p=[220,38,38];const e=t<0?n:p,f=Math.abs(t);return z.map((c,j)=>Math.round(c+(e[j]-c)*f))}
const css=c=>`rgb(${c[0]},${c[1]},${c[2]})`,lum=c=>(0.299*c[0]+0.587*c[1]+0.114*c[2])/255;
let pinned=null;
function draw(){
 const m=METRICS[+sel.value||0],vals=Object.values(W).map(c=>c[m.k]).filter(v=>v!=null);
 let lo=Math.min(...vals),hi=Math.max(...vals);if(!vals.length){lo=0;hi=1}
 const span=m.div?Math.max(Math.abs(lo),Math.abs(hi))||1:(hi-lo)||1;
 const color=v=>m.div?dvg(v/span):seq((v-lo)/span);
 const g=document.getElementById("grid");g.innerHTML="";
 const h=g.insertRow();h.appendChild(document.createElement("th"));cols.forEach(c=>{const th=document.createElement("th");th.textContent=c;h.appendChild(th)});
 rows.forEach(r=>{const tr=g.insertRow();const th=document.createElement("th");th.textContent=r;tr.appendChild(th);
  cols.forEach(c=>{const name=r+c,cell=W[name],td=tr.insertCell();td.dataset.well=name;
   const v=cell?cell[m.k]:null;
   if(v==null){td.className="none";td.textContent=cell?"·":""}else{const col=color(v);td.style.background=css(col);td.style.color=lum(col)<.55?"#fff":"#0f172a";td.textContent=fmt(v,m.d>2?3:m.d)}
   if(cell&&cell.status==="failed")td.classList.add("failed");if(name===pinned)td.classList.add("pin");
   td.onmousemove=e=>tip(e,name);td.onmouseleave=()=>tipEl.style.display="none";td.onclick=()=>{pinned=pinned===name?null:name;detail(pinned);draw()}})});
 document.getElementById("lo").textContent=m.div?"−"+fmt(span,m.d):fmt(lo,m.d);document.getElementById("hi").textContent=m.div?"+"+fmt(span,m.d):fmt(hi,m.d);
 document.getElementById("bar").style.background=m.div?`linear-gradient(90deg,${css(dvg(-1))},${css(dvg(0))},${css(dvg(1))})`:`linear-gradient(90deg,${css(seq(0))},${css(seq(.33))},${css(seq(.66))},${css(seq(1))})`;
}
const tipEl=document.getElementById("tip");
function tip(e,name){const c=W[name];tipEl.innerHTML=`<b>${name}</b>`+(c?`<br>mass ${fmt(c.mass_g,4)} g${c.deviation_pct!=null?` (${c.deviation_pct>0?"+":""}${fmt(c.deviation_pct,2)}%)`:""}${c.implied_volume_ul!=null?`<br>implied ${fmt(c.implied_volume_ul,2)} µL`:""}<br>dispensed ${fmt(c.volume_ul,1)} µL<br>${c.status.replace(/_/g," ")}${c.stable===false?' · <span class="warn">unstable</span>':""}${c.repeat_weighings?` · weighed ${c.repeat_weighings+1}×`:""}`:"<br>not in this run");
 tipEl.style.display="block";tipEl.style.left=Math.min(e.clientX+14,innerWidth-270)+"px";tipEl.style.top=(e.clientY+14)+"px"}
function detail(name){const el=document.getElementById("detail");if(!name){el.textContent="Click a well to see every weighing recorded for it.";return}
 const c=W[name];if(!c){el.innerHTML=`<b>${name}</b>: not in this run.`;return}
 const rowsH=c.weighings.map(w=>`<tr><td>${esc(w.plan_id)} step ${w.step}</td><td>${fmt(w.reading_g,4)}</td><td>${fmt(w.reference_g,4)} (${w.reference.replace(/_/g," ")})</td><td>${fmt(w.mass_g,4)}</td><td>${w.stable?"stable":"unstable"}</td><td>${w.observed_at||""}</td></tr>`).join("");
 el.innerHTML=`<b>${name}</b> · ${c.status.replace(/_/g," ")}`+(rowsH?`<table class="list"><tr><th>record</th><th>reading g</th><th>reference g</th><th>mass g</th><th></th><th>observed</th></tr>${rowsH}</table>`:"")+
 ((c.steps||[]).filter(s=>s.outcome!=="ok").map(s=>`<div class="warn">${esc(s.plan_id)} step ${s.step} ${esc(s.action)}: ${esc(s.outcome)}</div>`).join(""))}
let sortK="well",sortD=1;
function list(){const t=document.getElementById("list");const keys=[["well","Well"],["mass_g","Mass g"],["deviation_pct","Dev %"],["implied_volume_ul","Implied µL"],["volume_ul","Dispensed µL"],["status","Status"]].filter(k=>k[0]!=="implied_volume_ul"||R.density_g_per_ml);
 const order=w=>[w[0],parseInt(w.slice(1))];
 const items=Object.values(W).sort((a,b)=>{if(sortK==="well"){const x=order(a.well),y=order(b.well);return sortD*(x[0]<y[0]?-1:x[0]>y[0]?1:x[1]-y[1])}const x=a[sortK],y=b[sortK];return sortD*((x==null)-(y==null)||(x<y?-1:x>y?1:0))});
 t.innerHTML="<tr>"+keys.map(k=>`<th data-k="${k[0]}">${k[1]}${sortK===k[0]?(sortD>0?" ▲":" ▼"):""}</th>`).join("")+"</tr>"+items.map(c=>"<tr>"+keys.map(k=>`<td>${k[0]==="well"?c.well:k[0]==="status"?c.status.replace(/_/g," "):fmt(c[k[0]],k[0]==="mass_g"?4:2)}</td>`).join("")+"</tr>").join("");
 t.querySelectorAll("th").forEach(th=>th.onclick=()=>{const k=th.dataset.k;sortD=sortK===k?-sortD:1;sortK=k;list()})}
function stats(){const it=[["Wells weighed",S.n],["Mean",S.mean_g!=null?fmt(S.mean_g,4)+" g":"—"],["SD",S.sd_g!=null?fmt(S.sd_g,4)+" g":"—"],["CV",S.cv_pct!=null?fmt(S.cv_pct,2)+" %":"—"],["Min",S.min_g!=null?fmt(S.min_g,4)+" g":"—"],["Max",S.max_g!=null?fmt(S.max_g,4)+" g":"—"]];
 if(S.mean_implied_volume_ul!=null)it.push(["Mean implied",fmt(S.mean_implied_volume_ul,2)+" µL vs "+S.nominal_volume_ul]);
 document.getElementById("stats").innerHTML=it.map(([a,b])=>`<div class="stat"><span>${a}</span><b>${b}</b></div>`).join("")}
document.getElementById("csv").onclick=()=>{const h=["well","status","mass_g","deviation_pct","implied_volume_ul","volume_ul","stable","weighings","observed_at"];
 const lines=[h.join(",")].concat(Object.values(W).map(c=>h.map(k=>k==="weighings"?c.weighings.length:(c[k]??"")).join(",")));
 const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([lines.join("\n")],{type:"text/csv"}));a.download="plate-report.csv";a.click()};
document.getElementById("meta").innerHTML=`Labware ${esc(R.labware)} · `+R.plans.map(p=>`${esc(p.plan_id)} (${esc(p.status)}, ${p.steps_ok}/${p.steps} steps)`).join(" + ");
document.getElementById("notes").innerHTML=esc(R.attribution)+"."+(R.unattributed_readings.length?` <span class="warn">${R.unattributed_readings.length} reading(s) could not be attributed to a well.</span>`:"")+(R.excluded_deliveries.length?` ${R.excluded_deliveries.length} delivery(ies) to other labware excluded.`:"")+(R.density_g_per_ml?"":" No density given, so no implied volume is shown.");
// Served by the gateway: the workbook sits beside this page with the same query.
// Saved to disk, the page has no server to ask, so the link hides itself.
const xl=document.getElementById("xlsx");if(location.protocol.startsWith("http")){xl.href="plate-report.xlsx"+location.search}else{xl.style.display="none"}
sel.onchange=draw;stats();draw();list();
</script></body></html>
"""
