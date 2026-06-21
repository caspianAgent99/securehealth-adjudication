"""Human-readable table output."""

from __future__ import annotations

from ..models.settlement import SettlementReport


def report_to_table(report: SettlementReport) -> str:
    try:
        from tabulate import tabulate

        rows = [
            [
                s.claim_id,
                s.service_date.isoformat(),
                s.benefit_key,
                f"{s.billed_amount:.2f}",
                f"{s.eligible_amount:.2f}",
                f"{s.deductible_applied:.2f}",
                f"{s.coinsurance_member:.2f}",
                f"{s.coinsurance_insurer:.2f}",
                f"{s.penalty_amount:.2f}",
                f"{s.insurer_paid:.2f}",
                f"{s.member_paid:.2f}",
                s.decision.value,
                s.reason,
            ]
            for s in report.settlements
        ]
        headers = [
            "Claim",
            "Date",
            "Benefit",
            "Billed",
            "Eligible",
            "Deductible",
            "Coins(M)",
            "Coins(I)",
            "Penalty",
            "Insurer",
            "Member",
            "Decision",
            "Reason",
        ]
        out_lines = [
            f"Settlement Report — {report.policy_ref}",
            f"Plan year: {report.plan_year_start.isoformat()} to {report.plan_year_end.isoformat()}",
            "",
            tabulate(rows, headers=headers, tablefmt="github", stralign="left"),
            "",
            f"Insurer total: {report.insurer_total:.2f} AED",
            f"Member total:  {report.member_total:.2f} AED",
            f"Aggregate limit: {report.aggregate_limit:.2f} AED  |  remaining: {report.aggregate_remaining:.2f} AED",
        ]
        return "\n".join(out_lines)
    except ImportError:  # pragma: no cover — tabulate is a hard dep
        lines = [f"Settlement Report — {report.policy_ref}"]
        for s in report.settlements:
            lines.append(
                f"{s.claim_id} {s.service_date} {s.benefit_key} billed={s.billed_amount:.2f} "
                f"insurer={s.insurer_paid:.2f} member={s.member_paid:.2f} decision={s.decision.value}"
            )
        lines.append(f"Insurer total: {report.insurer_total:.2f} | Member total: {report.member_total:.2f}")
        return "\n".join(lines)
