"""两家供应商转写质量评测服务（框架无关）。

同一批访谈（batch）按稿号收录两家供应商各一份转写文本，以馆内现稿（文档
当前版）为共同参考：

- 错误数 = 转写稿改成参考稿的最少字符编辑次数（Levenshtein，插/删/换各计 1）；
- 字错率 = 该家总错误数 / 参考总字符数，分母为零拒绝出分；
- 参考版号取文档 revision，随成绩一并保存；
- 勘误/撤回在同一事务内登记“待复算”，复算只重比待复算稿件；
- 长计算在写事务之外完成，提交时再校验整批参考版号——评分期间勘误照常写入；
  发现换版或写入失败时，已有成绩原样保留。
"""

from __future__ import annotations

import sqlite3

from . import db, engine


class VersionConflictError(Exception):
    """复算提交时发现整批参考稿版号已变化，携带换版稿号。"""

    def __init__(self, message: str, changed_manuscripts: list[str]):
        super().__init__(message)
        self.changed_manuscripts = changed_manuscripts


# 延迟导入，避免与 service 形成模块级循环导入
def _errors():
    from . import service
    return service.NotFoundError, service.ValidationError, service.ConflictError


# ---------------------------------------------------------------- 内部工具

def _current_text(conn: sqlite3.Connection, doc: sqlite3.Row) -> str:
    pieces = db.load_pieces(conn, doc["id"])
    return engine.render(pieces, doc["original_text"],
                         db.correction_texts(conn, doc["id"]))


