"""装配提交边界（并发 / 原子性 / 多实例）回归测试。

本轮把全部装配写操作（录入/删除碎片、采纳/撤销关系、试拼确认、轮廓修订
确认、锚点校准确认、历史恢复）收束到可复用的装配提交边界
（DB.assembly_commit，见 database.py），本文件验证其承诺：

1. 成功回执中的 version 与 assembly 必须取自提交事务内的提交时刻，
   与本次提交严格对应（assembly.version == version，快照版本存在且内容
   与回执装配一致）；
2. 业务条件失败 / 版本过期一律整体回滚：不留下部分碎片 / 关系 / 分组
   状态、不递增版本、不写快照；
3. 预览（关系 / 试拼 / 修订 / 校准 / 恢复 / 导入）不写库；
4. 多个服务实例（这里用两个独立的 DB 连接对象模拟）共用同一 SQLite 文件
   时，写事务在 SQLite 写锁层按提交顺序串行，后提交者携带旧期望版本必须
   被判 409 过期；一先一后串行提交则各自成功、版本连续；
5. 迁移导入不经装配提交边界：原子保留包内原版本与快照、不额外递增。
"""
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.database import DB


SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]
# 演示 A/B 两片：采纳后 B 整体移到 (34,0)，A 右缘与 B 左缘共边接触
MARKERS_A = [[35, 0], [38, 15], [33, 30]]
MARKERS_B = [[1, 0], [4, 15], [-1, 30]]

# 本模块使用独立库路径；记录导入本模块前的全局环境，teardown 时还原，
# 避免与同进程后续测试模块（test_gaps / test_repair 等在模块级设置
# DATABASE_PATH）串库 —— 与 tests/test_transfer.py 的隔离方式一致。
_PREV_ENV_DB = os.environ.get("DATABASE_PATH")
_fd, _OWN_DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.unlink(_OWN_DB)


@pytest.fixture(autouse=True)
def _cleanup_db():
    """每个用例独立全新库（不播演示数据，手工装配，版本从 0 空白基线起）。"""
    main.db and main.db.conn.close()
    if os.path.exists(_OWN_DB):
        os.unlink(_OWN_DB)
    main.DB_PATH = _OWN_DB
    main.SEED_DEMO_DATA = False
    with TestClient(app) as c:
        c.db_path = _OWN_DB
        yield c
    if main.db is not None:
        main.db.conn.close()
        main.db = None
    if os.path.exists(_OWN_DB):
        os.unlink(_OWN_DB)
    # 还原全局环境，供同进程后续模块的 lifespan 按其自身 DATABASE_PATH 建连
    if _PREV_ENV_DB is not None:
        os.environ["DATABASE_PATH"] = _PREV_ENV_DB


# ---------------------------------------------------------------- 工具

def _add_fragment(c, name, contour=SQUARE):
    r = c.post("/api/fragments", json={"name": name, "contour": contour})
    assert r.status_code == 201, r.text
    return r.json()


def _two_squares(c):
    """录入两片远处方片（v1/v2），返回 (client, 当前版本)。"""
    _add_fragment(c, "甲片")
    b = _add_fragment(c, "乙片")
    return c, b["version"]


def _accept_ab(c, expected):
    return c.post("/api/relations", json={
        "fragment_a": 1, "fragment_b": 2,
        "markers_a": MARKERS_A, "markers_b": MARKERS_B,
        "expected_version": expected,
    })


def _versions(c):
    return [v["version"] for v in
            c.get("/api/assembly/versions").json()["versions"]]


# ---------------------------------------------------------- 回执与本次提交一致

def test_create_receipt_version_matches_inside_transaction(_cleanup_db):
    c = _cleanup_db
    b = _add_fragment(c, "回执片")
    assert b["version"] == 1
    # 录入回执补带 assembly（新增字段，不影响既有 fragment/version 字段），
    # 其版本必须与回执 version 一致且确为本次提交后的装配
    assert b["assembly"]["version"] == 1
    assert len(b["assembly"]["fragments"]) == 1
    assert b["assembly"]["fragments"][0]["id"] == b["fragment"]["id"]
    assert c.get("/api/assembly").json() == b["assembly"]
    # 空白 v0 基线仍在，本次新增 v1 快照（新版本在前）
    assert _versions(c) == [1, 0]


