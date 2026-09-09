# OT-2 Deck / Labware State — feature documentation

**Status:** shipped and deployed. Phases 0–2 landed in this repo (commit
`ede0a85`); the dashboard side (tile reading the device deck, `deck.declare`
SkillDef) shipped in `ac-organic-lab`. Both gateways (`ot2_hte` :8020,
`ot2_complexation` :8021) deployed 2026-07-10 and verified live; the old
dashboard stopgap layouts were migrated into the gateways' declared stores.
HTTP-transport deck parity (including idle-persistence across restart) was
hardware-validated 2026-07-14 — see
[`HTTP_DRIVE_VALIDATION.md`](HTTP_DRIVE_VALIDATION.md). Tip-rack registration
was rekeyed onto the deck slot 2026-08-07 (`4a7faf3`) and extended to gateway
boot (`6c46e57`); both gateways redeployed and verified live the same day.

> This document replaces the retired **`DECK_STATE_PLAN.md`** (the phased
> implementation plan, removed 2026-07-19 once its tasks completed; full
> history in git). It keeps the plan's durable final-state material: the
> model, the merge semantics, and the contract with the dashboard and
> workflow repos.

**What it is:** a normalized deck/labware state layer in the gateway
(`gateway/deck.py` + models in `gateway/models.py`) that unifies the
previously-orphaned state stores and publishes `details.snapshot.deck` on
`/status` — the single deck picture the dashboard tile and the full-page OT-2
control interface render.

---

## The model

### Labware kind enum

Derived from the Opentrons `load_name` (which is highly structured:
`<brand>_<count>_<category>_<...>`), never hand-set:

```python
LabwareKind = Literal[
    "96-well", "384-well", "48-well", "24-well", "12-well", "6-well",
    "well_plate",   # a wellPlate whose grid isn't one of the named sizes
    "tiprack", "reservoir", "tuberack", "trash", "adapter", "unknown",
]
```

`classify_labware(load_name, ...)` returns `(kind, rows, columns)`; the
12-count ambiguity (12-reservoir = 1×12 vs 12-wellplate = 3×4) is resolved by
the category token. The dashboard renders generically off `rows`/`columns`;
`kind` only drives label/icon/greying.

### Slot / deck models

```python
class SlotLabware(BaseModel):
    kind: LabwareKind
    load_name: str
    display_name: Optional[str] = None
    is_tiprack: bool = False
    rows: Optional[int] = None
    columns: Optional[int] = None
    # present only for the tracked plate (from PlateStateStore); None otherwise
    plate_id: Optional[str] = None
    wells: Optional[List[WellSample]] = None

class SlotModule(BaseModel):
    module_name: str
    status: Optional[str] = None
    serial_number: Optional[str] = None

SlotState = Literal["empty", "declared", "occupied", "in_use", "mismatch"]
DeckSource = Literal["run", "repl", "declared", "empty"]

class DeckSlot(BaseModel):
    labware: Optional[SlotLabware] = None
    module: Optional[SlotModule] = None
    slot_state: SlotState = "empty"
    source: DeckSource = "empty"
    declared: Optional[SlotLabware] = None        # the declaration on this slot, whatever won
    declared_module: Optional[SlotModule] = None  # ditto, when the declaration is a module

class DeckState(BaseModel):
    source: DeckSource            # highest-precedence source contributing any slot
    slots: Dict[str, DeckSlot]    # "1".."12", always all 12 keys present
    timestamp: datetime
```

## Sources and merge semantics

`build_deck(...)` is a pure function (no hardware, no I/O) merging four
sources with precedence **run > repl > declared > empty**:

- **run** — the robot-server run's loaded labware (the active external run
  probe on SSH; the gateway's own never-played run on the HTTP transport —
  see [`HTTP_TRANSPORT.md`](HTTP_TRANSPORT.md)). TTL-cached; never fetched
  from the `/status` handler.
- **repl** — the SSH REPL snapshot (`state_readers.get_all_states`), present
  only while the gateway owns a REPL session. Empty on the HTTP transport.
