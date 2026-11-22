"""口述史转录稿勘误供数与转写质量评测 API（FastAPI + SQLite）。

- POST   /documents                                  导入正文与实体标注
- GET    /documents                                  文档列表
- GET    /documents/{doc_id}                         版本查询：正文 + 有效标注区间 + 待复核标注
- POST   /documents/{doc_id}/corrections             应用一笔勘误，返回勘误号与受影响批次
- POST   /documents/{doc_id}/corrections/{n}/retract 按号撤回（冲突则 409 并保留现版）
- GET    /documents/{doc_id}/corrections             勘误流水
- GET    /documents/{doc_id}/entities                实体检索（kind / q 过滤）
- GET    /documents/{doc_id}/entities/{number}       单个实体
- POST   /batches                                    按批次号收录两家转写并出分
- GET    /batches/{batch_id}                         评测报告：逐稿版号/错误数、字错率、待复算
- POST   /batches/{batch_id}/recompute               仅重比待复算稿件并按整批版号校验提交
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db, service, scoring


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="口述史转录稿勘误供数 API", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 事务辅助

def _write(fn, *args):
    """写操作：BEGIN IMMEDIATE 保证读写串行，异常回滚。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = fn(conn, *args)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read(fn, *args):
    """读操作：显式读事务，保证同一请求内多次查询落在同一版本快照上。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN DEFERRED")
        return fn(conn, *args)
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------- 模型

class EntityIn(BaseModel):
    number: Optional[int] = Field(default=None, description="实体编号，缺省自动分配")
    kind: Literal["person", "place"]
    name: str = Field(min_length=1)
    start: int = Field(ge=0, description="原文区间起点（含）")
    end: int = Field(ge=0, description="原文区间终点（不含）")


class ImportIn(BaseModel):
    text: str = Field(description="转录稿正文")
    entities: list[EntityIn] = Field(default_factory=list)


class CorrectionIn(BaseModel):
    start: int = Field(ge=0, description="当前版区间起点（含）")
    end: int = Field(ge=0, description="当前版区间终点（不含）")
    text: str = Field(default="", description="替换文本；区间为空即插入，文本为空即删除")


class BatchItemIn(BaseModel):
    manuscript_number: str = Field(min_length=1, description="稿号，批次内唯一")
    document_id: int = Field(description="馆内参考稿文档 ID")
    vendor_a: str = Field(default="", description="供应商 A 的转写文本")
    vendor_b: str = Field(default="", description="供应商 B 的转写文本")


class BatchIn(BaseModel):
    batch_id: str = Field(min_length=1, description="批次号")
    items: list[BatchItemIn] = Field(min_length=1, description="逐稿两家转写文本")


# ---------------------------------------------------------------- 异常映射

@app.exception_handler(service.NotFoundError)
async def not_found_handler(request, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(service.ValidationError)
async def validation_handler(request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(service.ConflictError)
async def conflict_handler(request, exc: service.ConflictError):
    # 撤回被冲突阻断：返回冲突勘误号，现版保持不变
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "conflicts": exc.conflicts},
    )


@app.exception_handler(scoring.VersionConflictError)
async def version_conflict_handler(request, exc: scoring.VersionConflictError):
    # 评分/复算提交时发现参考稿换版：列出换版稿号，已有成绩原样保留
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc),
                 "changed_manuscripts": exc.changed_manuscripts},
    )


# ---------------------------------------------------------------- 路由

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/documents", status_code=201)
def import_document(payload: ImportIn):
    return _write(service.import_document, payload.text,
                  [e.model_dump() for e in payload.entities])


@app.get("/documents")
def documents():
    return _read(service.list_documents)


@app.get("/documents/{doc_id}")
def get_version(doc_id: int):
    """版本查询：同一版返回正文、有效标注当前区间、待复核标注。"""
    return _read(service.get_version, doc_id)


@app.post("/documents/{doc_id}/corrections", status_code=201)
def apply_correction(doc_id: int, payload: CorrectionIn):
    return _write(service.apply_correction, doc_id,
                  payload.start, payload.end, payload.text)


@app.post("/documents/{doc_id}/corrections/{number}/retract")
def retract_correction(doc_id: int, number: int):
    return _write(service.retract_correction, doc_id, number)


@app.get("/documents/{doc_id}/corrections")
def corrections(doc_id: int):
    return _read(service.list_corrections, doc_id)


@app.get("/documents/{doc_id}/entities")
def entities(doc_id: int, kind: Optional[Literal["person", "place"]] = None,
             q: Optional[str] = None):
    return _read(service.search_entities, doc_id, kind, q)


@app.get("/documents/{doc_id}/entities/{number}")
def entity(doc_id: int, number: int):
    items = _read(service.search_entities, doc_id, None, None)
    for item in items:
        if item["number"] == number:
            return item
    raise HTTPException(status_code=404, detail=f"实体编号不存在: {number}")


# ---------------------------------------------------------------- 批次评测

@app.post("/batches", status_code=201)
def create_batch(payload: BatchIn):
    """按稿号收录两家转写并出分。

    长计算在写事务之外完成（期间勘误/撤回照常写入），提交时复查整批参考版号：
    换版则 409 且不写任何成绩；参考总字符数为零则 400 拒绝出分。
    """
    items_in = [it.model_dump() for it in payload.items]
    snapshot = _read(scoring.snapshot_new_batch, payload.batch_id, items_in)
    computed = scoring.compute_new_batch(snapshot)
    return _write(scoring.commit_new_batch, snapshot, computed)


@app.get("/batches/{batch_id}")
def batch_report(batch_id: str):
    """逐稿列出参考版号与两家错误数、两家字错率及待复算稿号。"""
    return _read(scoring.get_batch_report, batch_id)


@app.post("/batches/{batch_id}/recompute")
def recompute_batch(batch_id: str):
    """只重比待复算稿件；整批版号未变才更新成绩并清除待复算标记。"""
    snapshot = _read(scoring.snapshot_recompute, batch_id)
    if not snapshot["pending"]:
        return _read(scoring.get_batch_report, batch_id)
    recomputed = scoring.compute_recompute(snapshot)
    return _write(scoring.commit_recompute, snapshot, recomputed)
