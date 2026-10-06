"""Batch-return dry-run planning and snapshot tokens.

House rule (decided, do not soften): the dry-run list is a contract.
If any loan's state changed after the dry run — someone returned it,
the same item went out again as a new loan — the whole commit fails
with 409 and nothing is written. Never silently recalculate at commit
time; the caller must re-run the dry run and confirm the fresh list.
"""
import hashlib
import json

def normalize_ids(loan_ids: list[int]) -> list[int]:
    """Sorted, de-duplicated ids, so one loan can never be taken twice."""
    return sorted(set(loan_ids))

def fingerprint(loans: list[dict]) -> str:
    """Token binding the exact loan states the dry run saw."""
    rows = sorted(
        [l["id"], l["item_id"], l["status"], l.get("borrower") or "",
         l.get("due_date") or "", l.get("lent_at") or ""]
        for l in loans
    )
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()

def plan_returns(loans: list[dict], requested_ids: list[int]) -> dict:
    """Split requested ids into returnable items (active) and blocked ones.

    A loan that is missing or not currently borrowed is blocked and must
    never be taken along by the commit.
    """
    by_id = {l["id"]: l for l in loans}
    items, blocked = [], []
    for lid in requested_ids:
        loan = by_id.get(lid)
        if loan is None:
            blocked.append({"loan_id": lid, "reason": "not_found"})
        elif loan.get("status") != "active":
            blocked.append({"loan_id": lid, "reason": "not_active"})
        else:
            items.append(loan)
    return {"items": items, "blocked": blocked}
