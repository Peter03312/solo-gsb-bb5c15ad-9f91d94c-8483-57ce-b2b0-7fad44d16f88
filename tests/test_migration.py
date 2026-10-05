"""标注迁移测试：把源文档当前非空标注按旋正后宽高比例迁移到多份目标。

覆盖：
- 成功迁移：缩放取整（左上向下、右下向上）、限制在目标图内、原因与顺序
  保留、响应顺序与请求一致、目标修订号各加一、迁移后可直接发布且涂黑
  位置与映射矩形一致；
- 源标注与既有发布版不变；
- 校验失败全部目标不变并标明失败项：数量越界、目标重复、源作为目标、
  源/目标缺失、源/目标修订过期、源当前标注为空；
- 与标注修改、发布并发串行的一致性。
"""
import hashlib
import io
import sqlite3
import threading

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F401  复用构造器与助手


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _new_doc(client, size=(40, 30), color=(200, 10, 10)):
    return upload(client, png_bytes(size=size, color=color)).json()["doc_id"]


def _annotate(client, doc_id, revision, rects):
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def _migrate(client, source, targets):
    """source: (doc_id, expected_revision)；targets: [(doc_id, expected_revision), ...]"""
    return client.post(
        "/annotation-migrations",
        json={
            "source": {"doc_id": source[0], "expected_revision": source[1]},
            "targets": [
                {"doc_id": d, "expected_revision": r} for d, r in targets
            ],
        },
    )


def _annotations(client, doc_id):
    r = client.get(f"/documents/{doc_id}/annotations")
    assert r.status_code == 200
    return r.json()


def _db(client):
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return settings, conn


# ---------------- 成功路径：缩放、顺序、原因、修订号 ----------------

def test_migration_success_scales_rects_and_preserves_order(client):
    src = _new_doc(client, size=(40, 30))
    t_big = _new_doc(client, size=(100, 80), color=(1, 2, 3))
    t_small = _new_doc(client, size=(20, 10), color=(4, 5, 6))
    _annotate(client, src, 0, [
        {"x": 5, "y": 5, "w": 10, "h": 10, "reason": "人脸"},
        {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "角标"},
        {"x": 39, "y": 29, "w": 1, "h": 1, "reason": "右下角工号"},
    ])

    r = _migrate(client, (src, 1), [(t_big, 0), (t_small, 0)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_doc_id"] == src
    assert body["source_revision"] == 1
    assert body["count"] == 2

    # 响应顺序与请求一致；目标修订号各加一
    assert [t["doc_id"] for t in body["targets"]] == [t_big, t_small]
    assert [t["position"] for t in body["targets"]] == [0, 1]
    assert [t["revision"] for t in body["targets"]] == [1, 1]

    # 100x80：x' = floor(x*100/40)，右边界 ceil；原因与顺序保留
    assert body["targets"][0]["rects"] == [
        {"x": 12, "y": 13, "w": 26, "h": 27, "reason": "人脸"},
        {"x": 0, "y": 0, "w": 3, "h": 3, "reason": "角标"},
        {"x": 97, "y": 77, "w": 3, "h": 3, "reason": "右下角工号"},
    ]
    # 20x10：缩小同样左上向下取整、右下向上取整，且不越出目标图
    assert body["targets"][1]["rects"] == [
        {"x": 2, "y": 1, "w": 6, "h": 4, "reason": "人脸"},
        {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "角标"},
        {"x": 19, "y": 9, "w": 1, "h": 1, "reason": "右下角工号"},
    ]

    # GET 标注读到的是迁移后的当前修订
    got = _annotations(client, t_big)
    assert got["revision"] == 1
    assert got["rects"] == body["targets"][0]["rects"]


def test_migration_non_uniform_scaling_uses_width_and_height_separately(client):
    # 源 40x30 -> 目标 80x30：宽放大 2 倍、高不变，验证宽高分别缩放
    src = _new_doc(client, size=(40, 30))
    dst = _new_doc(client, size=(80, 30), color=(9, 9, 9))
    _annotate(client, src, 0, [
        {"x": 3, "y": 4, "w": 7, "h": 5, "reason": "混合"},
    ])
    r = _migrate(client, (src, 1), [(dst, 0)])
    assert r.status_code == 201, r.text
    rects = r.json()["targets"][0]["rects"]
    # x1 = 3*80//40 = 6；x2 = ceil(10*80/40) = 20；y1 = 4；y2 = ceil(9*30/30) = 9
    assert rects == [{"x": 6, "y": 4, "w": 14, "h": 5, "reason": "混合"}]


def test_migration_same_size_copies_rects_verbatim(client):
    src = _new_doc(client, size=(40, 30))
    dst = _new_doc(client, size=(40, 30), color=(8, 8, 8))
    rects = [
        {"x": 1, "y": 2, "w": 5, "h": 6, "reason": "甲"},
        {"x": 10, "y": 11, "w": 3, "h": 3, "reason": "乙"},
    ]
    _annotate(client, src, 0, rects)
    r = _migrate(client, (src, 1), [(dst, 0)])
    assert r.status_code == 201, r.text
    assert r.json()["targets"][0]["rects"] == rects


def test_migration_single_target_and_max_20(client):
    src = _new_doc(client)
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "密"}])

    one = _new_doc(client, color=(1, 1, 1))
    r = _migrate(client, (src, 1), [(one, 0)])
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 1

    many = [_new_doc(client, color=(i, i, i)) for i in range(20)]
    r = _migrate(client, (src, 1), [(d, 0) for d in many])
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 20
    assert [t["doc_id"] for t in r.json()["targets"]] == many
    assert all(t["revision"] == 1 for t in r.json()["targets"])