def _get_document(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    NotFoundError, _, _ = _errors()
    row = conn.execute(
        "SELECT id, revision, original_text FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"文档（稿号对应参考稿）不存在: {document_id}")
    return row


# ---------------------------------------------------------------- 建批：快照 -> 计算 -> 提交

def snapshot_new_batch(conn: sqlite3.Connection, batch_id: str,
                       items_in: list[dict]) -> dict:
    """读事务内快照整批参考稿当前版文本与版号。"""
    _, ValidationError, _ = _errors()
    seen: set[str] = set()
    items = []
    for raw in items_in:
        ms = raw["manuscript_number"]
        if ms in seen:
            raise ValidationError(f"批次内稿号重复: {ms}")
        seen.add(ms)
        doc = _get_document(conn, raw["document_id"])
        items.append({
            "manuscript_number": ms,
            "document_id": raw["document_id"],
            "reference_revision": doc["revision"],
            "reference_text": _current_text(conn, doc),
            "vendor_a": raw["vendor_a"],
            "vendor_b": raw["vendor_b"],
        })
    return {"batch_id": batch_id, "items": items}


def compute_new_batch(snapshot: dict) -> list[dict]:
    """写事务之外完成两家长计算；参考总字符数为零则拒绝出分。"""
    _, ValidationError, _ = _errors()
    computed = []
    total_reference = 0
    for it in snapshot["items"]:
        reference = it["reference_text"]
        total_reference += len(reference)
        computed.append({
            "manuscript_number": it["manuscript_number"],
            "document_id": it["document_id"],
            "reference_revision": it["reference_revision"],
            "reference_length": len(reference),
            "errors_a": engine.levenshtein(it["vendor_a"], reference),
            "errors_b": engine.levenshtein(it["vendor_b"], reference),
        })
    if total_reference == 0:
        raise ValidationError("参考总字符数为零，分母为零，拒绝出分")
    return computed


def commit_new_batch(conn: sqlite3.Connection, snapshot: dict,
                     computed: list[dict]) -> dict:
    """短写事务：先复查整批版号，再写入批次与两家逐稿成绩。"""
    _, _, ConflictError = _errors()
    batch_id = snapshot["batch_id"]
    if conn.execute("SELECT 1 FROM batches WHERE batch_id = ?",
                    (batch_id,)).fetchone() is not None:
        raise ConflictError(f"批次号已存在: {batch_id}", [])

    changed = _changed_manuscripts(
        conn,
        {it["document_id"]: it["reference_revision"] for it in snapshot["items"]},
        {it["document_id"]: it["manuscript_number"] for it in snapshot["items"]},
    )
    if changed:
        raise VersionConflictError(
            "评分期间参考稿发生换版，未写入任何成绩", changed)

    conn.execute("INSERT INTO batches (batch_id) VALUES (?)", (batch_id,))
    rows = []
    for c in computed:
        for vendor, text_key, err_key in (
            ("a", "vendor_a", "errors_a"),
            ("b", "vendor_b", "errors_b"),
        ):
            rows.append((
                batch_id, c["manuscript_number"], c["document_id"], vendor,
                snapshot_item_text(snapshot, c["manuscript_number"], text_key),
                c[err_key], c["reference_length"], c["reference_revision"], 0,
            ))
    conn.executemany(
        "INSERT INTO batch_items (batch_id, manuscript_number, document_id,"
        " vendor, transcript, errors, reference_length, reference_revision, pending)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return get_batch_report(conn, batch_id)


def snapshot_item_text(snapshot: dict, manuscript_number: str, key: str) -> str:
    for it in snapshot["items"]:
        if it["manuscript_number"] == manuscript_number:
            return it[key]
    raise KeyError(manuscript_number)


# ---------------------------------------------------------------- 复算

def snapshot_recompute(conn: sqlite3.Connection, batch_id: str) -> dict:
    """读事务内取批次：待复算稿件重取当前参考稿，同时记录整批版号基线。"""
    NotFoundError, _, _ = _errors()
    if conn.execute("SELECT 1 FROM batches WHERE batch_id = ?",
                    (batch_id,)).fetchone() is None:
        raise NotFoundError(f"批次不存在: {batch_id}")

    stored = conn.execute(
        "SELECT * FROM batch_items WHERE batch_id = ?"
        " ORDER BY manuscript_number, vendor",
        (batch_id,),
    ).fetchall()

    pending_ms = sorted({r["manuscript_number"] for r in stored if r["pending"]})
    pending_docs = {
        r["manuscript_number"]: r["document_id"]
        for r in stored if r["pending"]
    }
    pending = []
    for ms in pending_ms:
        doc = _get_document(conn, pending_docs[ms])
        pending.append({
            "manuscript_number": ms,
            "document_id": pending_docs[ms],
            "reference_revision": doc["revision"],
            "reference_text": _current_text(conn, doc),
        })

    # 整批版号基线：待复算稿用刚取到的当前版号，其余沿用成绩所存版号
    baseline: dict[int, int] = {}
    for p in pending:
        baseline[p["document_id"]] = p["reference_revision"]
    for r in stored:
        baseline.setdefault(r["document_id"], r["reference_revision"])

    return {"batch_id": batch_id, "stored": [dict(r) for r in stored],
            "pending": pending, "baseline": baseline}


def compute_recompute(snapshot: dict) -> dict:
    """只重比待复算稿件；汇总后的整批分母为零则拒绝（不动旧成绩）。"""
    _, ValidationError, _ = _errors()
    recomputed = {}
    for p in snapshot["pending"]:
        reference = p["reference_text"]
        transcripts = _transcripts(snapshot, p["manuscript_number"])
        recomputed[p["manuscript_number"]] = {
            "document_id": p["document_id"],
            "reference_revision": p["reference_revision"],
            "reference_length": len(reference),
            "errors_a": engine.levenshtein(transcripts["a"], reference),
            "errors_b": engine.levenshtein(transcripts["b"], reference),
        }

    total_reference = 0
    seen_ms: set[str] = set()
    for r in snapshot["stored"]:
        if r["manuscript_number"] in seen_ms:
            continue
        seen_ms.add(r["manuscript_number"])
        if r["manuscript_number"] in recomputed:
            total_reference += recomputed[r["manuscript_number"]]["reference_length"]
        else:
            total_reference += r["reference_length"]
    if total_reference == 0:
        raise ValidationError("参考总字符数为零，分母为零，拒绝出分")
    return recomputed


def commit_recompute(conn: sqlite3.Connection, snapshot: dict,
                     recomputed: dict) -> dict:
    """短写事务：整批版号与计算基线一致才更新成绩并清除待复算标记。"""
    changed = _changed_manuscripts(
        conn, snapshot["baseline"],
        {r["document_id"]: r["manuscript_number"] for r in snapshot["stored"]},
    )
    if changed:
        raise VersionConflictError(
            "复算期间参考稿发生换版，已有成绩原样保留", changed)

    for ms, c in recomputed.items():
        conn.execute(
            "UPDATE batch_items SET errors = ?,"
            " reference_length = ?, reference_revision = ?, pending = 0"
            " WHERE batch_id = ? AND manuscript_number = ? AND vendor = 'a'",
            (c["errors_a"], c["reference_length"], c["reference_revision"],
             snapshot["batch_id"], ms),
        )
        conn.execute(
            "UPDATE batch_items SET errors = ?,"
            " reference_length = ?, reference_revision = ?, pending = 0"
            " WHERE batch_id = ? AND manuscript_number = ? AND vendor = 'b'",
            (c["errors_b"], c["reference_length"], c["reference_revision"],
             snapshot["batch_id"], ms),
        )
    return get_batch_report(conn, snapshot["batch_id"])


def _transcripts(snapshot: dict, manuscript_number: str) -> dict[str, str]:
    out = {}
    for r in snapshot["stored"]:
        if r["manuscript_number"] == manuscript_number:
            out[r["vendor"]] = r["transcript"]
    return out


def _changed_manuscripts(conn: sqlite3.Connection, baseline: dict[int, int],
                         doc_to_manuscript: dict[int, str]) -> list[str]:
    """复查基线中的文档当前 revision，返回已换版文档对应的稿号（去重排序）。"""
    current = {
        r["id"]: r["revision"]
        for r in conn.execute(
            "SELECT id, revision FROM documents WHERE id IN (%s)"
            % ",".join("?" * len(baseline)),
            tuple(baseline),
        ).fetchall()
    }
    changed = {
        doc_to_manuscript[doc_id]
        for doc_id, rev in baseline.items()
        if current.get(doc_id) != rev
    }
    return sorted(changed)


# ---------------------------------------------------------------- 待复算登记

def mark_pending_for_document(conn: sqlite3.Connection,
                              document_id: int) -> list[str]:
    """把引用该参考稿的所有批次项登记为待复算，返回受影响批次号。"""
    rows = conn.execute(
        "SELECT DISTINCT batch_id FROM batch_items WHERE document_id = ?"
        " ORDER BY batch_id",
        (document_id,),
    ).fetchall()
    conn.execute(
        "UPDATE batch_items SET pending = 1 WHERE document_id = ?",
        (document_id,),
    )
    return [r["batch_id"] for r in rows]


# ---------------------------------------------------------------- 报告查询

def get_batch_report(conn: sqlite3.Connection, batch_id: str) -> dict:
    """逐稿列出参考版号与两家错误数，并给出两家字错率与待复算稿号。"""
    NotFoundError, _, _ = _errors()
    batch = conn.execute(
        "SELECT batch_id, created_at FROM batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise NotFoundError(f"批次不存在: {batch_id}")

    rows = conn.execute(
        "SELECT * FROM batch_items WHERE batch_id = ?"
        " ORDER BY manuscript_number, vendor",
        (batch_id,),
    ).fetchall()

    manuscripts: dict[str, dict] = {}
    order: list[str] = []
    totals = {"reference_length": 0,
              "vendor_a": {"errors": 0}, "vendor_b": {"errors": 0}}
    pending_ms = []
    for r in rows:
        ms = r["manuscript_number"]
        is_pending = bool(r["pending"])
        if ms not in manuscripts:
            order.append(ms)
            manuscripts[ms] = {
                "manuscript_number": ms,
                "document_id": r["document_id"],
                "reference_revision": r["reference_revision"],
                "reference_length": r["reference_length"],
                "pending": is_pending,
            }
            if is_pending:
                pending_ms.append(ms)
            totals["reference_length"] += r["reference_length"]
        elif is_pending and not manuscripts[ms]["pending"]:
            # 两家行任一被登记待复算，整稿即为待复算
            manuscripts[ms]["pending"] = True
            pending_ms.append(ms)
        manuscripts[ms][f"vendor_{r['vendor']}"] = {"errors": r["errors"]}
        totals[f"vendor_{r['vendor']}"]["errors"] += r["errors"]

    denom = totals["reference_length"]
    for vendor in ("vendor_a", "vendor_b"):
        totals[vendor]["cer"] = (
            totals[vendor]["errors"] / denom if denom > 0 else None)

    return {
        "batch_id": batch["batch_id"],
        "created_at": batch["created_at"],
        "pending_manuscripts": pending_ms,
        "manuscripts": [manuscripts[ms] for ms in order],
        "totals": totals,
    }
