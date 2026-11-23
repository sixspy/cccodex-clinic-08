"""配额闸（quota gate）服务。

选定落地形态为**甲侧：旁路凭证 + 指纹链**。

- 每次开批 / 抓取 / 追加，都在同一写事务里先过闸：读已用配额、对照配额徽标，
  然后在 ``quota_ledger`` 落一条「落点」并附 HMAC 指纹（含前一条指纹成链）。
- 凭证键 ``gate_key`` 唯一：同一个当场键再次提交即判「回刷」并拒绝。
- 读侧只认本条旁路凭证：``verify_gate`` 重算指纹与链上一位，对不齐即驳回。
- 写事务使用 ``BEGIN IMMEDIATE``：两侧同事争抢时串行化，后到者重读后失败，
  不会双成功把钉点互相盖掉。
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

from app.config import Config
from app.db import get_db
from app.services.errors import ServiceError

LEDGER_STATUS = ("占用", "已封存", "已驳回")
GATE_ACTION = ("开批", "抓取", "追加")
CALIBER = "配额闸甲侧：旁路凭证 + 指纹链（只认本条凭证，拒回刷，印记不齐即驳回）"


def _secret() -> bytes:
    return str(Config.SECRET).encode("utf-8")


def _fingerprint(
    gate_key: str,
    site_id: int,
    batch_id: int | None,
    action: str,
    units: int,
    used_before: int,
    used_after: int,
    quota_total: int,
    badge_used: int,
    prev_fingerprint: str,
) -> str:
    material = "|".join(
        str(x)
        for x in (
            gate_key,
            int(site_id),
            "" if batch_id is None else int(batch_id),
            action,
            int(units),
            int(used_before),
            int(used_after),
            int(quota_total),
            int(badge_used),
            prev_fingerprint or "",
        )
    )
    return hmac.new(_secret(), material.encode("utf-8"), hashlib.sha256).hexdigest()


def _real_used(conn, site_id: int) -> int:
    """已用配额：实时统计该中心所有访视行。"""
    return conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM visit_rows r
        JOIN visit_batches b ON b.id=r.batch_id
        WHERE b.site_id=?
        """,
        (site_id,),
    ).fetchone()["c"]


def _badge_used(conn, site_id: int) -> int:
    """配额徽标：批次计数器（item_count）汇总，供列表展示。"""
    return conn.execute(
        "SELECT COALESCE(SUM(item_count),0) AS c FROM visit_batches WHERE site_id=?",
        (site_id,),
    ).fetchone()["c"]