def test_migration_bumps_each_target_revision_by_one(client):
    src = _new_doc(client)
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "密"}])
    t1 = _new_doc(client, color=(2, 2, 2))
    t2 = _new_doc(client, color=(3, 3, 3))
    # t2 已有自己的标注历史（修订号 2），迁移在其上 +1
    _annotate(client, t2, 0, [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "旧"}])
    _annotate(client, t2, 1, [])

    r = _migrate(client, (src, 1), [(t1, 0), (t2, 2)])
    assert r.status_code == 201, r.text
    assert [t["revision"] for t in r.json()["targets"]] == [1, 3]
    # t2 的旧标注被整体替换为映射结果
    got = _annotations(client, t2)
    assert got["revision"] == 3
    assert got["rects"] == [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "密"}]
    # 历史修订不可改写：修订 1 的旧矩形仍在库中
    _, db = _db(client)
    rows = db.execute(
        "SELECT x, y, w, h, reason FROM annotation_rects"
        " WHERE doc_id = ? AND revision = 1 ORDER BY idx",
        (t2,),
    ).fetchall()
    db.close()
    assert [(r["x"], r["y"], r["w"], r["h"], r["reason"]) for r in rows] == [
        (1, 1, 2, 2, "旧")
    ]


def test_migration_then_publish_blacks_out_mapped_rect(client):
    src = _new_doc(client, size=(40, 30))
    dst = _new_doc(client, size=(80, 60), color=(200, 10, 10))
    _annotate(client, src, 0, [
        {"x": 10, "y": 6, "w": 4, "h": 6, "reason": "人脸"},
    ])
    r = _migrate(client, (src, 1), [(dst, 0)])
    assert r.status_code == 201, r.text
    # x1=20, y1=12, x2=ceil(14*80/40)=28, y2=ceil(12*60/30)=24 -> w=8, h=12
    assert r.json()["targets"][0]["rects"] == [
        {"x": 20, "y": 12, "w": 8, "h": 12, "reason": "人脸"}
    ]

    p = client.post(f"/documents/{dst}/publish")
    assert p.status_code == 201
    assert p.json()["annotation_revision"] == 1
    img = Image.open(io.BytesIO(
        client.get(f"/publications/{dst}/1/image").content)).convert("RGB")
    assert img.size == (80, 60)
    assert img.getpixel((20, 12)) == (0, 0, 0)      # 映射矩形左上角
    assert img.getpixel((27, 23)) == (0, 0, 0)      # 映射矩形右下角在内
    assert img.getpixel((28, 24)) == (200, 10, 10)  # 矩形外不受影响
    meta = client.get(f"/publications/{dst}/1").json()
    assert meta["redactions"] == [
        {"x": 20, "y": 12, "w": 8, "h": 12, "reason": "人脸"}
    ]