- **declared** — operator/recipe intent: the persisted
  `DeckDeclarationStore` (`OT2_DECK_STATE_PATH`) merged with the
  `session_recipe` from `/control/setup`.
- **empty** — the fallback.

**Only `declared` survives a restart.** `run` and `repl` are session-scoped —
the run probe's cache and the REPL snapshot both die with the process — so a
deck that was *only ever observed* comes back `empty` after a service restart
and stays that way until the next protocol loads labware. Declaring is what
makes a layout durable, which matters most on a robot whose deck is set up by
hand rather than by a `/control/setup` recipe. Observed live 2026-08-07:
restarting `ot2_hte` blanked a deck that had only ever been observed, while
`ot2_complexation`'s declared deck came back intact. Note the direction of the
precedence — declaring can never override what a run observes, so declaring a
hand-set deck is safe even while protocols come and go.

### Slot-state decision table

Let `observed = run[s] or repl[s]` (run wins), `declared = declared[s]`,
`busy` = service BUSY or an active run:

| observed | declared | kinds agree? | `slot_state` | `source` |
|---|---|---|---|---|
| — | — | — | `empty` | `empty` |
| — | ✓ | — | `declared` | `declared` |
| ✓ | — | — | `in_use` if busy else `occupied` | run/repl |
| ✓ | ✓ | yes | `in_use` if busy else `occupied` | run/repl |
| ✓ | ✓ | **no** | `mismatch` (`labware`=observed, `declared`=the losing intent) | run/repl |

This declared-vs-observed reconciliation is the contract the workflow repos
asked for: declaring records *intent* (pure metadata, no hardware); the robot
observation wins on conflict, and the disagreement is flagged per slot rather
than silently resolved.

`slot_state` is therefore about the *slot*, not about the declaration: rows 4
and 5 both hold a declaration while saying `occupied`. So the declaration is
reported separately, on **every** declared slot, in `declared` (labware) or
`declared_module` (a sticky module) — not only on the mismatch row. Anything
reconstructing the declared layout must read those two fields. `slot_state`
alone loses every slot the robot has loaded, and because
`POST /control/deck/declare` is a full-layout replace, re-sending a layout built
that way **deletes** those declarations device-side. That was live for the
panel until 2026-08-19: a declared custom plate used by one successful run
became unclearable in the UI and was then wiped — definition included — by the
next unrelated declare, which brought back the `FileNotFoundError` that
auto-loading a custom plate without its definition raises. The
orchestrator-tracked plate is excluded from both fields: it is folded onto its
slot to carry its wells, not as operator intent.

## Store unification (no parallel structures)

- **`PlateStateStore` is the single well-truth store.** `build_deck` reads it
  and attaches `plate_id` + `wells` onto the slot the tracked plate occupies
  (resolved by matching `plate_id` against `session_recipe` nicknames →
  location; an unmatched plate stays "unplaced" and is still emitted in
  `details.loaded_plate`).
- **`details.loaded_plate` is retained verbatim** for back-compat (the
  solubility workflow reads it directly) but is now derived from the same
  store the deck slot reads — deprecated in favour of the deck slots once
  consumers migrate.
- **`last_snapshot`** stays as the internal raw snapshot cache feeding the
  `repl` source (SSH) or the run-derived shape (HTTP — see the snapshot-shape
  section of [`HTTP_SSH_PARITY.md`](HTTP_SSH_PARITY.md)).

### Tip-rack registration follows the deck

`TipStateStore` is keyed by **deck slot**, not by a recipe nickname: a tip rack
carries no sample and no history worth naming, and what an operator points at —
and refills — is "the rack in slot 4" (`4a7faf3`). Registration happens at
three points, all non-destructive (a slot already tracked keeps its used-tip
statuses):

| When | Why |
|---|---|
| `/control/setup` | the recipe places tip racks |
| `/control/deck/declare` | declaring a rack *is* the fact the tracker needs — before this, a declared rack was invisible to the tracker |
| gateway boot (`OT2Service.__init__`) | the persisted deck outlives the process; without it a rack declared in an earlier session came back untracked (`6c46e57`) |

