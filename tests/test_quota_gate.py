import json
import threading

import pytest

from app.db import get_db
from app.services import quota_gate as gate_svc
from app.services import reconcile, sites, visits
from app.services.errors import ServiceError


def _new_site(db_path, quota, name="闸门测试中心"):
    return sites.create_site(name, quota=quota, db_path=db_path)


def _new_batch(db_path, site_id):
    return visits.create_batch(site_id, note="闸门测试批", db_path=db_path)


def _rows_in_site(db_path, site_id):
    with get_db(db_path) as conn:
        return conn.execute(
            """
            SELECT COUNT(*) AS c FROM visit_rows r
            JOIN visit_batches b ON b.id=r.batch_id
            WHERE b.site_id=?
            """,
            (site_id,),
        ).fetchone()["c"]


def _seed_rows(db_path, site_id, n):
    """Fill a site up to n rows via gated crawls."""
    batch = _new_batch(db_path, site_id)
    remaining = n
    while remaining > 0:
        step = min(30, remaining)
        visits.mock_crawl(batch["id"], count=step, mix_bad=False, db_path=db_path)
        remaining -= step


def test_quota_full_blocks_new_batch(db_path):
    site = _new_site(db_path, quota=2, name="满配开批")
    _seed_rows(db_path, site["id"], 2)
    with pytest.raises(ServiceError) as exc:
        _new_batch(db_path, site["id"])
    assert "配额已满" in exc.value.message
    assert exc.value.status_code == 409


def test_quota_full_blocks_crawl(db_path):
    site = _new_site(db_path, quota=3, name="满配抓取")
    _seed_rows(db_path, site["id"], 3)
    batch = visits.list_batches(db_path, site_id=site["id"])[0]
    before = _rows_in_site(db_path, site["id"])
    with pytest.raises(ServiceError) as exc:
        visits.mock_crawl(batch["id"], count=1, db_path=db_path)
    assert "配额已满" in exc.value.message
    assert _rows_in_site(db_path, site["id"]) == before


def test_crawl_over_remaining_is_atomic(db_path):
    site = _new_site(db_path, quota=10, name="超额整笔")
    _seed_rows(db_path, site["id"], 8)
    batch = visits.list_batches(db_path, site_id=site["id"])[0]
    before = _rows_in_site(db_path, site["id"])
    assert before == 8
    with pytest.raises(ServiceError) as exc:
        visits.mock_crawl(batch["id"], count=5, db_path=db_path)
    msg = exc.value.message
    assert "整笔拦截" in msg
    assert "已用 8 / 配额 10" in msg
    assert "尚缺 3 行" in msg
    # no partial writes
    assert _rows_in_site(db_path, site["id"]) == 8
    # no gate point recorded for the rejected attempt
    with get_db(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM quota_ledger WHERE site_id=? AND note LIKE '模拟抓取%'",
            (site["id"],),
        ).fetchone()["c"]
    assert n == 1  # only the earlier successful fill crawl


def test_exact_remaining_succeeds_then_next_fails(db_path):
    site = _new_site(db_path, quota=5, name="刚好配额")
    _seed_rows(db_path, site["id"], 3)
    batch = visits.list_batches(db_path, site_id=site["id"])[0]
    done = visits.mock_crawl(batch["id"], count=2, db_path=db_path)
    assert done["created"] == 2
    assert _rows_in_site(db_path, site["id"]) == 5
    with pytest.raises(ServiceError):
        visits.mock_crawl(batch["id"], count=1, db_path=db_path)
    assert _rows_in_site(db_path, site["id"]) == 5


def test_add_row_blocked_at_quota(db_path):
    site = _new_site(db_path, quota=1, name="追加闸")
    batch = _new_batch(db_path, site["id"])
    visits.add_visit_row(
        batch["id"],
        "Q-0001",
        "V1",
        payload=json.dumps(
            {"visit_date": "2026-11-20", "investigator": "医生"}, ensure_ascii=False
        ),
        db_path=db_path,
    )
    with pytest.raises(ServiceError) as exc:
        visits.add_visit_row(
            batch["id"],
            "Q-0002",
            "V1",
            payload=json.dumps(
                {"visit_date": "2026-11-20", "investigator": "医生"},
                ensure_ascii=False,
            ),
            db_path=db_path,
        )
    assert "超出剩余配额" in exc.value.message or "配额已满" in exc.value.message