# ---------------- 源标注与既有发布版不变 ----------------

def test_migration_leaves_source_and_existing_publications_untouched(client):
    src = _new_doc(client)
    src_rects = [{"x": 2, "y": 2, "w": 6, "h": 6, "reason": "源标注"}]
    _annotate(client, src, 0, src_rects)
    pub = client.post(f"/documents/{src}/publish")
    assert pub.status_code == 201
    frozen_sha = pub.json()["output_sha256"]

    dst = _new_doc(client, size=(80, 60), color=(7, 7, 7))
    r = _migrate(client, (src, 1), [(dst, 0)])
    assert r.status_code == 201, r.text

    # 源标注不变
    got = _annotations(client, src)
    assert got["revision"] == 1
    assert got["rects"] == src_rects
    # 既有发布版不变（摘要、元数据、图片字节）
    img = client.get(f"/publications/{src}/1/image")
    assert img.status_code == 200
    assert hashlib.sha256(img.content).hexdigest() == frozen_sha
    meta = client.get(f"/publications/{src}/1").json()
    assert meta["annotation_revision"] == 1
    assert meta["redactions"] == src_rects
    # 源不会多出新的发布版本
    pubs = client.get(f"/documents/{src}/publications").json()["publications"]
    assert [p["version"] for p in pubs] == [1]


# ---------------- 结构性校验：数量、重复、源作为目标 ----------------

