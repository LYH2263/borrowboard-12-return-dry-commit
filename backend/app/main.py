from datetime import date, datetime, timezone
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.borrow_rules import can_lend, classify_loans
from app.engines.return_batch import fingerprint, normalize_ids, plan_returns

app = FastAPI(title="Borrowboard", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "borrowboard"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/board")
def board():
    c = connect()
    available = [dict(r) for r in c.execute("SELECT * FROM items WHERE status='available'")]
    loans = [dict(r) for r in c.execute(
        """SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id
           WHERE loans.status='active'""")]
    c.close()
    cls = classify_loans(loans, date.today().isoformat())
    return {
        "available": available,
        "active": cls["active"],
        "overdue": cls["overdue"],
        "counts": {"available": len(available), "active": len(cls["active"]), "overdue": len(cls["overdue"])},
    }

class ItemIn(BaseModel):
    title: str
    owner: str

@app.post("/api/items")
def add_item(body: ItemIn):
    c = connect()
    cur = c.execute("INSERT INTO items(title,owner,status,data_quality) VALUES (?,?,?,?)",
                    (body.title, body.owner, "available", "clean"))
    c.commit(); iid = cur.lastrowid; c.close(); return {"id": iid}

class LendIn(BaseModel):
    borrower: str
    due_date: str

@app.post("/api/items/{iid}/lend")
def lend(iid: int, body: LendIn):
    c = connect()
    item = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    active = c.execute("SELECT COUNT(*) c FROM loans WHERE item_id=? AND status='active'", (iid,)).fetchone()["c"]
    check = can_lend(item["status"], active)
    if not check["ok"]:
        c.close(); raise HTTPException(409, check["reason"])
    cur = c.execute(
        "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
        (iid, body.borrower, "active", body.due_date, datetime.now(timezone.utc).isoformat()))
    c.execute("UPDATE items SET status='on_loan' WHERE id=?", (iid,))
    c.commit(); lid = cur.lastrowid; c.close(); return {"loan_id": lid}

@app.post("/api/loans/{lid}/return")
def return_loan(lid: int):
    c = connect()
    loan = c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
    if not loan: c.close(); raise HTTPException(404, "loan")
    if loan["status"] != "active":
        c.close(); raise HTTPException(400, "not_active")
    c.execute("UPDATE loans SET status='returned', returned_at=? WHERE id=?",
              (datetime.now(timezone.utc).isoformat(), lid))
    c.execute("UPDATE items SET status='available' WHERE id=?", (loan["item_id"],))
    c.commit(); c.close(); return {"ok": True}

def _fetch_loans(c, ids):
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return [dict(r) for r in c.execute(
        f"SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id "
        f"WHERE loans.id IN ({marks})", ids)]

class ReturnBatchIn(BaseModel):
    loan_ids: list[int]

@app.post("/api/returns/dry-run")
def returns_dry_run(body: ReturnBatchIn):
    """Preview a batch return: which titles would go back to the shelf.
    Writes nothing — board columns and the status bar must not move."""
    ids = normalize_ids(body.loan_ids)
    c = connect(); loans = _fetch_loans(c, ids); c.close()
    plan = plan_returns(loans, ids)
    return {
        "token": fingerprint(loans),
        "items": [{"loan_id": l["id"], "title": l["title"], "borrower": l["borrower"],
                   "due_date": l["due_date"]} for l in plan["items"]],
        "blocked": plan["blocked"],
    }

class ReturnCommitIn(ReturnBatchIn):
    token: str

def _apply_loan_return(c, now, loan):
    cur = c.execute(
        "UPDATE loans SET status='returned', returned_at=? WHERE id=? AND status='active'",
        (now, loan["id"]))
    if cur.rowcount != 1:
        raise RuntimeError("loan_state_changed")
    c.execute("UPDATE items SET status='available' WHERE id=?", (loan["item_id"],))

@app.post("/api/returns/commit")
def returns_commit(body: ReturnCommitIn):
    """Apply a dry-run batch verbatim, or fail whole.

    - token mismatch (state moved since the dry run) -> 409, nothing written
    - any requested loan not currently borrowed      -> 409, nothing written
    - same token submitted again                     -> stored result, no rewrite
    - error mid-batch                                -> full rollback
    """
    ids = normalize_ids(body.loan_ids)
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT result_json FROM return_batches WHERE token=?",
                        (body.token,)).fetchone()
        if row:
            result = {**json.loads(row["result_json"]), "replayed": True}
            c.commit()
            return result
        loans = _fetch_loans(c, ids)
        if fingerprint(loans) != body.token:
            raise HTTPException(409, "stale_plan")
        plan = plan_returns(loans, ids)
        if plan["blocked"]:
            raise HTTPException(409, "blocked_present")
        now = datetime.now(timezone.utc).isoformat()
        for loan in plan["items"]:
            _apply_loan_return(c, now, loan)
        result = {
            "ok": True,
            "returned": [{"loan_id": l["id"], "title": l["title"], "borrower": l["borrower"]}
                         for l in plan["items"]],
            "returned_at": now,
        }
        c.execute("INSERT INTO return_batches(token,result_json,created_at) VALUES (?,?,?)",
                  (body.token, json.dumps(result), now))
        c.commit()
        return result
    except HTTPException:
        c.rollback()
        raise
    except Exception:
        c.rollback()
        raise HTTPException(500, "commit_failed_rolled_back")
    finally:
        c.close()

@app.get("/api/loans")
def loans():
    c = connect()
    rows = [dict(r) for r in c.execute(
        "SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id ORDER BY loans.id DESC")]
    c.close()
    return classify_loans(rows, date.today().isoformat())

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
