"""PDF extraction smoke tests."""

from __future__ import annotations

from adjudication.config import SETTINGS
from adjudication.extraction.claim_pdf import PDFClaimExtractor


def test_pdf_extractor_returns_six_rows_and_member_context():
    sheet = PDFClaimExtractor().extract(SETTINGS.claims_path)
    ids = sorted(r["claim_id"] for r in sheet.rows if r.get("claim_id"))
    assert ids == ["C1", "C2", "C3", "C4", "C5", "C6"]
    assert "asthma" in sheet.member.declared_chronic_conditions


def test_pdf_extractor_normalizes_wrapped_cells():
    sheet = PDFClaimExtractor().extract(SETTINGS.claims_path)
    by_id = {r["claim_id"]: r for r in sheet.rows}
    # "Outpatient\nConsultation" should be joined and mapped to the canonical key.
    assert by_id["C1"]["benefit_key"] == "outpatient_consultation"
    # "Out-of-Netwo\nrk" should still resolve to out_of_network.
    assert by_id["C6"]["network_status"] == "out_of_network"


def test_pdf_extractor_handles_wrapped_header_cells():
    """Regression: a synthetic PDF where header cells themselves wrap mid-word
    (e.g. 'Clai\\nm', 'Pre-au\\nth') must still produce 6 typed rows."""

    from pathlib import Path

    pdf_path = Path("data/test_inputs/04_Claim_Scenario_Member_B.pdf")
    if not pdf_path.exists():  # pragma: no cover
        return
    sheet = PDFClaimExtractor().extract(pdf_path)
    assert len(sheet.rows) == 6
    assert "hypertension" in sheet.member.declared_chronic_conditions
    by_id = {r["claim_id"]: r for r in sheet.rows}
    # Spot-check the trickiest normalizations.
    assert by_id["C2"]["benefit_key"] == "diagnostics"           # 'Diagnostics (lab\n& imaging)' → diagnostics
    assert by_id["C5"]["benefit_key"] == "inpatient_surgery"    # 'Inpatient &\nSurgery'
    assert by_id["C6"]["benefit_key"] == "pharmacy"             # 'Prescribed\nMedication'
    assert by_id["C6"]["network_status"] == "out_of_network"    # 'Out-of-Netw\nork'
    assert by_id["C5"]["preauth_status"] == "obtained"          # 'Yes' → obtained
    assert by_id["C4"]["billed_amount"] == "2400"                # comma stripped
