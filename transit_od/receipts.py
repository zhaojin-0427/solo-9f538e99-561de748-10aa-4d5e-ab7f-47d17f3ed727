"""分析作业签收：交付页的签收提交与判定。

规则（与需求对应）：
- 仅该日最新且未过期的发布可签收（accepted）；其余情况拒绝（rejected）。
- 同一作业（job_id）对同一 UTC 日的首次提交即落库，为"首次结论"（含被拒结论）；
  重试同一发布号 → 返回首次结论（幂等，不重复写入）；
  改报其他发布号 → 冲突（不改动已有记录）。
- 判定与写入在单个 BEGIN IMMEDIATE 事务内完成，与上传/发布互斥（SQLite 写锁），
  因此签收结论基于同一已提交日版本；任何失败整体回滚，不留半份签收。
"""
from .db import get_day_version, get_esv, now_iso

JOB_ID_MAX_LEN = 120


class ReceiptError(Exception):
    """签收请求本身无效（如作业 ID 为空、发布号不存在），不产生签收记录。"""


def pub_is_current(pub, day_version):
    """发布是否为可用版本：未被取代且底层数据未变更。"""
    return pub["status"] == "current" and pub["day_version"] >= day_version


def submit_receipt(conn, job_id, pub_id):
    """作业提交签收。返回 dict：outcome / day / receipt（receipts 行或 None）。

    outcome:
    - accepted: 首次提交，签收成功（已落库）
    - rejected: 首次提交，签收被拒（结论已落库）
    - retry:    重试同一发布号，返回首次结论（未写入）
    - conflict: 改报其他发布号，与首次结论冲突（未写入）
    """
    job_id = (job_id or "").strip()
    if not job_id:
        raise ReceiptError("作业 ID 不能为空")
    if len(job_id) > JOB_ID_MAX_LEN:
        raise ReceiptError(f"作业 ID 超长（>{JOB_ID_MAX_LEN} 字符）")

    conn.execute("BEGIN IMMEDIATE")
    try:
        pub = conn.execute("SELECT * FROM publications WHERE pub_id=?", (pub_id,)).fetchone()
        if pub is None:
            raise ReceiptError(f"发布号 #{pub_id} 不存在")
        day = pub["day"]

        existing = conn.execute(
            "SELECT * FROM receipts WHERE day=? AND job_id=?", (day, job_id)
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")  # 只读，结束事务释放写锁
            if existing["pub_id"] == pub_id:
                return {"outcome": "retry", "day": day, "receipt": existing}
            return {"outcome": "conflict", "day": day, "receipt": existing}

        # 首次提交：在同一事务内读取已提交日版本并判定
        dv = get_day_version(conn, day)
        esv = get_esv(conn)
        latest = conn.execute(
            "SELECT * FROM publications WHERE day=? ORDER BY pub_id DESC LIMIT 1", (day,)
        ).fetchone()
        if latest["pub_id"] != pub_id:
            verdict = "rejected"
            reason = f"发布 #{pub_id} 不是该日最新发布（最新为 #{latest['pub_id']}）"
        elif not pub_is_current(pub, dv):
            verdict = "rejected"
            reason = f"发布 #{pub_id} 已过期（底层数据已变更，该日暂停供数）"
        else:
            verdict = "accepted"
            reason = None

        conn.execute(
            "INSERT INTO receipts(day, job_id, pub_id, verdict, reason, day_version,"
            " event_set_version, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (day, job_id, pub_id, verdict, reason, dv, esv, now_iso()),
        )
        receipt = conn.execute(
            "SELECT * FROM receipts WHERE day=? AND job_id=?", (day, job_id)
        ).fetchone()
        conn.execute("COMMIT")
        return {"outcome": verdict, "day": day, "receipt": receipt}
    except Exception:
        conn.execute("ROLLBACK")
        raise