def test_accept_receipt_assembly_is_committed_state(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    r = _accept_ab(c, v)
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["version"] == v + 1
    # 回执装配就是提交时刻的库内装配，且其内嵌版本与回执 version 相同
    assert b["assembly"]["version"] == b["version"]
    assert c.get("/api/assembly").json() == b["assembly"]
    # 快照已在同一事务按新版本固化，快照状态与回执装配逐对象一致
    snap = c.get(f"/api/assembly/versions/{b['version']}").json()
    assert snap["version"] == b["version"] and snap["source"] == "accept"
    assert snap["assembly"]["relations"] == b["assembly"]["relations"]
    assert snap["assembly"]["fragments"] == b["assembly"]["fragments"]


@pytest.mark.parametrize("endpoint", [
    lambda c, v: c.delete("/api/fragments/2",
                          params={"expected_version": v}),
    lambda c, v: c.post("/api/relations/1/undo",
                        json={"expected_version": v}),
])
def test_write_endpoints_require_current_version(_cleanup_db, endpoint):
    """删除 / 撤销携带过期期望版本时一律 409，且不产生部分状态。"""
    c, v = _two_squares(_cleanup_db)
    r = _accept_ab(c, v)
    assert r.status_code == 201
    stale = v          # 采纳后当前版本为 v+1，v 已过期
    assert endpoint(c, stale).status_code == 409


def test_undo_receipt_consistency(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    v = _accept_ab(c, v).json()["version"]
    r = c.post("/api/relations/1/undo", json={"expected_version": v})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["version"] == v + 1 and b["assembly"]["version"] == b["version"]
    assert c.get("/api/assembly").json() == b["assembly"]
    assert b["assembly"]["fragments"][0]["group_id"] is None
    assert b["assembly"]["groups"] == []


def test_delete_receipt_consistency(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    r = c.delete("/api/fragments/2", params={"expected_version": v})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["version"] == v + 1 and b["assembly"]["version"] == b["version"]
    assert c.get("/api/assembly").json() == b["assembly"]
    assert [f["id"] for f in b["assembly"]["fragments"]] == [1]


def test_trial_commit_receipt_consistency(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    r = c.post("/api/trials/commit", json={
        "expected_version": v,
        "steps": [{
            "action": "relate", "fragment_a": 1, "fragment_b": 2,
            "markers_a": MARKERS_A, "markers_b": MARKERS_B,
            "client_id": "s1",
        }],
    })
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["version"] == v + 1
    assert b["committed_steps"] == 1 and b["relation_ids"] == {"s1": 1}
    assert b["assembly"]["version"] == b["version"]
    assert c.get("/api/assembly").json() == b["assembly"]


# ---------------------------------------------------------- 失败 / 过期原子性

def _state_fingerprint(c):
    asm = c.get("/api/assembly").json()
    return (asm["version"],
            len(asm["fragments"]), len(asm["relations"]),
            json.dumps(asm["groups"], sort_keys=True),
            _versions(c))


def test_rejected_accept_leaves_nothing(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    before = _state_fingerprint(c)
    # 标记误差超限 → 整次拒绝
    r = c.post("/api/relations", json={
        "fragment_a": 1, "fragment_b": 2,
        "markers_a": [[35, 0], [38, 15]],
        "markers_b": [[1, 0], [9, 9]],
        "expected_version": v,
    })
    assert r.status_code == 400
    assert _state_fingerprint(c) == before          # 状态/版本/快照都不变


def test_stale_accept_409_and_no_change(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    # 先做一次无关录入把版本推进，使 v 过期
    nv = _add_fragment(c, "丙片")["version"]
    assert nv == v + 1
    before = _state_fingerprint(c)
    r = _accept_ab(c, v)
    assert r.status_code == 409
    assert "版本冲突" in r.json()["detail"]
    assert _state_fingerprint(c) == before


def test_trial_failed_step_leaves_nothing(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    before = _state_fingerprint(c)
    r = c.post("/api/trials/commit", json={
        "expected_version": v,
        "steps": [
            {"action": "relate", "fragment_a": 1, "fragment_b": 2,
             "markers_a": MARKERS_A, "markers_b": MARKERS_B},
            # 第二步撤销一个根本不存在的关系 → 整序列回滚
            {"action": "undo", "relation_id": 999},
        ],
    })
    assert r.status_code == 404
    assert _state_fingerprint(c) == before          # 第一步也未落库


def test_restore_missing_target_leaves_nothing(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    before = _state_fingerprint(c)
    r = c.post("/api/assembly/versions/999/restore/confirm",
               json={"expected_version": v})
    assert r.status_code == 404
    assert _state_fingerprint(c) == before


def test_undo_missing_relation_leaves_nothing(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    before = _state_fingerprint(c)
    r = c.post("/api/relations/42/undo", json={"expected_version": v})
    assert r.status_code == 404
    assert _state_fingerprint(c) == before
    # 版本仍过期语义不变
    r = c.post("/api/relations/42/undo", json={"expected_version": v + 999})
    assert r.status_code == 409


def test_delete_missing_fragment_leaves_nothing(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    before = _state_fingerprint(c)
    r = c.delete("/api/fragments/77", params={"expected_version": v})
    assert r.status_code == 404
    assert _state_fingerprint(c) == before


# ---------------------------------------------------------- 预览不留状态

def test_previews_are_read_only(_cleanup_db):
    c, v = _two_squares(_cleanup_db)
    before = _state_fingerprint(c)
    c.post("/api/relations/preview", json={
        "fragment_a": 1, "fragment_b": 2,
        "markers_a": MARKERS_A, "markers_b": MARKERS_B})
    c.post("/api/trials/rehearse", json={
        "expected_version": v,
        "steps": [{"action": "relate", "fragment_a": 1, "fragment_b": 2,
                   "markers_a": MARKERS_A, "markers_b": MARKERS_B}]})
    c.post("/api/fragments/1/revision/preview", json={
        "contour": SQUARE, "relations": [], "expected_version": v})
    c.post("/api/calibration/preview",
           json={"anchor_id": 1, "expected_version": v})
    c.post("/api/assembly/versions/1/restore/preview",
           json={"expected_version": v})
    assert _state_fingerprint(c) == before


# ---------------------------------------------------------- 多实例并发

def test_two_instances_serialized_by_commit_order(_cleanup_db):
    """两个服务实例（独立 DB 连接）共用同一库：同基版并发采纳，一个成功
    一个 409；成功方回执属于其本次提交，库内只递增一次版本。"""
    c = _cleanup_db
    c, v = _two_squares(c)
    assert v == 2
    c2_db = DB(c.db_path)                       # 模拟第二个服务实例
    barrier = threading.Barrier(2)
    results = {}

    def accept(instance_name, d):
        # 两线程都在提交前对齐，再各自开写事务，最大化抢锁竞争
        barrier.wait()
        from app.database import VersionConflict
        try:
            with d.assembly_commit("accept", 2) as cb:
                fa, fb = d.get_fragment(1), d.get_fragment(2)
                from app.trial import evaluate_proposal
                ev = evaluate_proposal(d, fa, fb, MARKERS_A, MARKERS_B)
                if not ev["ok"]:
                    raise AssertionError(ev["conflicts"])
                for fid, pose in ev["new_poses"].items():
                    d.update_pose(fid, pose)
                d.insert_relation(1, 2, ev["markers"][0], ev["markers"][1],
                                  max(ev["errors"]))
                cb.result["rid"] = 1
            results[instance_name] = ("ok", cb.new_version)
        except VersionConflict as e:
            results[instance_name] = ("conflict", e.current)

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(accept, "inst1", main.db)
            f2 = ex.submit(accept, "inst2", c2_db)
            f1.result(); f2.result()
    finally:
        c2_db.conn.close()

    statuses = {k: v[0] for k, v in results.items()}
    assert sorted(statuses.values()) == ["conflict", "ok"]
    winner = next(k for k, st in results.items() if st[0] == "ok")
    assert results[winner][1] == 3                 # v2 → v3，仅一次递增
    asm = c.get("/api/assembly").json()
    assert asm["version"] == 3
    assert len(asm["relations"]) == 1             # 败者事务完全回滚
    # v0 空白基线不随提交变化；本次仅新增 v3 快照（v1/v2 为录入）
    assert _versions(c) == [3, 2, 1, 0]


def test_two_instances_sequential_commits_both_succeed(_cleanup_db):
    """两实例串行（后者基于前者的新版本）提交：各自成功、版本连续。"""
    c = _cleanup_db
    c, v = _two_squares(c)
    assert v == 2
    other = DB(c.db_path)
    try:
        # 实例 1 在 v2 上采纳 → v3
        with other.write_tx():
            other.check_version(2)
            fa, fb = other.get_fragment(1), other.get_fragment(2)
            from app.trial import evaluate_proposal
            ev = evaluate_proposal(other, fa, fb, MARKERS_A, MARKERS_B)
            assert ev["ok"]
            for fid, pose in ev["new_poses"].items():
                other.update_pose(fid, pose)
            other.insert_relation(1, 2, ev["markers"][0], ev["markers"][1],
                                  max(ev["errors"]))
            other.recompute_groups()
            v = other.bump_version()
            other.save_snapshot(v, "accept", other.assembly_state())
        assert v == 3

        # 主实例在 v3 上撤销 → v4（读事务已能看到对方已提交版本）
        r = c.post("/api/relations/1/undo", json={"expected_version": 3})
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 4
        assert c.get("/api/assembly").json()["version"] == 4
    finally:
        other.conn.close()


def test_concurrent_http_writes_only_one_wins(_cleanup_db):
    """HTTP 层并发：两个同期望版本的采纳请求同时到达，恰一个 201 一个 409。"""
    c, v = _two_squares(_cleanup_db)
    assert v == 2
    barrier = threading.Barrier(2)

    def fire():
        barrier.wait()
        return _accept_ab(c, 2)

    with ThreadPoolExecutor(max_workers=2) as ex:
        rs = list(ex.map(lambda _: fire(), range(2)))
    codes = sorted(r.status_code for r in rs)
    assert codes == [201, 409]
    ok = next(r for r in rs if r.status_code == 201).json()
    assert ok["version"] == 3 and ok["assembly"]["version"] == 3
    assert c.get("/api/assembly").json() == ok["assembly"]
    assert len(c.get("/api/relations").json()) == 1


# ---------------------------------------------------------- 导入不额外递增

def test_import_preserves_package_versions_without_extra_bump(_cleanup_db):
    """空白库迁移导入：包内原版本与快照原子保留，不额外递增版本/快照。"""
    c = _cleanup_db
    # 当前 fixture 库本就是空白 v0 基线；先另造一份含 v1/v2/v3 的包：
    # 直接经 HTTP 在本库造好后导出，再把库重置回空白 v0 基线。
    _add_fragment(c, "甲片")
    _add_fragment(c, "乙片")
    r = _accept_ab(c, 2)
    assert r.status_code == 201 and r.json()["version"] == 3
    pkg = c.get("/api/transfer/export").json()
    assert pkg["current_version"] == 3

    # 把应用连接重置为全新空白库（v0 空白基线），模拟另一台空白工作站
    blank_path = c.db_path + ".blank"
    if os.path.exists(blank_path):
        os.unlink(blank_path)
    main.db.conn.close()
    main.db = main.DB(blank_path)
    main.ensure_baseline(main.db)
    dst = TestClient(app)                       # 不进 lifespan，复用全局 main.db
    try:
        assert dst.get("/api/assembly").json()["version"] == 0
        r = dst.post("/api/transfer/import/confirm",
                     json={"package": pkg, "expected_version": 0})
        assert r.status_code == 201, r.text
        b = r.json()
        assert b["imported_version"] == 3          # 包内原版本，未额外递增
        assert b["snapshot_count"] == 4            # 包内 v0 空白基线 + v1/v2/v3
        assert b["assembly"]["version"] == 3
        # 目标的 v0 空白基线被整包替换为包内原版本号快照（含包内 v0）
        assert _versions(dst) == [3, 2, 1, 0]
        # 回执装配即导入事务提交时刻状态
        assert dst.get("/api/assembly").json() == b["assembly"]
    finally:
        os.unlink(blank_path)


def test_import_stale_version_409_is_serialized(_cleanup_db):
    """导入的期望版本同样在写事务内复核：他实例先提交后，旧期望版本 → 409。"""
    c = _cleanup_db
    pkg = c.get("/api/transfer/export").json()     # 空白 v0 包
    other = DB(c.db_path)
    try:
        # 他实例先在库上写入一次（模拟目标库已被他人变更）
        with other.write_tx():
            other.check_version(0)
            fid = other.insert_fragment("他人碎片", SQUARE)
            other.recompute_groups()
            nv = other.bump_version()
            other.save_snapshot(nv, "create", other.assembly_state())
        assert fid == 1 and nv == 1
        # 主实例仍持 expected_version=0 → 409，且库与历史保持他实例提交后原状
        r = c.post("/api/transfer/import/confirm",
                   json={"package": pkg, "expected_version": 0})
        assert r.status_code == 409
        asm = c.get("/api/assembly").json()
        assert asm["version"] == 1
        assert [f["name"] for f in asm["fragments"]] == ["他人碎片"]
    finally:
        other.conn.close()