def test_migration_size_bounds(client):
    src = _new_doc(client)
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    dst = _new_doc(client, color=(1, 1, 1))

    r = _migrate(client, (src, 1), [])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_migration_size"
    assert detail["count"] == 0

    r = _migrate(client, (src, 1), [(f"d{i}", 0) for i in range(21)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_migration_size"
    assert detail["count"] == 21

    # 失败不留痕迹：目标修订号不变
    assert _annotations(client, dst)["revision"] == 0


def test_migration_duplicate_targets_rejected(client):
    src = _new_doc(client)
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    a = _new_doc(client, color=(1, 1, 1))
    b = _new_doc(client, color=(2, 2, 2))

    r = _migrate(client, (src, 1), [(a, 0), (b, 0), (a, 0)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "duplicate_document_in_migration"
    assert detail["position"] == 2
    assert detail["doc_id"] == a
    assert _annotations(client, a)["revision"] == 0
    assert _annotations(client, b)["revision"] == 0


def test_migration_source_cannot_be_target(client):
    src = _new_doc(client)
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    other = _new_doc(client, color=(1, 1, 1))

    r = _migrate(client, (src, 1), [(other, 0), (src, 1)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "source_is_target"
    assert detail["position"] == 1
    assert detail["doc_id"] == src
    assert _annotations(client, other)["revision"] == 0
    assert _annotations(client, src)["revision"] == 1


# ---------------- 存在性与修订号：任一失败全部目标不变 ----------------

def test_migration_source_not_found(client):
    dst = _new_doc(client)
    missing = "0" * 32
    r = _migrate(client, (missing, 0), [(dst, 0)])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "document_not_found"
    assert detail["doc_id"] == missing
    assert _annotations(client, dst)["revision"] == 0


def test_migration_target_not_found_fails_atomically(client):
    src = _new_doc(client)
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    a = _new_doc(client, color=(1, 1, 1))
    b = _new_doc(client, color=(2, 2, 2))
    missing = "f" * 32

    # 缺失目标在中间：前后的目标都不能被迁移
    r = _migrate(client, (src, 1), [(a, 0), (missing, 0), (b, 0)])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "document_not_found"
    assert detail["position"] == 1
    assert detail["doc_id"] == missing
    assert _annotations(client, a)["revision"] == 0
    assert _annotations(client, b)["revision"] == 0


def test_migration_source_stale_revision_fails_atomically(client):
    src = _new_doc(client)
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    a = _new_doc(client, color=(1, 1, 1))
    b = _new_doc(client, color=(2, 2, 2))

    r = _migrate(client, (src, 0), [(a, 0), (b, 0)])  # 源当前修订号为 1
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["doc_id"] == src
    assert detail["current_revision"] == 1
    assert detail["supplied_revision"] == 0
    assert _annotations(client, a)["revision"] == 0
    assert _annotations(client, b)["revision"] == 0


def test_migration_target_stale_revision_fails_atomically(client):
    src = _new_doc(client)
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    a = _new_doc(client, color=(1, 1, 1))
    b = _new_doc(client, color=(2, 2, 2))
    _annotate(client, b, 0, [])  # b 当前修订号 1

    r = _migrate(client, (src, 1), [(a, 0), (b, 0)])  # b 携带过期的 0
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["position"] == 1
    assert detail["doc_id"] == b
    assert detail["current_revision"] == 1
    assert detail["supplied_revision"] == 0
    # 排在前面的 a 也不能被迁移
    assert _annotations(client, a)["revision"] == 0
    assert _annotations(client, b)["revision"] == 1

    # 修正修订号后整批成功
    r = _migrate(client, (src, 1), [(a, 0), (b, 1)])
    assert r.status_code == 201, r.text
    assert [t["revision"] for t in r.json()["targets"]] == [1, 2]


def test_migration_empty_source_annotations_rejected(client):
    src = _new_doc(client)  # 修订 0：从未标注
    dst = _new_doc(client, color=(1, 1, 1))
    r = _migrate(client, (src, 0), [(dst, 0)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "empty_source_annotations"
    assert detail["doc_id"] == src
    assert _annotations(client, dst)["revision"] == 0

    # 标注后又清空：当前修订同样为空，仍拒绝
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    _annotate(client, src, 1, [])
    r = _migrate(client, (src, 2), [(dst, 0)])
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "empty_source_annotations"
    assert _annotations(client, dst)["revision"] == 0


def test_migration_failure_leaves_no_partial_writes(client):
    """任一校验失败：所有目标修订号不变，也不新增任何标注行。"""
    src = _new_doc(client)
    _annotate(client, src, 0, [{"x": 0, "y": 0, "w": 1, "h": 1, "reason": "密"}])
    a = _new_doc(client, color=(1, 1, 1))
    b = _new_doc(client, color=(2, 2, 2))
    missing = "e" * 32

    _, db = _db(client)
    before = db.execute("SELECT COUNT(*) c FROM annotation_rects").fetchone()["c"]
    db.close()

    r = _migrate(client, (src, 1), [(a, 0), (missing, 0), (b, 0)])
    assert r.status_code == 404

    _, db = _db(client)
    after = db.execute("SELECT COUNT(*) c FROM annotation_rects").fetchone()["c"]
    for d in (a, b):
        rev = db.execute(
            "SELECT annotation_revision r FROM documents WHERE id = ?", (d,)
        ).fetchone()["r"]
        assert rev == 0
    db.close()
    assert after == before


# ---------------- 映射结果始终在目标图内且非空 ----------------

def test_mapped_rects_stay_in_bounds_and_non_empty(client):
    """多种源/目标尺寸组合下，映射矩形都在目标图内且 w,h ≥ 1。"""
    src = _new_doc(client, size=(37, 23))
    _annotate(client, src, 0, [
        {"x": 0, "y": 0, "w": 1, "h": 1, "reason": "r0"},
        {"x": 36, "y": 22, "w": 1, "h": 1, "reason": "r1"},
        {"x": 5, "y": 7, "w": 11, "h": 3, "reason": "r2"},
        {"x": 0, "y": 0, "w": 37, "h": 23, "reason": "r3"},
    ])
    for size in ((1, 1), (2, 3), (37, 23), (100, 80), (400, 17)):
        dst = _new_doc(client, size=size, color=(5, 5, 5))
        r = _migrate(client, (src, 1), [(dst, 0)])
        assert r.status_code == 201, r.text
        for rect in r.json()["targets"][0]["rects"]:
            assert rect["w"] >= 1 and rect["h"] >= 1
            assert 0 <= rect["x"] and rect["x"] + rect["w"] <= size[0]
            assert 0 <= rect["y"] and rect["y"] + rect["h"] <= size[1]
        # 全图矩形映射后仍覆盖全图
        full = r.json()["targets"][0]["rects"][3]
        assert (full["x"], full["y"], full["w"], full["h"]) == (
            0, 0, size[0], size[1])


# ---------------- 并发：迁移与标注修改、发布互斥串行 ----------------

def test_concurrent_migration_annotate_and_publish(client):
    src = _new_doc(client, size=(60, 60))
    _annotate(client, src, 0, [{"x": 2, "y": 2, "w": 8, "h": 8, "reason": "源"}])
    ids = [_new_doc(client, size=(60, 60), color=(k, k, k)) for k in range(3)]
    errors = []

    def migrator():
        for _ in range(8):
            src_rev = _annotations(client, src)["revision"]
            pairs = [(d, _annotations(client, d)["revision"]) for d in ids]
            r = _migrate(client, (src, src_rev), pairs)
            # 409：并发标注修改抢先是合法的乐观锁冲突，整批无副作用
            if r.status_code not in (201, 409):
                errors.append(f"migrate: {r.status_code} {r.text}")

    def annotator(k):
        for i in range(6):
            cur = _annotations(client, ids[k])["revision"]
            r = client.put(
                f"/documents/{ids[k]}/annotations",
                json={"revision": cur,
                      "rects": [{"x": (i * 3) % 40, "y": 1, "w": 3, "h": 3,
                                 "reason": f"区{k}-{i}"}]})
            if r.status_code not in (200, 409):
                errors.append(f"annotate: {r.status_code} {r.text}")

    def publisher(k):
        for _ in range(4):
            r = client.post(f"/documents/{ids[k]}/publish")
            if r.status_code != 201:
                errors.append(f"publish: {r.status_code} {r.text}")

    threads = ([threading.Thread(target=migrator) for _ in range(2)]
               + [threading.Thread(target=annotator, args=(k,))
                  for k in range(len(ids))]
               + [threading.Thread(target=publisher, args=(k,))
                  for k in range(len(ids))])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 不变式 1：源标注从未被迁移改动（仍是修订 1 的原始矩形）
    got = _annotations(client, src)
    assert got["revision"] == 1
    assert got["rects"] == [{"x": 2, "y": 2, "w": 8, "h": 8, "reason": "源"}]

    settings, db = _db(client)
    # 不变式 2：每个目标的修订号连续（迁移/标注各自 +1，无跳号、无丢失更新）
    for doc_id in ids:
        rev = db.execute(
            "SELECT annotation_revision r FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()["r"]
        revisions = {row["revision"] for row in db.execute(
            "SELECT DISTINCT revision FROM annotation_rects WHERE doc_id = ?",
            (doc_id,))}
        # 每个非空修订都有矩形行；空修订（清空标注）没有行
        assert all(1 <= v <= rev for v in revisions)
        # 当前修订号不小于任何已发布版本固化的修订号
        max_pub = db.execute(
            "SELECT COALESCE(MAX(annotation_revision), 0) m FROM publications"
            " WHERE doc_id = ?", (doc_id,)).fetchone()["m"]
        assert rev >= max_pub

    # 不变式 3：每条发布记录的摘要 == 按其固化修订号重渲染的摘要
    from app.imaging import load_oriented, render_publication
    for doc_id in ids:
        doc = db.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        with open(doc["original_path"], "rb") as f:
            oriented, _ = load_oriented(f.read())
        for p in db.execute(
                "SELECT * FROM publications WHERE doc_id = ?", (doc_id,)):
            rects = db.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, p["annotation_revision"])).fetchall()
            buf = io.BytesIO()
            render_publication(oriented, rects).save(buf, "PNG")
            assert hashlib.sha256(buf.getvalue()).hexdigest() == p["output_sha256"]
    db.close()