def _last_fingerprint(conn, site_id: int) -> tuple[int, str]:
    row = conn.execute(
        """
        SELECT id, fingerprint FROM quota_ledger
        WHERE site_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (site_id,),
    ).fetchone()
    if not row:
        return 0, ""
    return row["id"], row["fingerprint"]


def capacity(site_id: int, conn) -> dict:
    site = conn.execute(
        "SELECT id, site_code, name, status, quota FROM trial_sites WHERE id=?",
        (site_id,),
    ).fetchone()
    if not site:
        raise ServiceError("试验中心不存在", 404)
    used = _real_used(conn, site_id)
    badge = _badge_used(conn, site_id)
    quota = int(site["quota"])
    return {
        "site_id": int(site["id"]),
        "site_code": site["site_code"],
        "site_name": site["name"],
        "site_status": site["status"],
        "quota": quota,
        "used": used,
        "badge": badge,
        "remaining": max(0, quota - used),
        "badge_matches": badge == used,
        "full": used >= quota,
    }


def _human_capacity(cap: dict) -> str:
    return (
        f"中心 {cap['site_code']}·{cap['site_name']}："
        f"已用 {cap['used']} / 配额 {cap['quota']}，剩余 {cap['remaining']}"
    )


def _check_badge(cap: dict) -> None:
    if not cap["badge_matches"]:
        raise ServiceError(
            "配额徽标与已用配额不一致："
            f"徽标 {cap['badge']} · 已用 {cap['used']}。"
            "已拦截本次抓取，请先刷新批次计数使两者对齐后重试。",
            409,
        )


def assert_can_open_batch(site_id: int, db_path: str | None = None) -> dict:
    """只读预检：配额已满时不允许再开新批次。"""
    with get_db(db_path) as conn:
        cap = capacity(site_id, conn)
    _check_badge(cap)
    if cap["full"]:
        raise ServiceError(
            f"配额已满，不能开新批次。{_human_capacity(cap)}；本次开批需要剩余配额大于 0。",
            409,
        )
    return cap


def _record(
    conn,
    *,
    site_id: int,
    batch_id: int | None,
    action: str,
    units: int,
    gate_key: str | None,
    note: str,
    actor: str,
) -> dict:
    """在既有写事务里完成「过闸 + 落点 + 封存」。"""
    if action not in GATE_ACTION:
        raise ServiceError("配额闸动作无效")
    units = int(units)
    if units < 0:
        raise ServiceError("配额闸占用数不能为负")

    cap = capacity(site_id, conn)
    _check_badge(cap)

    if action == "开批" and cap["full"]:
        raise ServiceError(
            f"配额已满，不能开新批次。{_human_capacity(cap)}；"
            "请先提高中心配额或关闭存量批次后再开批。",
            409,
        )
    if units > cap["remaining"]:
        lead = "配额已满，" if cap["full"] else "本次抓取超出剩余配额，"
        raise ServiceError(
            f"{lead}已整笔拦截，未写入任何访视行。"
            f"{_human_capacity(cap)}；本次需要 {units} 行，尚缺 {units - cap['remaining']} 行。",
            409,
        )

    prev_id, prev_fp = _last_fingerprint(conn, site_id)
    used_before = cap["used"]
    used_after = used_before + units

    if gate_key:
        gate_key = str(gate_key).strip()
        if not gate_key:
            gate_key = ""
    if gate_key:
        exists = conn.execute(
            "SELECT id FROM quota_ledger WHERE gate_key=?", (gate_key,)
        ).fetchone()
        if exists:
            raise ServiceError(
                f"凭证 {gate_key} 已存在，拒绝回刷；读侧只认本条旁路凭证。", 409
            )
    else:
        seq = conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM quota_ledger").fetchone()["m"] + 1
        gate_key = f"QG-{seq:06d}"

    fp = _fingerprint(
        gate_key,
        cap["site_id"],
        batch_id,
        action,
        units,
        used_before,
        used_after,
        cap["quota"],
        cap["badge"],
        prev_fp,
    )
    cur = conn.execute(
        """
        INSERT INTO quota_ledger(
          gate_key, site_id, batch_id, action, units, used_before, used_after,
          quota_total, badge_used, prev_fingerprint, fingerprint, status, note, actor
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '占用', ?, ?)
        """,
        (
            gate_key,
            cap["site_id"],
            batch_id,
            action,
            units,
            used_before,
            used_after,
            cap["quota"],
            cap["badge"],
            prev_fp,
            fp,
            (note or "")[:200],
            actor or "admin",
        ),
    )
    ledger_id = cur.lastrowid
    # 提交随即复核并封存：重算指纹，链上一位必须对得上。
    sealed = _seal_in_txn(conn, ledger_id)
    sealed["prev_ledger_id"] = prev_id
    return sealed


def _seal_in_txn(conn, ledger_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM quota_ledger WHERE id=?", (ledger_id,)
    ).fetchone()
    if not row:
        raise ServiceError("配额闸落点不存在", 404)
    if row["status"] == "已驳回":
        raise ServiceError("配额闸落点已驳回，不能封存", 409)
    expected = _fingerprint(
        row["gate_key"],
        row["site_id"],
        row["batch_id"],
        row["action"],
        row["units"],
        row["used_before"],
        row["used_after"],
        row["quota_total"],
        row["badge_used"],
        row["prev_fingerprint"] or "",
    )
    if not hmac.compare_digest(expected, row["fingerprint"]):
        conn.execute(
            "UPDATE quota_ledger SET status='已驳回', note=? WHERE id=?",
            ("封存复核：指纹对不齐，已驳回", ledger_id),
        )
        raise ServiceError("配额闸印记对不齐，已驳回，不得放行。", 409)
    if row["prev_fingerprint"]:
        prev = conn.execute(
            """
            SELECT fingerprint FROM quota_ledger
            WHERE site_id=? AND id<? ORDER BY id DESC LIMIT 1
            """,
            (row["site_id"], ledger_id),
        ).fetchone()
        if not prev or not hmac.compare_digest(
            row["prev_fingerprint"], prev["fingerprint"]
        ):
            conn.execute(
                "UPDATE quota_ledger SET status='已驳回', note=? WHERE id=?",
                ("封存复核：指纹链断裂，已驳回", ledger_id),
            )
            raise ServiceError("配额闸指纹链断裂，已驳回，不得放行。", 409)
    conn.execute(
        """
        UPDATE quota_ledger
        SET status='已封存', sealed_at=datetime('now')
        WHERE id=?
        """,
        (ledger_id,),
    )
    sealed = conn.execute(
        "SELECT * FROM quota_ledger WHERE id=?", (ledger_id,)
    ).fetchone()
    return dict(sealed)


def verify_gate(gate_key: str, db_path: str | None = None) -> dict:
    """读侧：只认本条旁路凭证；指纹或链对不齐即驳回。"""
    gate_key = (gate_key or "").strip()
    if not gate_key:
        raise ServiceError("必须提供配额闸凭证", 400)
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM quota_ledger WHERE gate_key=?", (gate_key,)
        ).fetchone()
        if not row:
            raise ServiceError("配额闸凭证不存在", 404)
        recomputed = _fingerprint(
            row["gate_key"],
            row["site_id"],
            row["batch_id"],
            row["action"],
            row["units"],
            row["used_before"],
            row["used_after"],
            row["quota_total"],
            row["badge_used"],
            row["prev_fingerprint"] or "",
        )
        chain_ok = True
        if row["prev_fingerprint"]:
            prev = conn.execute(
                """
                SELECT fingerprint FROM quota_ledger
                WHERE site_id=? AND id<? ORDER BY id DESC LIMIT 1
                """,
                (row["site_id"], row["id"]),
            ).fetchone()
            chain_ok = bool(prev) and hmac.compare_digest(
                row["prev_fingerprint"], prev["fingerprint"]
            )
        fingerprint_ok = hmac.compare_digest(recomputed, row["fingerprint"])
        data = dict(row)
        data["fingerprint_ok"] = fingerprint_ok
        data["chain_ok"] = bool(chain_ok)
        data["verified"] = fingerprint_ok and chain_ok
        if not data["verified"]:
            data["reject_reason"] = "印记对不齐或指纹链断裂"
        return data


def list_ledger(
    db_path: str | None = None,
    site_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses = []
    params: list = []
    if site_id:
        clauses.append("q.site_id=?")
        params.append(int(site_id))
    if status:
        if status not in LEDGER_STATUS:
            raise ServiceError("配额闸落点状态无效")
        clauses.append("q.status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = max(1, min(500, int(limit or 100)))
    params.append(limit)
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT q.*, s.site_code, s.name AS site_name, b.batch_code
            FROM quota_ledger q
            JOIN trial_sites s ON s.id=q.site_id
            LEFT JOIN visit_batches b ON b.id=q.batch_id
            {where}
            ORDER BY q.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def summary(db_path: str | None = None, site_id: int | None = None) -> dict:
    with get_db(db_path) as conn:
        where = "WHERE site_id=?" if site_id else ""
        params = (int(site_id),) if site_id else ()
        rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS c, COALESCE(SUM(units),0) AS units
            FROM quota_ledger {where}
            GROUP BY status
            """,
            params,
        ).fetchall()
        data = {s: {"count": 0, "units": 0} for s in LEDGER_STATUS}
        for r in rows:
            data[r["status"]] = {"count": r["c"], "units": r["units"]}
        data["total"] = sum(v["count"] for v in data.values() if isinstance(v, dict))
        return data