def test_gate_ledger_row_and_chain(db_path):
    site = _new_site(db_path, quota=20, name="落点入表")
    _seed_rows(db_path, site["id"], 3)
    _seed_rows(db_path, site["id"], 2)
    all_gates = gate_svc.list_ledger(db_path=db_path, site_id=site["id"])
    gates = [g for g in all_gates if g["action"] == "抓取"]
    assert len(gates) == 2
    # opening a batch also lands a zero-unit gate point
    assert len([g for g in all_gates if g["action"] == "开批"]) == 2
    for g in gates:
        assert g["status"] == "已封存"
        assert g["quota_total"] == 20
        assert g["fingerprint"]
        verified = gate_svc.verify_gate(g["gate_key"], db_path=db_path)
        assert verified["verified"] is True
    # chain: every point links to the immediately preceding point at this site
    by_id = {g["id"]: g for g in all_gates}
    for g in all_gates:
        prev = by_id.get(g["id"] - 1)
        if prev is not None:
            assert g["prev_fingerprint"] == prev["fingerprint"]


def test_verify_gate_rejects_tampered_fingerprint(db_path):
    site = _new_site(db_path, quota=10, name="印记校验")
    _seed_rows(db_path, site["id"], 2)
    gate = gate_svc.list_ledger(db_path=db_path, site_id=site["id"])[0]
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE quota_ledger SET fingerprint='deadbeef' WHERE id=?",
            (gate["id"],),
        )
    result = gate_svc.verify_gate(gate["gate_key"], db_path=db_path)
    assert result["verified"] is False
    assert result["fingerprint_ok"] is False


def test_duplicate_gate_key_rejected(db_path):
    site = _new_site(db_path, quota=10, name="拒回刷")
    batch = _new_batch(db_path, site["id"])
    with get_db(db_path, immediate=True) as conn:
        first = gate_svc._record(
            conn,
            site_id=site["id"],
            batch_id=batch["id"],
            action="抓取",
            units=1,
            gate_key="QGK-FIXED",
            note="首次",
            actor="admin",
        )
    assert first["status"] == "已封存"
    with pytest.raises(ServiceError) as exc:
        with get_db(db_path, immediate=True) as conn:
            gate_svc._record(
                conn,
                site_id=site["id"],
                batch_id=batch["id"],
                action="抓取",
                units=1,
                gate_key="QGK-FIXED",
                note="回刷",
                actor="admin",
            )
    assert "拒绝回刷" in exc.value.message


def test_badge_drift_blocks_crawl(db_path):
    site = _new_site(db_path, quota=10, name="徽标拧花")
    _seed_rows(db_path, site["id"], 2)
    batch = visits.list_batches(db_path, site_id=site["id"])[0]
    # force the counter badge out of sync with the real row count
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE visit_batches SET item_count=item_count+3 WHERE site_id=?",
            (site["id"],),
        )
    with pytest.raises(ServiceError) as exc:
        visits.mock_crawl(batch["id"], count=1, db_path=db_path)
    assert "徽标与已用配额不一致" in exc.value.message
    assert exc.value.status_code == 409


def test_concurrent_crawl_does_not_double_spend(db_path):
    site = _new_site(db_path, quota=10, name="争用测试")
    batch = _new_batch(db_path, site["id"])
    results = []
    errors = []

    def worker():
        try:
            r = visits.mock_crawl(batch["id"], count=8, db_path=db_path)
            results.append(r)
        except ServiceError as exc:
            errors.append(exc)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1, f"expected exactly one success, got {len(results)}"
    assert len(errors) == 1
    used = _rows_in_site(db_path, site["id"])
    assert used == 8, f"quota double-spent: used={used}"


def test_sites_list_exposes_badge_and_used(db_path):
    site = _new_site(db_path, quota=7, name="列表徽标")
    _seed_rows(db_path, site["id"], 2)
    row = next(s for s in sites.list_sites(db_path) if s["id"] == site["id"])
    assert row["quota_used"] == 2
    assert row["quota_badge"] == 2
    assert row["quota_remaining"] == 5
    assert row["badge_matches"] is True
    assert row["gate_count"] >= 1
    detail = sites.get_site(site["id"], db_path)
    assert detail["quota_used"] == 2
    assert detail["gates"]


def test_reconcile_matrix_carries_quota_summary(db_path):
    site = _new_site(db_path, quota=6, name="矩阵配额")
    _seed_rows(db_path, site["id"], 2)
    matrix = {m["id"]: m for m in reconcile.site_gap_matrix(db_path)}
    m = matrix[site["id"]]
    assert m["quota"] == 6
    assert m["quota_used"] == 2
    assert m["quota_badge"] == 2
    assert m["badge_matches"] is True
    assert m["quota_remaining"] == 4
    assert m["gate_count"] >= 1


def test_seeded_sites_badge_aligned(db_path):
    for row in sites.list_sites(db_path):
        assert row["badge_matches"] is True, row["site_code"]

