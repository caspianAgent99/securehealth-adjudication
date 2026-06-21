"""Both outputs derive from the same SettlementReport — they can never disagree."""

from .json_report import report_to_json
from .table_report import report_to_table

__all__ = ["report_to_json", "report_to_table"]
