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
import json
import math
import re
import statistics
from typing import Any, Dict, Iterable, List, Optional

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
        "interactive_report": "The operator opens the interactive heatmap from the plan card's Plate report button.",
    }


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
select,button{font:inherit;padding:4px 8px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
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
  <div class="row"><label>Heatmap <select id="metric"></select></label><button id="csv">Download CSV</button></div>
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
sel.onchange=draw;stats();draw();list();
</script></body></html>
"""
