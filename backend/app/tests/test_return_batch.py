import pytest
from fastapi import HTTPException

from app import main, seed
from app.db import connect
from app.engines.return_batch import fingerprint, normalize_ids, plan_returns


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()  # items 1-3 available, item 4 on_loan with active loan 1 (overdue)
    return tmp_path


def _lend(item_id, borrower="邻居乙", due="2026-12-01"):
    main.lend(item_id, main.LendIn(borrower=borrower, due_date=due))


def _loan(lid):
    c = connect(); row = dict(c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()); c.close()
    return row


def _item(iid):
    c = connect(); row = dict(c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()); c.close()
    return row


def _batch_count():
    c = connect(); n = c.execute("SELECT COUNT(*) c FROM return_batches").fetchone()["c"]; c.close()
    return n


def _dry(ids):
    return main.returns_dry_run(main.ReturnBatchIn(loan_ids=ids))


def _commit(token, ids):
    return main.returns_commit(main.ReturnCommitIn(token=token, loan_ids=ids))


def test_normalize_ids():
    assert normalize_ids([3, 1, 3, 2]) == [1, 2, 3]


def test_plan_splits_returnable_and_blocked():
    loans = [{"id": 1, "status": "active"}, {"id": 2, "status": "returned"}]
    plan = plan_returns(loans, [1, 2, 9])
    assert [l["id"] for l in plan["items"]] == [1]
    assert plan["blocked"] == [{"loan_id": 2, "reason": "not_active"},
                               {"loan_id": 9, "reason": "not_found"}]


def test_fingerprint_tracks_state():
    a = [{"id": 1, "item_id": 4, "status": "active", "borrower": "甲",
          "due_date": "2020-06-01", "lent_at": "2020-05-01"}]
    assert fingerprint(a) == fingerprint(a)
    assert fingerprint(a) != fingerprint([{**a[0], "status": "returned"}])


def test_dry_run_lists_titles_and_writes_nothing(db):
    plan = _dry([1])
    assert plan["token"]
    assert [i["title"] for i in plan["items"]] == ["已外借样例"]
    assert plan["blocked"] == []
    # 干跑之后：分栏与顶细条集合不变
    assert _loan(1)["status"] == "active" and _loan(1)["returned_at"] is None
    assert _item(4)["status"] == "on_loan"
    assert main.board()["counts"] == {"available": 3, "active": 0, "overdue": 1}
    assert _batch_count() == 0


def test_commit_applies_and_counts_move_together(db):
    _lend(1)  # loan 2 on item 1
    plan = _dry([1, 2])
    res = _commit(plan["token"], [1, 2])
    assert res["ok"] and [r["loan_id"] for r in res["returned"]] == [1, 2]
    assert _loan(1)["status"] == "returned" and _loan(1)["returned_at"]
    assert _loan(2)["status"] == "returned"
    assert _item(4)["status"] == "available" and _item(1)["status"] == "available"
    # 顶细条与分栏同一次取数：可借加上去时，在借栏必然已摘下
    assert main.board()["counts"] == {"available": 4, "active": 0, "overdue": 0}


def test_commit_replay_does_not_rewrite(db):
    plan = _dry([1])
    first = _commit(plan["token"], [1])
    again = _commit(plan["token"], [1])
    assert again["replayed"] is True
    assert again["returned_at"] == first["returned_at"]
    assert _loan(1)["returned_at"] == first["returned_at"]  # 已还不再写一遍
    assert _batch_count() == 1


def test_stale_plan_fails_whole_and_takes_nothing(db):
    _lend(1)                      # loan 2 on item 1
    plan = _dry([1, 2])
    main.return_loan(1)           # 干跑之后别人先还了 loan 1
    _lend(4)                      # 同一物又被借出 → loan 3
    with pytest.raises(HTTPException) as e:
        _commit(plan["token"], [1, 2])
    assert e.value.status_code == 409 and e.value.detail == "stale_plan"
    # 整单失败：名单里的 loan 2 不被带走，新借出的 loan 3 不受影响
    assert _loan(2)["status"] == "active" and _item(1)["status"] == "on_loan"
    assert _loan(3)["status"] == "active"
    assert main.board()["counts"] == {"available": 2, "active": 2, "overdue": 0}
    assert _batch_count() == 0


def test_non_active_loan_is_never_taken(db):
    main.return_loan(1)           # loan 1 已还
    _lend(1)                      # loan 2 在借
    plan = _dry([1, 2])
    assert plan["blocked"] == [{"loan_id": 1, "reason": "not_active"}]
    with pytest.raises(HTTPException) as e:
        _commit(plan["token"], [1, 2])
    assert e.value.status_code == 409 and e.value.detail == "blocked_present"
    assert _loan(2)["status"] == "active" and _item(1)["status"] == "on_loan"
    assert _batch_count() == 0


def test_mid_commit_failure_rolls_everything_back(db, monkeypatch):
    _lend(1)                      # loan 2
    plan = _dry([1, 2])
    orig, calls = main._apply_loan_return, {"n": 0}

    def boom(c, now, loan):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return orig(c, now, loan)

    monkeypatch.setattr(main, "_apply_loan_return", boom)
    with pytest.raises(HTTPException) as e:
        _commit(plan["token"], [1, 2])
    assert e.value.status_code == 500 and e.value.detail == "commit_failed_rolled_back"
    # 分栏、物主栏、顶细条一起回到提交前：第一笔已写的也被回滚
    assert _loan(1)["status"] == "active" and _loan(2)["status"] == "active"
    assert _item(4)["status"] == "on_loan" and _item(1)["status"] == "on_loan"
    assert main.board()["counts"] == {"available": 2, "active": 1, "overdue": 1}
    assert _batch_count() == 0