def site_snapshot(site_id: int, db_path: str | None = None) -> dict:
    """中心侧配额摘要：徽标 / 已用 / 剩余 / 落点条数。"""
    with get_db(db_path) as conn:
        cap = capacity(site_id, conn)
        last = conn.execute(
            """
            SELECT gate_key, action, units, status, created_at
            FROM quota_ledger WHERE site_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(site_id),),
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM quota_ledger WHERE site_id=? AND status='占用'",
            (int(site_id),),
        ).fetchone()["c"]
    cap["last_gate"] = dict(last) if last else None
    cap["open_gates"] = pending
    return cap


def quota_matrix(db_path: str | None = None) -> list[dict]:
    """对账矩阵用的配额摘要。"""
    with get_db(db_path) as conn:
        sites = conn.execute(
            "SELECT id, site_code, name, status, quota FROM trial_sites ORDER BY id"
        ).fetchall()
        out = []
        for s in sites:
            used = _real_used(conn, s["id"])
            badge = _badge_used(conn, s["id"])
            gate_n = conn.execute(
                "SELECT COUNT(*) AS c FROM quota_ledger WHERE site_id=?",
                (s["id"],),
            ).fetchone()["c"]
            out.append(
                {
                    "site_id": s["id"],
                    "site_code": s["site_code"],
                    "name": s["name"],
                    "status": s["status"],
                    "quota": s["quota"],
                    "used": used,
                    "badge": badge,
                    "remaining": max(0, s["quota"] - used),
                    "badge_matches": badge == used,
                    "gate_count": gate_n,
                }
            )
        return out