**Registration asserts a *full* rack.** `_fresh_rack` marks every well `new`,
so a count that came from registration alone is an assumption — "a rack
appeared on this slot, presume it is untouched" — not an observation. Three
things corroborate a count, in descending strength: pick/drop events (evidence
the gateway itself recorded), an operator `POST /control/tips/reset` (a human
asserting a physical refill or swap), and nothing at all. Treat a rack with no
history and no reset as unverified, however confident its `96/96` looks; the
device cannot see tips and will never correct itself. Registration is
deliberately non-destructive for the same reason — re-declaring or restarting
must never silently "refill" a rack that has been used.

**Clearing a declaration never touches tip state.** The two stores are
independent, and `TipStateStore.remove_rack` has no caller: undeclaring a slot
only hides its rack from the panel, and re-declaring brings the same used-tip
map back. That is the intended behaviour — declare/undeclare is layout
bookkeeping, and letting it reset a rack would make a full-layout replace
(`POST /control/deck/declare`) silently refill every rack it happened to omit.
Correcting a count is its own explicit act, never a side effect of a layout
edit.

Boot registration reads `_build_deck_state`, which is cache-only — no HTTP, no
REPL — so it cannot block or slow startup. It follows the *declared* deck by
construction, so a robot with no declared layout registers nothing at boot and
falls back to whatever the tip store itself persisted. That asymmetry is worth
knowing: the tip store and the deck can disagree after a restart, with the
store asserting racks the deck no longer shows.

The store and the deck are joined on the slot, which is why the join always
resolves — `labware.nickname` is `null` on every slot until a setup runs, and
keying on it was why a tracked rack could still render as untracked.

### A tip off the rack: `on_pipette` and the mount

A well holds one of four things: a fresh tip (`new`), a tip that has touched a
sample (the sample id), a bare hole (`empty`), or **`on_pipette`** — a hole
whose tip is on a head and may come back to it.

`on_pipette` is written **when the pick is issued**, not when the tip is
eventually thrown away. Before that, a rack went on claiming a tip that was
already riding the head until the run reached a `drop_tip`, with three
consequences: `/status` and the panel over-counted, `next_available` handed out
the *same* well again (sending the head back onto a hole it had just emptied),
and — the reported failure — returning the tip was refused as "would drop onto
a seated tip", so an interrupted run could not put its tip back without an
operator correction first.

The marking happens **before** the motion, and the two failure paths are
deliberately asymmetric:

| the pick | the rack | why |
|---|---|---|
| succeeds | stays `on_pipette` | the tip is on the head |
| fails definitely | rolled back to its prior status | nothing moved; restoring `new` wholesale would erase a returned tip's history, so the *prior* status is what comes back |
| ends unknown (transport loss) | stays `on_pipette`, mount flagged `uncertain` | assuming otherwise sends the next pick onto a well that may be a bare hole — a crash. A tip needlessly skipped is cheap; a head driven into a hole is not |

The tip's own history is **not** in the rack map while it is off the rack (the
well says only `on_pipette`). It lives on the pipette's `TipMount`, persisted
beside the racks and published at `details.mounted_tips`:

| field | answers |
|---|---|
| `rack` / `well` / `wells` | where it came from, so it can go back |
| `channels` | how many tips this one record speaks for |
| `contacted_liquid` | has it been in a real aspirate/dispense — the question asked before re-seating a tip |
| `last_sample` | what it touched most recently |
| `origin_status` | what the well read before the pick, so a return restores it |
| `picked_at` | when it came up |
| `uncertain` | the pick or drop never got a definite answer |

**Mounts are persisted, not session state.** A tip stays physically on the head
across a gateway restart, so the record of where it came from must too — held in
memory it was lost exactly when it was needed, and the tip was stranded: its
origin well no longer read as free and nothing recalled its exposure. On a
return, `last_sample` is written to the destination wells, so a tip that touched
a sample carries that history back into the rack, and one that never met liquid
returns as `new`.

