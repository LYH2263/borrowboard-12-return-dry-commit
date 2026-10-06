"""归还干跑/提交：整单失败拍板、未在借不带走、二次提交不重写、中途失败回滚。"""
import os, tempfile
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="bb_returns_")

import pytest
from fastapi.testclient import TestClient
from app import seed
from app.db import connect as real_connect
from app.main import app
from app.engines.borrow_rules import stale_loan_ids, counts_after_return, commit_decision

# ---------- 引擎纯函数 ----------

def test_stale_loan_ids():
    by_id = {1: {"id": 1, "status": "active"}, 2: {"id": 2, "status": "returned"}}
    assert stale_loan_ids(by_id, [1]) == []
    assert stale_loan_ids(by_id, [2]) == [2]        # 已还 = 漂移
    assert stale_loan_ids(by_id, [9]) == [9]        # 不存在 = 漂移
    assert stale_loan_ids(by_id, [1, 2, 9]) == [2, 9]

def test_counts_after_return():
    counts = {"available": 2, "active": 2, "overdue": 1}
    loans = [
        {"item_id": 10, "due_date": "2099-01-01", "status": "active"},
        {"item_id": 11, "due_date": "2020-01-01", "status": "active"},
    ]
    assert counts_after_return(counts, loans, "2026-01-01") == {"available": 4, "active": 1, "overdue": 0}

def test_commit_decision():
    assert commit_decision("committed", [1])["action"] == "replay"  # 已提交优先，世界再变也原样回放
    assert commit_decision("preview", [2]) == {"action": "reject", "stale": [2]}
    assert commit_decision("preview", [])["action"] == "commit"

# ---------- 接口级 ----------

@pytest.fixture()
def client():
    seed.init_db()  # 建表（幂等）
    c = real_connect()
    c.executescript("""DELETE FROM return_batches; DELETE FROM loans; DELETE FROM items; DELETE FROM settings;
                       DELETE FROM sqlite_sequence WHERE name IN ('items','loans');""")
    c.commit(); c.close()
    seed.init_db()  # 重新播种
    with TestClient(app) as tc:
        yield tc

def _lend(tc, item_id, borrower="邻居乙", due="2099-01-01"):
    r = tc.post(f"/api/items/{item_id}/lend", json={"borrower": borrower, "due_date": due})
    assert r.status_code == 200
    return r.json()["loan_id"]

def _counts(tc):
    return tc.get("/api/board").json()["counts"]

def test_dry_run_lists_titles_and_leaves_board_untouched(client):
    before = client.get("/api/board").json()
    r = client.post("/api/returns/dry-run", json={"loan_ids": [1]})
    assert r.status_code == 200
    body = r.json()
    assert body["titles"] == ["已外借样例"]            # 将回到可借栏的 title
    assert body["loans"][0]["overdue"] is True
    assert body["counts_after"] == {"available": 4, "active": 0, "overdue": 0}
    assert client.get("/api/board").json() == before  # 干跑后分栏与顶细条集合不变

def test_dry_run_rejects_empty_and_stale(client):
    assert client.post("/api/returns/dry-run", json={"loan_ids": []}).status_code == 400
    assert client.post("/api/returns/dry-run", json={"loan_ids": [999]}).status_code == 409

def test_commit_flips_batch_and_counts_move_together(client):
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [1]}).json()["batch_id"]
    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 200 and r.json()["replay"] is False
    board = client.get("/api/board").json()            # 同一次读取：顶细条与分栏必然一致
    assert board["counts"] == {"available": 4, "active": 0, "overdue": 0}
    assert [i["title"] for i in board["available"]] == ["电钻", "折叠桌", "脏数据-无主", "已外借样例"]
    loans = client.get("/api/loans").json()
    assert [l["id"] for l in loans["returned"]] == [1]  # 提交才改为 returned

def test_commit_stale_batch_fails_whole(client):
    lid2 = _lend(client, 1)                            # 电钻也借出
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [1, lid2]}).json()["batch_id"]
    client.post(f"/api/loans/{lid2}/return")           # 干跑后有人先把电钻还了
    before = client.get("/api/board").json()
    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 409 and r.json()["detail"] == "stale_batch"
    after = client.get("/api/board").json()
    assert after == before                             # 整单失败：分栏与顶细条一丝不动
    assert [l["id"] for l in after["overdue"]] == [1]  # 名单里仍在借的那笔也不许被带走

def test_commit_after_reborrow_same_item_fails_whole(client):
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [1]}).json()["batch_id"]
    client.post("/api/loans/1/return")                 # 同一物先被还
    lid2 = _lend(client, 4, borrower="邻居丙")          # 又借出通过同一物 → 新一笔
    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 409
    board = client.get("/api/board").json()
    assert board["counts"] == {"available": 3, "active": 1, "overdue": 0}
    assert [l["id"] for l in board["active"]] == [lid2]  # 新借出的笔未被旧名单带走

def test_second_commit_replays_without_rewrite(client):
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [1]}).json()["batch_id"]
    assert client.post("/api/returns/commit", json={"batch_id": bid}).status_code == 200
    c = real_connect()
    first = c.execute("SELECT returned_at FROM loans WHERE id=1").fetchone()["returned_at"]
    c.close()
    r = client.post("/api/returns/commit", json={"batch_id": bid})
    assert r.status_code == 200 and r.json()["replay"] is True
    c = real_connect()
    again = c.execute("SELECT returned_at FROM loans WHERE id=1").fetchone()["returned_at"]
    n_returned = c.execute("SELECT COUNT(*) c FROM loans WHERE status='returned'").fetchone()["c"]
    c.close()
    assert again == first and n_returned == 1          # 已还的笔不再写第二遍

def test_commit_unknown_batch_404(client):
    assert client.post("/api/returns/commit", json={"batch_id": "nope"}).status_code == 404

class _FlakyConn:
    """一碰到 UPDATE items 就炸，模拟提交中途失败。"""
    def __init__(self, real): self.__dict__["_real"] = real
    def __getattr__(self, k): return getattr(self.__dict__["_real"], k)
    def __setattr__(self, k, v): setattr(self.__dict__["_real"], k, v)
    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("UPDATE ITEMS"):
            raise RuntimeError("boom_mid_commit")
        return self.__dict__["_real"].execute(sql, params)

def test_commit_mid_failure_rolls_back(client, monkeypatch):
    lid2 = _lend(client, 1)
    bid = client.post("/api/returns/dry-run", json={"loan_ids": [1, lid2]}).json()["batch_id"]
    before = client.get("/api/board").json()
    monkeypatch.setattr("app.main.connect", lambda: _FlakyConn(real_connect()))
    tc = TestClient(app, raise_server_exceptions=False)
    assert tc.post("/api/returns/commit", json={"batch_id": bid}).status_code == 500
    monkeypatch.undo()
    assert client.get("/api/board").json() == before   # 分栏/物主栏/顶细条一起回到提交前
    c = real_connect()
    st = c.execute("SELECT status FROM return_batches WHERE id=?", (bid,)).fetchone()["status"]
    c.close()
    assert st == "preview"                             # 批次未半提交，可原样重试
    assert client.post("/api/returns/commit", json={"batch_id": bid}).status_code == 200
