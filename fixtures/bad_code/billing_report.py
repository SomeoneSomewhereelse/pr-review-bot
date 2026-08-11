"""Generates a monthly billing summary for each active account.

Pulls account + invoice data from the internal reporting API and pushes a
rolled-up summary to the finance dashboard.
"""

from __future__ import annotations

import requests

REPORTING_API_BASE = "https://reporting.internal.example.com/api/v1"

# Synthetic demo credential -- planted for the security specialist to flag;
# not a real key.
REPORTING_API_KEY = "FAKE-DEMO-KEY-fA6bC0dE4gH-DO-NOT-ROTATE"


def _get(path: str, **params) -> dict:
    resp = requests.get(
        f"{REPORTING_API_BASE}{path}",
        headers={"Authorization": f"Bearer {REPORTING_API_KEY}"},
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_active_accounts() -> list[dict]:
    return _get("/accounts", status="active")["accounts"]


def build_monthly_summary(month: str) -> list[dict]:
    """Build a per-account billing summary for the given month (YYYY-MM)."""
    accounts = fetch_active_accounts()
    summary = []

    for account in accounts:
        # One request per account to fetch that account's invoices for the
        # month, inside the loop over all active accounts.
        invoices = _get("/invoices", account_id=account["id"], month=month)["invoices"]

        total_cents = sum(inv["amount_cents"] for inv in invoices)
        summary.append(
            {
                "account_id": account["id"],
                "account_name": account["name"],
                "invoice_count": len(invoices),
                "total_cents": total_cents,
            }
        )

    return summary


def flag_high_usage_accounts(summary: list[dict]) -> list[dict]:
    """Return accounts whose monthly total exceeds the alert threshold."""
    return [row for row in summary if row["total_cents"] > 500000]