`contacted_liquid` is set only by a real aspirate or dispense (`_mark_tip_used`)
and never inferred. Touching a tracked tiprack does not count as contact — which
is what lets a tip be picked, moved, and re-seated without being marked used.

### Correcting a count: reset vs mark

Two operator assertions, both claim-gated, both audited, neither inferable:

| endpoint | scope | says |
|---|---|---|
| `POST /control/tips/reset` | whole rack | "a fresh rack is in this slot" |
| `POST /control/tips/mark` | `wells` or whole `columns` | "*these* tips are present / gone" |

`tips/mark` exists because reset is all-or-nothing, so a rack that is genuinely
used in some columns and full in others could only be corrected by overstating
it — and an overstated rack sends the head onto bare holes. It sets only the
wells it names, and `set_statuses` validates the whole set before mutating any,
so a typo cannot leave a rack half-corrected.

**Columns and wells are different units for different jobs.** Columns are the
primary one: that is how an 8-channel head consumes a rack. Wells are the
*repair* unit — a tracker that has drifted by one tip needs two wells set to
**different** statuses ("A1 has a tip, B1 is the hole"), which no column
selection can express. The panel offers both (`TipEditor`, column mode by
default; it hides itself on any rack that is not 8 rows deep rather than
mislabelling which wells a click would touch). It shipped column-only, so the
one-well repair — the case a drifted tracker actually presents — had to be made
with a raw API call carrying a machine key, past both operator surfaces. The
endpoint always accepted `wells`; only the UI could not say it.

`status` is restricted to `new` | `empty`: **presence is assertable, contact is
not.** A *touched* tip carries the sample id it contacted, which is evidence the
gateway recorded during a real aspirate — so amber is a state the panel's column
editor reads but can never write.

**Both endpoints release any mount whose tips came from the wells they touch.**
An operator saying "there is a tip in A1" contradicts a mount claiming A1's tip
is on a head, as squarely as "A1 is a bare hole" contradicts a tip coming back
to it; leaving the mount would let the rack disagree with itself. Only the
bookkeeping is dropped — a tip physically on the head stays there, and dropping
it into the trash remains available. This is what makes `tips/mark` the recovery
path when the gateway and the bench disagree about what a head is holding.

Note `tips/reset`'s `wells` argument is *not* a partial reset — it redefines
which wells the rack has, all fresh. `{"slot": "4", "wells": ["A4"]}` leaves
slot 4 tracked as a one-well rack reporting `1/1`. Use `tips/mark` for a subset.

## Declared-layout endpoints

Control-class, claim-gated, audited when proxied through the dashboard's
`/control/*` passthrough:

- `POST /control/deck/declare` — body `{"slots": {"2": "<load_name>" |
  {"load_name"|"kind": ...} | null}}`; accepts full load-names (preferred) or
  the legacy kind-string shape; `null`/omitted clears a slot, empty `{}`
  clears all. POST (not PUT) so it flows through the dashboard passthrough
  and matches the SDK skill-catalog method set.
- `DELETE /control/deck/declare` — clear all.
- `deck.declare` is advertised in `allowed_actions` in every state except
  `EXTERNAL_CONTROL` (pure metadata, like `lights.set`).

**Hard constraint kept:** `GET /status` stays side-effect-free and fast —
`build_deck` runs inline over cached inputs only; no HTTP or REPL read ever
happens in the status path (enforced by test).

## Cross-repo state

- **Dashboard** (`ac-organic-lab`): `LiquidHandlerTile` and the full-page
  OT-2 control interface render `details.snapshot.deck` (declared and
  observed shown separately, mismatches badged ≠). The tile keeps a
  read-only legacy fallback to the old dashboard store for gateways that
  don't publish a deck yet; final deletion of the `api/app/deck.py` stopgap
  is tracked in that repo (`EQUIP_STATUS.md` §11). **That legacy store is
  stale and must not be used to seed a declaration** — for `ot2_hte` it still
  claims slot 7 is a 24-well plate, where the robot reports a 1000 µL tip
  rack.

