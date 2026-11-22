"""批次评测 API 集成测试：两家供应商转写质量、待复算与换版保护。

运行：python3 -m pytest tests/test_scoring.py
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_doc(client, text, entities=None):
    payload = {"text": text, "entities": entities or []}
    r = client.post("/documents", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def create_batch(client, batch_id, entries):
    """entries: [(稿号, 文档ID, A稿, B稿)]"""
    payload = {
        "batch_id": batch_id,
        "items": [
            {"manuscript_number": ms, "document_id": doc,
             "vendor_a": a, "vendor_b": b}
            for ms, doc, a, b in entries
        ],
    }
    return client.post("/batches", json=payload)


def correct(client, doc, start, end, text=""):
    r = client.post(f"/documents/{doc}/corrections",
                    json={"start": start, "end": end, "text": text})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- 建批与报告

def test_batch_scoring_report(client):
    d1 = make_doc(client, "ABCDEFG")          # 7 字符
    d2 = make_doc(client, "甲乙丙")            # 3 字符
    r = create_batch(client, "B01", [
        ("MS-1", d1, "ABXYEFG", "ABCDEFG"),   # A 错 2（C->X,D->Y），B 全对
        ("MS-2", d2, "甲乙丁", "甲丁丙"),      # A 错 1，B 错 1
    ])
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["batch_id"] == "B01"
    assert [m["manuscript_number"] for m in rep["manuscripts"]] == ["MS-1", "MS-2"]
    ms1, ms2 = rep["manuscripts"]
    assert ms1["reference_revision"] == 0
    assert ms1["reference_length"] == 7
    assert ms1["vendor_a"]["errors"] == 2
    assert ms1["vendor_b"]["errors"] == 0
    assert ms2["vendor_a"]["errors"] == 1
    assert ms2["vendor_b"]["errors"] == 1
    assert ms1["pending"] is False
    # 参考总长 10：A 总错 3 -> 0.3；B 总错 1 -> 0.1
    assert rep["totals"]["vendor_a"] == {"errors": 3, "cer": pytest.approx(0.3)}
    assert rep["totals"]["vendor_b"] == {"errors": 1, "cer": pytest.approx(0.1)}
    assert rep["totals"]["reference_length"] == 10
    assert rep["pending_manuscripts"] == []

    # 报告可凭批次号查询，内容一致
    got = client.get("/batches/B01")
    assert got.status_code == 200
    assert got.json() == rep


def test_empty_reference_total_rejected(client):
    d = make_doc(client, "")
    r = create_batch(client, "B02", [("MS-9", d, "随便写", "也写点")])
    assert r.status_code == 400
    assert "分母" in r.json()["detail"]
    # 拒绝出分：批次与成绩均未写入
    assert client.get("/batches/B02").status_code == 404


def test_partial_empty_reference_allowed(client):
    d_empty = make_doc(client, "")
    d_ok = make_doc(client, "甲乙")
    r = create_batch(client, "B03", [
        ("MS-E", d_empty, "xyz", ""),         # 单稿参考 0 字符，总分母仍为 2
        ("MS-O", d_ok, "甲丁", "甲乙"),
    ])
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["manuscripts"][0]["reference_length"] == 0
    assert rep["totals"]["reference_length"] == 2
    assert rep["totals"]["vendor_a"]["cer"] == pytest.approx(4 / 2)  # 3+1


def test_batch_validation(client):
    d = make_doc(client, "ABC")
    # 稿号重复
    r = create_batch(client, "B04", [
        ("MS-1", d, "a", "b"), ("MS-1", d, "c", "d")])
    assert r.status_code == 400
    # 参考稿不存在
    r = create_batch(client, "B05", [("MS-1", 9999, "a", "b")])
    assert r.status_code == 404
    # 批次号重复
    create_batch(client, "B06", [("MS-1", d, "a", "b")])
    r = create_batch(client, "B06", [("MS-1", d, "a", "b")])
    assert r.status_code == 409
    # 查询不存在的批次
    assert client.get("/batches/NOPE").status_code == 404
    assert client.post("/batches/NOPE/recompute").status_code == 404


# ---------------------------------------------------------------- 待复算登记

def test_correction_registers_pending_and_lists_batches(client):
    d1 = make_doc(client, "ABCDEFG")
    d2 = make_doc(client, "甲乙丙")
    create_batch(client, "B01", [("MS-1", d1, "ABXYEFG", "ABCDEFG")])
    create_batch(client, "B02", [("MS-2", d2, "甲乙丁", "甲丁丙"),
                                 ("MS-1b", d1, "x", "y")])
    # 无关文档的勘误不影响批次
    d3 = make_doc(client, "ZZZZ")
    out = correct(client, d3, 0, 1, "Q")
    assert out["affected_batches"] == []

    out = correct(client, d1, 2, 4, "XY")     # 改 d1（MS-1 / MS-1b 的参考稿）
    assert out["affected_batches"] == ["B01", "B02"]

    rep = client.get("/batches/B01").json()
    assert rep["pending_manuscripts"] == ["MS-1"]
    b01 = {m["manuscript_number"]: m for m in rep["manuscripts"]}
    assert b01["MS-1"]["pending"] is True
    # 上次成绩原样可查
    assert b01["MS-1"]["vendor_a"]["errors"] == 2
    rep2 = client.get("/batches/B02").json()
    assert rep2["pending_manuscripts"] == ["MS-1b"]
    b02 = {m["manuscript_number"]: m for m in rep2["manuscripts"]}
    assert b02["MS-1b"]["pending"] is True
    assert b02["MS-2"]["pending"] is False

    # 撤回同样登记并回执
    r = client.post(f"/documents/{d1}/corrections/{out['correction_number']}/retract")
    assert r.status_code == 200
    assert r.json()["affected_batches"] == ["B01", "B02"]
    assert client.get("/batches/B01").json()["pending_manuscripts"] == ["MS-1"]


def test_pending_idempotent_within_same_transaction(client):
    d = make_doc(client, "ABCDEFG")
    create_batch(client, "B01", [("MS-1", d, "ABXYEFG", "ABCDEFG")])
    correct(client, d, 0, 1, "X")
    correct(client, d, 0, 1, "Y")   # 再次勘误：仍只有一份待复算登记
    rep = client.get("/batches/B01").json()
    assert rep["pending_manuscripts"] == ["MS-1"]


# ---------------------------------------------------------------- 复算

def test_recompute_only_pending_and_clears_flag(client):
    d1 = make_doc(client, "ABCDEFG")
    d2 = make_doc(client, "甲乙丙")
    create_batch(client, "B01", [
        ("MS-1", d1, "ABXYEFG", "ABCDEFG"),
        ("MS-2", d2, "甲乙丁", "甲丁丙"),
    ])
    # 只改 d1：MS-1 待复算，MS-2 不在重比范围
    correct(client, d1, 2, 4, "XY")   # 现版 ABXYEFG（恰与 A 稿一致）
    rep = client.get("/batches/B01").json()
    assert rep["pending_manuscripts"] == ["MS-1"]

    r = client.post("/batches/B01/recompute")
    assert r.status_code == 200, r.text
    new = r.json()
    ms1, ms2 = new["manuscripts"]
    assert ms1["pending"] is False
    assert ms1["reference_revision"] == 1
    assert ms1["vendor_a"]["errors"] == 0     # A 稿与新参考完全一致
    assert ms1["vendor_b"]["errors"] == 2     # B 旧稿相对新参考错 2
    assert ms2["reference_revision"] == 0     # 未重比，版号保留
    assert ms2["vendor_a"]["errors"] == 1
    assert new["pending_manuscripts"] == []
    # 新总分母不变（7+3=10）：A 总错 0+1=1，B 总错 2+1=3
    assert new["totals"]["vendor_a"] == {"errors": 1, "cer": pytest.approx(0.1)}
    assert new["totals"]["vendor_b"] == {"errors": 3, "cer": pytest.approx(0.3)}


def test_recompute_no_pending_is_noop(client):
    d = make_doc(client, "ABC")
    create_batch(client, "B01", [("MS-1", d, "ABD", "ABC")])
    before = client.get("/batches/B01").json()
    r = client.post("/batches/B01/recompute")
    assert r.status_code == 200
    assert r.json() == before


def test_recompute_version_change_keeps_old_scores(client, monkeypatch):
    from app import db, scoring, service
    d = make_doc(client, "ABCDEFG")
    create_batch(client, "B01", [("MS-1", d, "ABXYEFG", "ABCDEFG")])
    correct(client, d, 2, 4, "XY")

    # 模拟“计算期间又发生勘误”：长计算完成后、提交事务前，参考稿由另一事务换版。
    # 在单线程内按服务层顺序编排（TestClient 请求生命周期内开第二连接会触发其
    # WAL 环境问题，与本服务逻辑无关）；HTTP 只用于校验最终状态。
    conn = db.connect()
    conn.execute("BEGIN DEFERRED")
    snapshot = scoring.snapshot_recompute(conn, "B01")
    conn.rollback(); conn.close()
    recomputed = scoring.compute_recompute(snapshot)

    writer = db.connect()
    writer.execute("BEGIN IMMEDIATE")
    service.apply_correction(writer, d, 0, 0, "Z")   # 评分期间照常保存勘误
    writer.commit(); writer.close()

    committer = db.connect()
    with pytest.raises(scoring.VersionConflictError) as exc:
        committer.execute("BEGIN IMMEDIATE")
        scoring.commit_recompute(committer, snapshot, recomputed)
    assert exc.value.changed_manuscripts == ["MS-1"]
    committer.rollback(); committer.close()

    # 已有成绩与待复算标记原样保留（经 HTTP 复核）
    rep = client.get("/batches/B01").json()
    ms = rep["manuscripts"][0]
    assert ms["reference_revision"] == 0
    assert ms["vendor_a"]["errors"] == 2
    assert ms["vendor_b"]["errors"] == 0
    assert rep["pending_manuscripts"] == ["MS-1"]

    # 再次复算（不再换版）：正常更新并清标记
    r = client.post("/batches/B01/recompute")
    assert r.status_code == 200
    rep = r.json()
    assert rep["pending_manuscripts"] == []
    assert rep["manuscripts"][0]["reference_revision"] == 2


def test_initial_scoring_version_change_writes_nothing(client):
    # 长计算期间另一事务使参考稿换版：服务层单线程编排，验证提交 409 且不写批次
    from app import db, scoring, service
    d = make_doc(client, "ABCDEFG")

    reader = db.connect()
    reader.execute("BEGIN DEFERRED")
    snapshot = scoring.snapshot_new_batch(
        reader, "BRACE",
        [{"manuscript_number": "MS-1", "document_id": d,
          "vendor_a": "ABXYEFG", "vendor_b": "ABCDEFG"}])
    reader.rollback(); reader.close()
    computed = scoring.compute_new_batch(snapshot)

    writer = db.connect()
    writer.execute("BEGIN IMMEDIATE")
    service.apply_correction(writer, d, 2, 4, "XY")
    writer.commit(); writer.close()

    committer = db.connect()
    with pytest.raises(scoring.VersionConflictError) as exc:
        committer.execute("BEGIN IMMEDIATE")
        scoring.commit_new_batch(committer, snapshot, computed)
    assert exc.value.changed_manuscripts == ["MS-1"]
    committer.rollback(); committer.close()

    # 批次与成绩均未写入（经 HTTP 复核）
    assert client.get("/batches/BRACE").status_code == 404

    # 重新出分成功（基于新版）
    r = create_batch(client, "BRACE", [("MS-1", d, "ABXYEFG", "ABCDEFG")])
    assert r.status_code == 201
    rep = r.json()
    assert rep["manuscripts"][0]["reference_revision"] == 1


def test_write_failure_preserves_old_scores(client):
    from unittest import mock
    from app import db, scoring
    d = make_doc(client, "ABCDEFG")
    create_batch(client, "B01", [("MS-1", d, "ABXYEFG", "ABCDEFG")])
    correct(client, d, 2, 4, "XY")

    # 服务层复算：提交跑到第二条 UPDATE 时模拟磁盘故障，事务回滚。
    # sqlite3.Connection 为内置不可变类型，无法直接 patch，故在 connect 工厂
    # 层包装，仅对复算提交连接注入故障。
    conn = db.connect()
    conn.execute("BEGIN DEFERRED")
    snapshot = scoring.snapshot_recompute(conn, "B01")
    conn.rollback(); conn.close()
    recomputed = scoring.compute_recompute(snapshot)

    state = {"updates": 0}
    real_connect = db.sqlite3.connect

    def flaky_connect(path, *a, **kw):
        c = real_connect(path, *a, **kw)
        c.row_factory = sqlite3.Row
        first = not state.get("wrapped")
        if first:
            state["wrapped"] = True

            class FailingConn:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, sql, params=()):
                    if "UPDATE batch_items" in sql:
                        state["updates"] += 1
                        if state["updates"] == 2:
                            raise sqlite3.OperationalError(
                                "simulated disk I/O error")
                    return self._inner.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(self._inner, name)
            return FailingConn(c)
        return c

    with mock.patch.object(db.sqlite3, "connect", flaky_connect):
        committer = db.connect()
        with pytest.raises(sqlite3.OperationalError):
            committer.execute("BEGIN IMMEDIATE")
            scoring.commit_recompute(committer, snapshot, recomputed)
        committer.rollback()
        committer.close()

    # 旧成绩与待复算标记原样保留（经 HTTP 复核）
    rep = client.get("/batches/B01").json()
    ms = rep["manuscripts"][0]
    assert ms["reference_revision"] == 0
    assert ms["vendor_a"]["errors"] == 2
    assert ms["vendor_b"]["errors"] == 0
    assert rep["pending_manuscripts"] == ["MS-1"]


def test_multiple_manuscripts_changed_all_listed(client):
    d1 = make_doc(client, "ABC")
    d2 = make_doc(client, "DEF")
    create_batch(client, "B01", [
        ("MS-1", d1, "ABX", "ABC"),
        ("MS-2", d2, "DEY", "DEF"),
    ])
    correct(client, d1, 0, 1, "X")
    correct(client, d2, 0, 1, "Y")
    r = client.post("/batches/B01/recompute")
    assert r.status_code == 200
    # 快照在同一读事务取得、之后无换版 -> 成功，两稿都更新并清标记
    rep = r.json()
    assert rep["pending_manuscripts"] == []
    assert [m["reference_revision"] for m in rep["manuscripts"]] == [1, 1]
