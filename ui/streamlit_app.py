"""Minimal HITL UI — state-driven single page.

Backend's `/policy/state` is the source of truth for which screen to show.

States:
  - empty   → upload widget
  - draft   → editable forms; edits persist to disk via PUT /policy/draft
  - locked  → read-only policy summary + claims upload + adjudication
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import streamlit as st

# Make `api` and `adjudication` importable when Streamlit runs this file directly
# (Streamlit Cloud executes ui/streamlit_app.py without installing the package).
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------- transport ----------
# If API_BASE_URL is set  -> talk to that remote API over the network (split local dev, or a
#                            separately-hosted backend).
# If it is NOT set        -> mount the FastAPI app in-process via ASGI transport. This is what
#                            Streamlit Community Cloud needs: it runs a single process, so there
#                            is no separate uvicorn to connect to. Same routes, no socket.
_API_BASE = os.getenv("API_BASE_URL")

if _API_BASE:
    _client = httpx.Client(base_url=_API_BASE, timeout=180.0)
else:
    from fastapi.testclient import TestClient

    from api.main import app as _api_app  # existing FastAPI app, unchanged

    # TestClient drives the ASGI app synchronously (Streamlit's script run is sync).
    # raise_server_exceptions=False so a 5xx comes back as a response the helpers can
    # surface in the UI, instead of bubbling up as a Python exception.
    _client = TestClient(_api_app, base_url="http://engine", raise_server_exceptions=False)

# What we display to the user when a call fails.
API_BASE = _API_BASE or "in-process engine (ASGI)"

st.set_page_config(page_title="Claim Adjudication Engine", layout="centered")


# ---------- HTTP helpers ----------

def _get(path: str) -> tuple[int, Any]:
    try:
        r = _client.get(path)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    except httpx.HTTPError as e:
        return 0, str(e)


def _post(path: str, json_body: Any = None, files: Any = None) -> tuple[int, Any]:
    try:
        r = _client.post(path, json=json_body, files=files)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    except httpx.HTTPError as e:
        return 0, str(e)


def _put(path: str, json_body: Any) -> tuple[int, Any]:
    try:
        r = _client.put(path, json=json_body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    except httpx.HTTPError as e:
        return 0, str(e)


def _delete(path: str) -> tuple[int, Any]:
    try:
        r = _client.delete(path)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    except httpx.HTTPError as e:
        return 0, str(e)


# ---------- error rendering ----------

def _show_error(payload: Any) -> None:
    """Surface an API error body as a readable Streamlit error.

    Backend errors come back as {"detail": {"error", "message", "hint"}} (or a plain
    string). Pull out the human-readable parts instead of dumping the raw dict.
    """

    detail = payload.get("detail") if isinstance(payload, dict) else payload
    if isinstance(detail, dict):
        msg = detail.get("message") or detail.get("error") or json.dumps(detail)
        st.error(msg)
        if detail.get("hint"):
            st.caption(detail["hint"])
    else:
        st.error(str(detail))


# ---------- formatters ----------

def _aed(v: Any) -> str:
    if v is None:
        return "—"
    return f"AED {float(v):,.2f}"


def _pct(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:.0f}%"


def _render_kb_evidence(
    label: str,
    evidence_ids: list[str],
    kb_index: dict[str, dict],
    key_prefix: str = "",
) -> None:
    """Render the rows the LLM classifier cited as its evidence — as an always-visible table.

    Each row shows the KB id, the category/condition it belongs to, the indicator text the
    classifier matched on, and the relation type. The full data for every cited id is on
    screen — no clicking, no popover quirks when nested in expanders.
    """

    if not evidence_ids:
        return
    st.markdown(f"**{label}**")
    rows: list[dict[str, str]] = []
    for eid in evidence_ids:
        row = kb_index.get(eid)
        if row is None:
            rows.append({"ID": eid, "Category / condition": "—", "Indicator": "(not in KB)", "Relation": "—"})
        else:
            rows.append(
                {
                    "ID": row["id"],
                    "Category / condition": str(row.get("group") or "—"),
                    "Indicator": str(row.get("indicator") or "—"),
                    "Relation": str(row.get("relation") or "—"),
                }
            )
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _decided_by(source: str | None) -> str:
    """Render a ``pre_existing_link.source`` string in a way a human can read at a glance."""

    if not source:
        return "—"
    s = source.strip().lower()
    if s == "manual":
        return "Human reviewer (manual override)"
    if s == "rule":
        return "Deterministic rule"
    if s.startswith("llm:"):
        provider = s.split(":", 1)[1] or "unknown"
        return f"LLM classifier ({provider.title()})"
    return source


def _render_q6_card(card: dict[str, Any]) -> None:
    """Render Q6 — the structured settlement statement — in the two forms the brief requires:
    a human-readable per-claim table (visible inline) and the machine-readable JSON (in an expander).

    Columns mirror the brief verbatim: billed, eligible, deductible, coinsurance, insurer-paid,
    member-paid, decision/reason. Year totals appear below the table.
    """

    answer = card.get("answer") or {}
    rows = answer.get("rows") or []
    totals = answer.get("totals") or {}

    table_rows = [
        {
            "Claim": r["claim_id"],
            "Date": r["service_date"],
            "Benefit": r["benefit_key"],
            "Billed": _aed(r["billed_amount"]),
            "Eligible": _aed(r["eligible_amount"]),
            "Deductible": _aed(r["deductible_applied"]),
            "Coins (M)": _aed(r["coinsurance_member"]),
            "Coins (I)": _aed(r["coinsurance_insurer"]),
            "Penalty": _aed(r["penalty_amount"]),
            "Insurer paid": _aed(r["insurer_paid"]),
            "Member paid": _aed(r["member_paid"]),
            "Decision": r["decision"],
            "Reason": r["reason"],
        }
        for r in rows
    ]
    st.markdown("**Human-readable table**")
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    st.markdown("**Year totals**")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Insurer total", _aed(totals.get("insurer_total")))
    c2.metric("Member total", _aed(totals.get("member_total")))
    c3.metric("Aggregate limit", _aed(totals.get("aggregate_limit")))
    c4.metric("Aggregate remaining", _aed(totals.get("aggregate_remaining")))

    if card.get("sources"):
        st.caption("Sources: " + " · ".join(f"`{s}`" for s in card["sources"]))

    with st.expander("Machine-readable JSON"):
        st.json(card)


def _stringify(v: Any) -> str:
    """Compact rendering of mixed types for the 'derivation' tables."""

    if v is None:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:,}" if isinstance(v, int) else f"{v:.4g}"
    if isinstance(v, dict):
        return ", ".join(f"{k}={_stringify(val)}" for k, val in v.items())
    if isinstance(v, list):
        return ", ".join(_stringify(x) for x in v)
    return str(v)


# ---------- header ----------

st.title("Claim Adjudication Engine")

state_status, state_payload = _get("/policy/state")
if state_status != 200 or not isinstance(state_payload, dict):
    st.error(f"Cannot reach the API at {API_BASE}. status={state_status} payload={state_payload}")
    st.stop()

current_state = state_payload.get("state", "unknown")

step_labels = ["Upload", "Edit (HITL)", "Lock", "Claims"]
step_index = {"empty": 0, "draft": 1, "locked": 3}.get(current_state, 0)
cols = st.columns(4)
for i, label in enumerate(step_labels):
    marker = "●" if i == step_index else ("✓" if i < step_index else "○")
    cols[i].markdown(
        f"<div style='text-align:center'><b>{marker}</b><br><small>{label}</small></div>",
        unsafe_allow_html=True,
    )
st.divider()


# ============================================================
# STATE 1 — EMPTY
# ============================================================

def render_empty() -> None:
    st.subheader("Upload a policy document")
    st.caption(
        "The LLM proposes a structured config from the policy wording. "
        "You will review and edit it on the next screen before locking."
    )
    pol_file = st.file_uploader("Policy PDF or plain text", type=["pdf", "txt"], key="pol_upload_empty")
    if pol_file is None:
        return
    if st.button("Extract with LLM", type="primary", use_container_width=True):
        files = {"file": (pol_file.name, pol_file.getvalue(), pol_file.type or "application/octet-stream")}
        with st.spinner("Calling Claude…"):
            status, payload = _post("/policy/draft/from-pdf", files=files)
        if status != 200:
            _show_error(payload)
            return
        if not payload.get("structurally_valid"):
            st.warning("Schema validation flagged issues — you can fix them in the editor:")
            st.write(payload.get("validation_errors"))
        st.success("Draft saved. Switching to editor…")
        st.rerun()


# ============================================================
# STATE 2 — DRAFT (editable HITL)
# ============================================================

def render_draft(draft: dict[str, Any]) -> None:
    st.subheader("Review & edit — Human-in-the-loop")
    st.caption("All edits live in a persistent draft on disk. Lock the policy when satisfied.")

    # Working copy in session state — survives reruns until we hit the API again.
    if "working_draft_id" not in st.session_state or st.session_state["working_draft_id"] != draft.get("policy_ref"):
        st.session_state.working_draft = json.loads(json.dumps(draft))
        st.session_state["working_draft_id"] = draft.get("policy_ref")
    working: dict[str, Any] = st.session_state.working_draft

    # ---- Policy header ----
    with st.expander("Policy header", expanded=True):
        working["policy_ref"] = st.text_input("Policy reference", working.get("policy_ref", ""))
        c1, c2, c3 = st.columns(3)
        with c1:
            working["plan_year_start"] = st.text_input("Plan year start (YYYY-MM-DD)", working.get("plan_year_start", ""))
        with c2:
            working["plan_year_end"] = st.text_input("Plan year end (YYYY-MM-DD)", working.get("plan_year_end", ""))
        with c3:
            working["policy_start_date"] = st.text_input("Inception date (YYYY-MM-DD)", working.get("policy_start_date", ""))
        c4, c5 = st.columns(2)
        with c4:
            working["aggregate_annual_limit"] = st.number_input(
                "Annual Aggregate Limit (AED)",
                value=float(working.get("aggregate_annual_limit") or 0.0),
                min_value=0.0,
                step=1000.0,
            )
        with c5:
            working["approved_by"] = st.text_input("Approved by", working.get("approved_by", "hitl"))
        working.setdefault("approved_at", working.get("approved_at") or working.get("plan_year_start", ""))

    # ---- Benefits ----
    with st.expander("Benefits (Table of Benefits)", expanded=True):
        rows = []
        for b in working.get("benefits", []):
            rows.append(
                {
                    "key": b.get("key", ""),
                    "name": b.get("name", ""),
                    "annual_sub_limit": b.get("annual_sub_limit"),
                    "in_network_coinsurance": b.get("in_network_coinsurance"),
                    "out_of_network_coinsurance": b.get("out_of_network_coinsurance"),
                    "deductible": b.get("deductible", 0),
                    "requires_preauth": bool(b.get("requires_preauth", False)),
                    "notes": b.get("notes") or "",
                }
            )
        edited = st.data_editor(
            rows,
            column_config={
                "key": st.column_config.TextColumn("key", required=True, help="snake_case stable id"),
                "name": st.column_config.TextColumn("name", required=True),
                "annual_sub_limit": st.column_config.NumberColumn("sub-limit (AED)", help="Empty = within aggregate"),
                "in_network_coinsurance": st.column_config.NumberColumn("IN coins (0..1)", min_value=0.0, max_value=1.0, step=0.01),
                "out_of_network_coinsurance": st.column_config.NumberColumn("OON coins (0..1)", min_value=0.0, max_value=1.0, step=0.01, help="Empty = OON not covered"),
                "deductible": st.column_config.NumberColumn("deductible (AED)", min_value=0.0, step=1.0),
                "requires_preauth": st.column_config.CheckboxColumn("pre-auth?"),
                "notes": st.column_config.TextColumn("notes"),
            },
            num_rows="dynamic",
            use_container_width=True,
            key="benefits_editor",
        )
        working["benefits"] = [
            {
                "key": r["key"],
                "name": r["name"],
                "annual_sub_limit": r.get("annual_sub_limit"),
                "in_network_coinsurance": r["in_network_coinsurance"],
                "out_of_network_coinsurance": r.get("out_of_network_coinsurance"),
                "deductible": r.get("deductible") or 0.0,
                "requires_preauth": bool(r.get("requires_preauth", False)),
                "notes": r.get("notes") or None,
            }
            for r in edited
            if r.get("key")
        ]

    # ---- Endorsements ----
    with st.expander("Endorsements", expanded=True):
        if working.get("endorsements") is None:
            working["endorsements"] = []
        new_endorsements: list[dict[str, Any]] = []
        for i, e in enumerate(working["endorsements"]):
            st.markdown(f"**Endorsement {i + 1}**")
            c1, c2 = st.columns([1, 2])
            with c1:
                e_id = st.text_input("id", e.get("id", ""), key=f"endorse_id_{i}")
                e_benefit = st.text_input("targets benefit key", e.get("benefit_key", ""), key=f"endorse_bkey_{i}")
            with c2:
                e_source = st.text_input("source citation", e.get("source", ""), key=f"endorse_src_{i}")
                c3, c4 = st.columns(2)
                with c3:
                    e_from = st.text_input("effective from", e.get("effective_from") or "", key=f"endorse_from_{i}")
                with c4:
                    e_to = st.text_input("effective to", e.get("effective_to") or "", key=f"endorse_to_{i}")
            overrides_json = st.text_area(
                "overrides (JSON)",
                json.dumps(e.get("overrides") or {}, indent=2),
                height=120,
                key=f"endorse_ov_{i}",
            )
            try:
                overrides = json.loads(overrides_json) if overrides_json.strip() else {}
            except json.JSONDecodeError as je:
                st.error(f"Endorsement {i + 1}: invalid JSON — {je}")
                overrides = e.get("overrides") or {}
            if st.button(f"🗑 Remove endorsement {e_id or i + 1}", key=f"endorse_rm_{i}"):
                continue
            new_endorsements.append(
                {
                    "id": e_id,
                    "benefit_key": e_benefit,
                    "overrides": overrides,
                    "source": e_source,
                    "effective_from": e_from or None,
                    "effective_to": e_to or None,
                }
            )
            st.markdown("---")
        if st.button("+ Add endorsement"):
            new_endorsements.append({"id": "", "benefit_key": "", "overrides": {}, "source": ""})
            working["endorsements"] = new_endorsements
            st.rerun()
        working["endorsements"] = new_endorsements

    # ---- Exclusion rules ----
    with st.expander("Exclusion rules", expanded=True):
        if working.get("exclusion_rules") is None:
            working["exclusion_rules"] = []
        rule_types = ["waiting_period", "preauth_penalty", "not_covered_oon", "not_covered_condition"]
        new_rules: list[dict[str, Any]] = []
        for i, r in enumerate(working["exclusion_rules"]):
            st.markdown(f"**Rule {i + 1}**")
            c1, c2 = st.columns([1, 2])
            with c1:
                r_id = st.text_input("id", r.get("id", ""), key=f"rule_id_{i}")
                r_type_default = r.get("type") if r.get("type") in rule_types else "waiting_period"
                r_type = st.selectbox(
                    "type",
                    rule_types,
                    index=rule_types.index(r_type_default),
                    key=f"rule_type_{i}",
                )
            with c2:
                applies = r.get("applies_to_benefits") or []
                applies_str = st.text_input(
                    "applies_to_benefits (comma-separated, empty = all)",
                    ",".join(applies),
                    key=f"rule_applies_{i}",
                )
                r_reason = st.text_area(
                    "reason_template",
                    r.get("reason_template", ""),
                    height=70,
                    key=f"rule_reason_{i}",
                )
            params_json = st.text_area(
                "params (JSON)",
                json.dumps(r.get("params") or {}, indent=2),
                height=120,
                key=f"rule_params_{i}",
            )
            try:
                params = json.loads(params_json) if params_json.strip() else {}
            except json.JSONDecodeError as je:
                st.error(f"Rule {i + 1}: invalid JSON — {je}")
                params = r.get("params") or {}
            if st.button(f"🗑 Remove rule {r_id or i + 1}", key=f"rule_rm_{i}"):
                continue
            new_rules.append(
                {
                    "id": r_id,
                    "type": r_type,
                    "params": params,
                    "applies_to_benefits": [a.strip() for a in applies_str.split(",") if a.strip()] or None,
                    "reason_template": r_reason,
                }
            )
            st.markdown("---")
        if st.button("+ Add exclusion rule"):
            new_rules.append(
                {"id": "", "type": "waiting_period", "params": {}, "reason_template": "", "applies_to_benefits": None}
            )
            working["exclusion_rules"] = new_rules
            st.rerun()
        working["exclusion_rules"] = new_rules

    with st.expander("Calculation order (GC-1)", expanded=False):
        order_options = ["cap_to_eligible", "apply_deductible", "apply_coinsurance"]
        working["calculation_order"] = st.multiselect(
            "Steps applied in order, per claim",
            order_options,
            default=working.get("calculation_order", order_options),
        )

    # ---- Sticky actions ----
    st.divider()
    a, b, c = st.columns([1, 1, 1])
    with a:
        if st.button("💾 Save draft", use_container_width=True):
            status, payload = _put("/policy/draft", working)
            if status == 200:
                st.success("Draft saved.")
            else:
                st.error(payload)
    with b:
        if st.button("🗑 Discard draft", use_container_width=True):
            status, _ = _delete("/policy/draft")
            if status == 200:
                st.session_state.pop("working_draft", None)
                st.session_state.pop("working_draft_id", None)
                st.rerun()
    with c:
        if st.button("🔒 Lock policy", type="primary", use_container_width=True):
            status, payload = _put("/policy/draft", working)
            if status != 200:
                st.error(payload)
            else:
                status2, payload2 = _post("/policy/lock")
                if status2 == 200:
                    st.session_state.pop("working_draft", None)
                    st.session_state.pop("working_draft_id", None)
                    st.success(f"Locked → {payload2.get('locked_to')}")
                    st.rerun()
                else:
                    st.error(payload2)


# ============================================================
# STATE 3 — LOCKED (waiting for claims)
# ============================================================

def _render_policy_summary(policy: dict[str, Any]) -> None:
    c1, c2 = st.columns(2)
    c1.metric("Annual Aggregate Limit", _aed(policy.get("aggregate_annual_limit")))
    c2.metric("Plan year", f"{policy.get('plan_year_start')} → {policy.get('plan_year_end')}")

    endorsed = {e["benefit_key"] for e in policy.get("endorsements", [])}
    benefits_rows = []
    for b in policy.get("benefits", []):
        oon = "Not covered" if b.get("out_of_network_coinsurance") is None else _pct(b["out_of_network_coinsurance"])
        benefits_rows.append(
            {
                "Benefit": b["name"] + (" ✱" if b["key"] in endorsed else ""),
                "Sub-limit": _aed(b["annual_sub_limit"]) if b.get("annual_sub_limit") is not None else "Within aggregate",
                "IN coins": _pct(b.get("in_network_coinsurance")),
                "OON coins": oon,
                "Deductible": _aed(b.get("deductible")) if b.get("deductible") else "—",
                "Pre-auth?": "Yes" if b.get("requires_preauth") else "No",
            }
        )
    st.dataframe(benefits_rows, use_container_width=True, hide_index=True)
    if endorsed:
        st.caption("✱ overridden by an endorsement (see expander below)")
    with st.expander("Endorsements"):
        for e in policy.get("endorsements", []):
            st.markdown(f"**{e['id']}** — `{e['benefit_key']}` — _{e.get('source', '')}_")
            st.write(e.get("overrides") or {})
    with st.expander("Exclusion rules"):
        for r in policy.get("exclusion_rules", []):
            st.markdown(f"**{r['id']}** — `{r['type']}` — applies to: {r.get('applies_to_benefits') or 'all'}")
            st.caption(r.get("reason_template", ""))


def render_locked(policy: dict[str, Any]) -> None:
    st.subheader("Policy locked — ready for claims")
    _render_policy_summary(policy)
    st.divider()

    unlock_col, clear_col = st.columns(2)
    with unlock_col:
        if st.button("🔓 Unlock for editing", use_container_width=True):
            status, payload = _post("/policy/unlock")
            if status == 200:
                st.rerun()
            else:
                st.error(payload)
    with clear_col:
        if st.button("🗑 Reset to empty", use_container_width=True):
            status, payload = _delete("/policy")
            if status == 200:
                st.rerun()
            else:
                st.error(payload)

    # ── Claim sub-screen: persistent. Check for a saved bundle first. ──
    cur_status, cur_payload = _get("/claims/current")
    if cur_status == 200 and isinstance(cur_payload, dict):
        _render_claim_bundle(cur_payload, persisted=True)
        return
    # No persisted claim → show the upload widget.
    st.subheader("Upload a claim PDF")
    st.caption(
        "Adjudication runs against the locked policy and is saved on disk — refresh the page "
        "and the result stays. Click **Remove claim** below the settlement table to clear it."
    )
    cl_file = st.file_uploader("Claim PDF", type=["pdf"], key="claim_upload")
    if cl_file is None:
        return
    if not st.button("Extract & adjudicate", type="primary"):
        return
    files = {"file": (cl_file.name, cl_file.getvalue(), cl_file.type or "application/pdf")}
    with st.spinner("Extracting claim + adjudicating + answering Q1–Q6…"):
        status, bundle = _post("/claims/run", files=files)
    if status != 200:
        _show_error(bundle)
        return
    _render_claim_bundle(bundle, persisted=True)


def _render_claim_bundle(bundle: dict[str, Any], *, persisted: bool) -> None:
    """Render an adjudication bundle — the same shape that `/claims/run` produces and
    that `/claims/current` returns. Works for both 'fresh' and 'restored from disk' bundles."""

    report = bundle.get("settlement") or {}
    qa_cards = bundle.get("answers") or []
    kb_index: dict[str, dict] = bundle.get("kb_index") or {}
    member = bundle.get("member") or {}
    typed_claims = bundle.get("claims") or []
    filename = bundle.get("filename")

    if st.button("🗑 Remove claim", help="Clear the saved adjudication and return to the upload screen."):
        status, _ = _delete("/claims/current")
        if status == 200:
            st.rerun()

    st.markdown("##### Member context")
    st.write(member)

    c1, c2 = st.columns(2)
    c1.metric("Insurer total", _aed(report.get("insurer_total")))
    c2.metric("Member total", _aed(report.get("member_total")))

    rows = []
    for s in report.get("settlements", []):
        net = s.get("network_status", "")
        rows.append(
            {
                "Claim": s["claim_id"],
                "Review": "🚩" if s.get("requires_review") else "",
                "Date": s["service_date"],
                "Benefit": s["benefit_key"],
                "Network": "In" if net == "in_network" else ("OON" if net == "out_of_network" else "—"),
                "Billed": _aed(s["billed_amount"]),
                "Insurer paid": _aed(s["insurer_paid"]),
                "Member paid": _aed(s["member_paid"]),
                "Decision": s["decision"],
                "Reason": s["reason"],
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    review_count = sum(1 for s in report.get("settlements", []) if s.get("requires_review"))
    if review_count:
        st.warning(
            f"🚩 {review_count} claim(s) flagged for human review — one of the LLM classifiers "
            "(pre-existing or §4.1 category) returned confidence below `high` on a verdict that "
            "would change the outcome if flipped. Expand each flagged row to see which side fired."
        )

    # Per-claim details: reasoning chain + (if flagged) classifier audit trail.
    claims_by_id = {c["claim_id"]: c for c in typed_claims}
    for s in report.get("settlements", []):
        cid = s["claim_id"]
        flag = "🚩 " if s.get("requires_review") else ""
        with st.expander(f"{flag}{cid} — details"):
            st.write(s["reason"])
            claim_blob = claims_by_id.get(cid) or {}
            link = claim_blob.get("pre_existing_link") or {}
            if link:
                st.markdown(
                    "**Chronic condition check**  "
                    "<small style='opacity:0.6'>(is this related to the member's declared chronic condition — policy §4.2)</small>",
                    unsafe_allow_html=True,
                )
                cols = st.columns(3)
                cols[0].metric("Related to chronic?", "Yes" if link.get("is_related") else "No")
                cols[1].metric("Confidence", str(link.get("confidence", "—")).title())
                cols[2].metric("Evidence rows", len(link.get("evidence_ids") or []))
                st.caption(f"**Decided by:** {_decided_by(link.get('source'))}")
                _render_kb_evidence(
                    "Cited KB rows (clinical indicators of the chronic condition)",
                    link.get("evidence_ids") or [],
                    kb_index,
                    key_prefix=f"pre_{cid}",
                )
                if link.get("reasoning"):
                    st.caption("**Reasoning:** " + link["reasoning"])

            cat_evidence = claim_blob.get("category_flags_evidence_ids") or []
            cat_flags = claim_blob.get("category_flags") or []
            cat_conf = claim_blob.get("category_flags_confidence") or "—"
            cat_review = bool(claim_blob.get("category_flags_requires_review"))
            cat_reason = claim_blob.get("category_flags_reasoning")
            # Render the §4.1 panel whenever there's anything to say — flags set, evidence
            # cited, or the category classifier flagged this for review even with empty flags.
            if cat_flags or cat_evidence or cat_review or cat_reason:
                st.markdown("---")
                st.markdown(
                    "**General exclusion check**  "
                    "<small style='opacity:0.6'>(cosmetic / self-inflicted / experimental — policy §4.1)</small>",
                    unsafe_allow_html=True,
                )
                cc1, cc2, cc3 = st.columns(3)
                cc1.metric("Excluded as", ", ".join(cat_flags) if cat_flags else "—")
                cc2.metric("Confidence", str(cat_conf).title())
                cc3.metric("Evidence rows", len(cat_evidence))
                if cat_review:
                    st.caption(
                        "🚩 Flagged because this check's verdict was non-`high` and flipping it "
                        "(marking this as an excluded treatment) would change the outcome."
                    )
                _render_kb_evidence(
                    "Cited KB rows (clinical indicators of an excluded treatment)",
                    cat_evidence,
                    kb_index,
                    key_prefix=f"cat_{cid}",
                )
                if cat_reason:
                    st.caption("**Reasoning:** " + cat_reason)

            with st.expander("Engine reasoning chain (audit log)"):
                st.json(s.get("reasoning") or [])

    # ---------- Q1–Q6 audit panel ----------
    if qa_cards:
        st.divider()
        st.subheader("Q1–Q6 audit · every number with its derivation")
        st.caption(
            "Each card answers one assessment question against THIS claim run. "
            "Open the expander to see the source for every number."
        )
        for card in qa_cards:
            st.markdown(f"##### {card['id'].upper()} · {card['title']}  \n_{card['task']}_")
            st.markdown(f"**{card['headline']}**")

            # Q6 has its own renderer: the brief asks for the structured settlement statement
            # as BOTH machine-readable JSON and a human-readable table.
            if card.get("id") == "q6":
                _render_q6_card(card)
                st.markdown("&nbsp;")
                continue

            with st.expander("Show working"):
                rows = []
                for step in card.get("derivation") or []:
                    rows.append(
                        {
                            "Step": step.get("label", ""),
                            "Value": _stringify(step.get("value")),
                            "Source": step.get("source", ""),
                            "Note": step.get("note") or "",
                        }
                    )
                if rows:
                    st.dataframe(rows, use_container_width=True, hide_index=True)
                if card.get("sources"):
                    st.caption("Citations: " + " · ".join(f"`{s}`" for s in card["sources"]))
                with st.expander("Machine-readable JSON"):
                    st.json(card)
            st.markdown("&nbsp;")


# ============================================================
# Dispatch
# ============================================================

if current_state == "empty":
    render_empty()
elif current_state == "draft":
    render_draft(state_payload.get("draft") or {})
elif current_state == "locked":
    render_locked(state_payload.get("policy") or {})
else:
    st.error(f"Unknown state: {current_state}")