**Deck view conventions.** `DeckPanel` exists in two copies — this repo's
`/ui` and the dashboard's tile — kept deliberately in step, so a change to one
is ported to the other (`6c46e57` here, `6b95e86` / `a35ad41` there):

- slot number in the cell's top-left corner, as bare text — a pill background
  there reads as an overlay covering A1;
- a `declared` slot carries a solid orange border, named **once** by a legend
  above the deck rather than by a badge on every slot (on a typical deck most
  slots are declared);
- `busy` / `≠` badges in the cell's top-right corner, in both variants;
- tip-rack wells tinted green (fresh) / amber (touched) / grey. A well whose
  tip is on a head reads as grey here too — at 2 px the useful question is "is
  there a tip in it", and *where it went* is the inspector's job (which draws it
  as a violet hollow ring, distinct from an `empty` hole). Grey covers
  both an emptied well *and* an untracked rack: "no tip" and "no idea" are
  both "do not count on it", and the tooltip carries the distinction. Green
  therefore means "known available", so a rack with no green is either spent
  or unregistered — both worth a closer look.
- The declared outline and legend are `page`-variant only. On the compact
  tile nearly every slot is declared, so outlining them all would say nothing
  while shouting.

**Editing a declaration takes two acts.** A slot that already declares
something cannot be re-declared in place: the picker's catalog and free-text
field go inert, only **Clear slot** stays live, and `ControlPanel.declare`
refuses a non-null entry for an already-declared slot. A declaration is not a
label — the gateway *auto-loads* labware from it — so a slot changed by a stray
click reaches the robot. **Clear all declared intent** likewise confirms before
firing, being the one declare action with no per-slot undo.
- **Workflow repos** consume `details.loaded_plate` (unchanged contract) and
  gain the per-slot deck + mismatch flags.
- **xArm** never reports placements — the OT-2 owns its own loaded truth;
  plate handoffs are recorded by the gateway per the handoff spec in
  [`HTTP_TRANSPORT.md`](HTTP_TRANSPORT.md).

## Module assignment in the operator panel

The Modules panel lists attached hardware even when it has no deck assignment.
Unassigned modules are displayed as off deck and do not create deck tiles.
After physically placing a module, an operator holding control can select its
slot. A declared module can be moved to an available slot or unassigned with
**Clear slot**. These controls update declared intent only; they issue no motion
or temperature commands. The full-layout update preserves other declarations,
including custom labware definitions and module serial numbers.

An observed module loaded by the active run or REPL cannot be relocated through
these controls: its session must first be ended or updated. The UI retains that
observed slot instead of hiding it. Occupied destinations and thermocycler
footprint conflicts are unavailable, and labware on a module must be cleared
before changing its placement. Serial matching never substitutes a different
serial; family-only matching is used only for a single unambiguous candidate.

## Implementation map

| Piece | Where |
|---|---|
| Pure normalizers + `build_deck` | `gateway/deck.py` |
| Models (`SlotLabware`, `DeckSlot`, `DeckState`, …) | `gateway/models.py` |
| Declaration store (JSON-atomic, corrupt-tolerant) | `gateway/deck.py::DeckDeclarationStore` |
| Tip store + column geometry (`wells_in_columns`) | `gateway/tip_state.py` |
| Tip corrections (`reset_tip_rack`, `mark_tips`) + column editor | `gateway/service.py`, `ui/src/components/ControlPanel.tsx::TipColumnEditor` |
| Wiring (`_build_deck_state`, TTL run probe, endpoints) | `gateway/service.py`, `gateway/api.py` |
| Tests (decision table, normalizers, store, side-effect-free status) | `tests/unit/test_deck.py`, `test_deck_declare.py`, `test_deck_status.py` |
| Fixtures | `tests/fixtures/status_deck_*.json`, `repl_get_all_states.json`, `robot_run_labware.json` |
