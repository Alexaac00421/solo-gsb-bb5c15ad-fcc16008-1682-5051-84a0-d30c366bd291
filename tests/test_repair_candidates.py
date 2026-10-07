"""补配候选组评估 API（/api/assembly/versions/{v}/repair-candidates）测试。

数据准备（模块级全新库）：
- v1..v5：依次录入 A(#1,30×30)、B(#2,20×20)、B高(#3,20×25)、C(#4,20×20)、
  D(#5,20×20)，全部位于原点且相互独立；
- v6：C(#4)-D(#5) 以共边标记拼接，D 移到 (20,0)，同属连通组 G4；
- v7：录入 F(#6,30×30)；
- v8：A(#1)-F(#6) 拼接，F 移到 (-30,0)（仅共边，目标 x≥0 内不占面积），
  A/F 同属连通组 G1。

目标轮廓统一为世界坐标矩形 (0,0)-(100,60)，面积 6000 mm²；A 在目标内
覆盖 900 mm²。候选组整体平移+旋转后：
- 候选 #2（孤片，左侧→A 右侧）：补到 x∈[30,50]，净新增 400，零误差；
- 候选 #3（孤片，左侧→A 右侧偏上）：净新增 500，零误差；
- 候选 #4（G4 双片）：整组右移，净新增 800，组内相对位姿不变。

全部用例只读评估，不产生新版本。
"""
import os
import tempfile

_fd, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_PATH"] = _DB_PATH
os.environ["SEED_DEMO_DATA"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import candidates as cmod  # noqa: E402
from app import main  # noqa: E402
from app.main import app  # noqa: E402

SQ30 = [[0, 0], [30, 0], [30, 30], [0, 30]]
SQ20 = [[0, 0], [20, 0], [20, 20], [0, 20]]
SQ25 = [[0, 0], [20, 0], [20, 25], [0, 25]]
TARGET = [[0, 0], [100, 0], [100, 60], [0, 60]]

# A 右侧 / 左侧标记；候选片左/右边缘标记
A_RIGHT = [[30, 5], [30, 15]]
A_RIGHT_UP = [[30, 10], [30, 20]]
A_LEFT = [[0, 5], [0, 15]]
B_LEFT = [[0, 5], [0, 15]]
B_RIGHT = [[20, 5], [20, 15]]


@pytest.fixture(scope="module")
def client():
    if os.path.exists(_DB_PATH):
        os.unlink(_DB_PATH)
    if main.db is not None:
        main.db.conn.close()
    main.SEED_DEMO_DATA = False
    main.DB_PATH = _DB_PATH
    with TestClient(app) as c:
        yield c


def _create(client, name, contour):
    r = client.post("/api/fragments", json={"name": name, "contour": contour})
    assert r.status_code == 201, r.text
    return r.json()["version"]


@pytest.fixture(scope="module")
def prepared(client):
    versions = {}
    versions["a"] = _create(client, "陶片A", SQ30)
    versions["b"] = _create(client, "陶片B", SQ20)
    versions["btall"] = _create(client, "陶片B高", SQ25)
    versions["c"] = _create(client, "陶片C", SQ20)
    versions["d"] = _create(client, "陶片D", SQ20)

    r = client.post("/api/relations", json={
        "fragment_a": 4, "fragment_b": 5,
        "markers_a": [[20, 5], [20, 15]], "markers_b": [[0, 5], [0, 15]],
        "expected_version": versions["d"],
    })
    assert r.status_code == 201, r.text
    versions["cd"] = r.json()["version"]

    versions["f"] = _create(client, "陶片F", SQ30)

    r = client.post("/api/relations", json={
        "fragment_a": 1, "fragment_b": 6,
        "markers_a": A_LEFT, "markers_b": [[30, 5], [30, 15]],
        "expected_version": versions["f"],
    })
    assert r.status_code == 201, r.text
    versions["af"] = r.json()["version"]
    versions["latest"] = versions["af"]
    return {"client": client, "v": versions}


def _evaluate(client, version, anchor, target, cands):
    return client.post(
        f"/api/assembly/versions/{version}/repair-candidates",
        json={"anchor_id": anchor, "target_contour": target,
              "candidates": cands})


def _cand(cid, aid, mc, ma):
    return {"candidate_fragment_id": cid, "anchor_fragment_id": aid,
            "markers_candidate": mc, "markers_anchor": ma}


# ---------------------------------------------------------------- 正常评估

def test_basic_candidate_metrics_and_ranking(prepared):
    """两个合格候选：净新增 400(#2) / 500(#3)，按净新增降序排名。"""
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [
        _cand(2, 1, B_LEFT, A_RIGHT),       # 净新增 400
        _cand(3, 1, B_LEFT, A_RIGHT_UP),    # 净新增 500
    ])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True
    assert b["version"] == prepared["v"]["latest"]
    assert b["historical"] is False
    assert b["anchor"] == {"id": 1, "name": "陶片A"}
    assert b["anchor_fragment_ids"] == [1, 6]   # 锚组成员含 F
    assert b["target_area_mm2"] == pytest.approx(6000.0)
    assert b["anchor_covered_area_mm2"] == pytest.approx(900.0)
    assert b["candidate_count"] == 2 and b["eligible_count"] == 2

    rows = {c["candidate_fragment_id"]: c for c in b["candidates"]}
    c2, c3 = rows[2], rows[3]
    assert c2["eligible"] and c2["rank"] == 2
    assert c2["max_error_mm"] == 0.0
    assert c2["pair_errors_mm"] == [0.0, 0.0]
    assert c2["has_internal_overlap"] is False and c2["overlaps"] == []
    assert c2["net_new_coverage_mm2"] == pytest.approx(400.0)
    assert c2["remaining_gap_area_mm2"] == pytest.approx(4700.0)
    # 移动后位姿：B 从原点平移到 (30,0)，无旋转
    pose2 = next(p for p in c2["moved_poses"] if p["fragment_id"] == 2)
    assert pose2["after"]["x"] == 30.0 and pose2["after"]["y"] == 0.0
    assert pose2["after"]["theta_deg"] == 0.0
    assert c2["transform"]["tx_mm"] == 30.0

    assert c3["eligible"] and c3["rank"] == 1
    assert c3["net_new_coverage_mm2"] == pytest.approx(500.0)
    assert c3["remaining_gap_area_mm2"] == pytest.approx(4600.0)
    pose3 = next(p for p in c3["moved_poses"] if p["fragment_id"] == 3)
    assert pose3["after"] == {"x": 30.0, "y": 5.0, "theta_deg": 0.0}

    assert [x["candidate_fragment_id"] for x in b["ranking"]] == [3, 2]
    assert b["ranking"][0]["net_new_coverage_mm2"] == pytest.approx(500.0)
    assert b["ranking"][0]["max_error_mm"] == 0.0


def test_candidate_group_moves_as_rigid_whole(prepared):
    """候选为连通组：组内相对位姿不变，整组平移，净新增为两片之和。"""
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [
        _cand(4, 1, B_LEFT, A_RIGHT),       # G4={#4,#5} 整体右移
    ])
    assert r.status_code == 200, r.text
    b = r.json()
    c4 = b["candidates"][0]
    assert c4["eligible"] and c4["rank"] == 1
    assert c4["candidate_group"] == {"id": 4, "fragment_ids": [4, 5]}
    assert c4["moved_fragment_ids"] == [4, 5]
    poses = {p["fragment_id"]: p for p in c4["moved_poses"]}
    assert poses[4]["after"] == {"x": 30.0, "y": 0.0, "theta_deg": 0.0}
    assert poses[5]["after"] == {"x": 50.0, "y": 0.0, "theta_deg": 0.0}
    # 组内间距 20 mm 保持：before (0,0)/(20,0) → after (30,0)/(50,0)
    assert poses[4]["before"]["x"] == 0.0 and poses[5]["before"]["x"] == 20.0
    assert c4["net_new_coverage_mm2"] == pytest.approx(800.0)
    assert c4["remaining_gap_area_mm2"] == pytest.approx(4300.0)
    assert c4["anchor_covered_area_mm2"] == pytest.approx(900.0)


def test_zero_gain_eligible_candidate_displayed_and_ranked(prepared):
    """候选落在目标范围外：净新增 0，仍合格并参与排名（照实显示）。"""
    far_target = [[200, 0], [300, 0], [300, 60], [200, 60]]
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, far_target, [
        _cand(2, 1, B_LEFT, A_RIGHT),
        _cand(3, 1, B_LEFT, A_RIGHT_UP),
    ])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["anchor_covered_area_mm2"] == 0.0
    for c in b["candidates"]:
        assert c["eligible"] is True
        assert c["net_new_coverage_mm2"] == 0.0
        assert c["candidate_clipped_area_mm2"] == 0.0
        assert c["remaining_gap_area_mm2"] == pytest.approx(6000.0)
    # 净新增并列 0 → 最大误差并列 0 → 候选片编号升序
    assert [x["candidate_fragment_id"] for x in b["ranking"]] == [2, 3]
    assert [c["rank"] for c in b["candidates"]] == [1, 2]


def test_anchor_side_can_be_any_anchor_group_fragment(prepared):
    """锚侧标记片可指定锚组内另一片（#6）：以 #6 的边缘标记拟合。"""
    # #6 在 (-30,0)，其右边缘 x=0；以它为锚侧把候选 B 接到 x=0
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [
        _cand(2, 6, B_LEFT, [[30, 5], [30, 15]]),
    ])
    assert r.status_code == 200, r.text
    c = r.json()["candidates"][0]
    assert c["anchor_fragment_id"] == 6
    assert c["max_error_mm"] == 0.0
    pose = next(p for p in c["moved_poses"] if p["fragment_id"] == 2)
    assert pose["after"]["x"] == 0.0       # B 左缘贴到 #6 右缘（即 x=0）
    # B 占据 x∈[0,20]，与 A(x∈[0,30]) 大面积重叠 → 必报碰撞
    assert c["has_internal_overlap"] is True
    assert c["eligible"] is False
    assert c["rejection_reasons"] == ["internal_overlap"]
    assert c["rank"] is None
    # 与锚片 A(#1) 重叠 20×20=400 mm²（与 #6 仅共边，不计）
    ov = {(o["candidate_fragment_id"], o["anchor_fragment_id"]): o["area_mm2"]
          for o in c["overlaps"]}
    assert ov[(2, 1)] == pytest.approx(400.0)
    assert c["net_new_coverage_mm2"] == 0.0      # 覆盖全在锚组内，无净新增


def test_over_tolerance_candidate_only_itself_marked(prepared):
    """单候选拟合超差：只标记自身，不影响同批其他候选。"""
    # 候选标记间距 20 对锚侧间距 10 → 最小二乘残差各 5 mm
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [
        _cand(2, 1, [[0, 0], [0, 20]], [[30, 10], [30, 20]]),
        _cand(3, 1, B_LEFT, A_RIGHT_UP),
    ])
    assert r.status_code == 200, r.text
    b = r.json()
    bad, good = b["candidates"]
    assert bad["candidate_fragment_id"] == 2
    assert bad["all_pair_errors_within_1mm"] is False
    assert bad["max_error_mm"] == pytest.approx(5.0)
    assert bad["eligible"] is False
    assert bad["rejection_reasons"] == ["error_exceeds_1mm"]
    assert bad["rank"] is None
    # 指标仍照实给出
    assert bad["net_new_coverage_mm2"] >= 0.0
    assert good["candidate_fragment_id"] == 3 and good["eligible"] is True
    assert b["eligible_count"] == 1
    assert [x["candidate_fragment_id"] for x in b["ranking"]] == [3]


def test_collision_candidate_only_itself_marked(prepared):
    """单候选与锚组内部重叠：零误差但标记碰撞，只排除自身。"""
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [
        # 候选右缘贴到锚片右缘 → B 落入 x∈[10,30]，与 A 重叠 400 mm²
        _cand(2, 1, B_RIGHT, A_RIGHT),
        _cand(4, 1, B_LEFT, A_RIGHT),
    ])
    assert r.status_code == 200, r.text
    b = r.json()
    coll, group = b["candidates"]
    assert coll["eligible"] is False
    assert coll["max_error_mm"] == 0.0
    assert coll["rejection_reasons"] == ["internal_overlap"]
    assert coll["has_internal_overlap"] is True
    ov = coll["overlaps"][0]
    assert {ov["candidate_fragment_id"], ov["anchor_fragment_id"]} == {2, 1}
    assert ov["area_mm2"] == pytest.approx(400.0)
    # 同批另一候选不受影响，照常合格排名
    assert group["eligible"] is True and group["rank"] == 1
    assert b["eligible_count"] == 1


def test_historical_version_snapshot_only(prepared):
    """只取指定版本快照：v6 时 F(#6) 尚不存在，锚组也与当前版不同。"""
    # v6：#1 还是孤片，#4/#5 已成组
    r = _evaluate(prepared["client"], prepared["v"]["cd"], 1, TARGET, [
        _cand(4, 1, B_LEFT, A_RIGHT),
    ])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["version"] == prepared["v"]["cd"]
    assert b["historical"] is True
    assert b["anchor_fragment_ids"] == [1]      # v6 时 A/F 尚未拼接
    assert b["candidates"][0]["eligible"] is True


# ---------------------------------------------------------------- 整次拒绝

def test_empty_candidates_rejected(prepared):
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "empty_candidates"
    assert d["errors"][0]["field"] == "candidates"


def test_same_group_candidate_rejected(prepared):
    """候选片属锚组（含锚片自己）：整次拒绝，不执行拟合。"""
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [
        _cand(6, 1, B_LEFT, A_RIGHT),       # #6 与 #1 同属 G1
        _cand(1, 1, B_LEFT, A_RIGHT),       # 锚片自己
    ])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "invalid_candidates"
    fields = {(e["field"], e["code"]) for e in d["errors"]}
    assert ("candidates[0].candidate_fragment_id", "same_group") in fields
    assert ("candidates[1].candidate_fragment_id", "same_group") in fields


def test_anchor_side_fragment_not_in_anchor_group_rejected(prepared):
    r = _evaluate(prepared["client"], prepared["v"]["cd"], 1, TARGET, [
        _cand(2, 3, B_LEFT, B_LEFT),        # v6 时 #1 是孤片，#3 不在锚组
    ])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["errors"][0]["code"] == "anchor_fragment_not_in_anchor_group"
    assert d["errors"][0]["field"] == "candidates[0].anchor_fragment_id"


def test_candidate_fragment_missing_in_version_rejected(prepared):
    """候选陶片在该历史版尚未录入：404 逐字段指出，整次拒绝。"""
    r = _evaluate(prepared["client"], prepared["v"]["cd"], 1, TARGET, [
        _cand(99, 1, B_LEFT, A_RIGHT),
        _cand(6, 1, B_LEFT, A_RIGHT),       # #6 在 v7 才录入，v6 不存在
    ])
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "fragment_not_found"
    fields = [e["field"] for e in d["errors"]]
    assert "candidates[0].candidate_fragment_id" in fields
    assert "candidates[1].candidate_fragment_id" in fields


def test_anchor_not_found_in_version_rejected(prepared):
    r = _evaluate(prepared["client"], prepared["v"]["cd"], 99, TARGET, [
        _cand(2, 99, B_LEFT, A_RIGHT)])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "anchor_not_found"


def test_version_not_found(prepared):
    r = _evaluate(prepared["client"], 9999, 1, TARGET, [
        _cand(2, 1, B_LEFT, A_RIGHT)])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "version_not_found"


def test_invalid_anchor_id_rejected(prepared):
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 0, TARGET, [
        _cand(2, 0, B_LEFT, A_RIGHT)])
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_anchor"


def test_invalid_target_contour_rejected(prepared):
    bowtie = [[0, 0], [10, 10], [10, 0], [0, 10]]
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, bowtie, [
        _cand(2, 1, B_LEFT, A_RIGHT)])
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_target_contour"


def test_marker_not_on_edge_rejected(prepared):
    """标记不在该片该版轮廓边缘：整次拒绝并逐标记指出。"""
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [
        _cand(2, 1, [[5, 5], [15, 5]], A_RIGHT),      # 候选片内部点
        _cand(3, 1, B_LEFT, [[30, 10], [31, 20]]),    # 锚侧第二点越出边缘
    ])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "invalid_candidates"
    codes_fields = [(e["code"], e["field"]) for e in d["errors"]]
    assert ("marker_not_on_edge",
            "candidates[0].markers_candidate[0]") in codes_fields
    assert ("marker_not_on_edge",
            "candidates[0].markers_candidate[1]") in codes_fields
    assert ("marker_not_on_edge",
            "candidates[1].markers_anchor[1]") in codes_fields


def test_marker_count_and_duplicate_and_too_few_collected(prepared):
    """数量不一致 / 同片重合 / 不足 2 处：一次性逐字段全部指出。"""
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [
        _cand(2, 1, [[0, 5]], A_RIGHT),                       # 不足 2 处
        _cand(3, 1, [[0, 5], [0, 5]], [[30, 5], [30, 15]]),   # 候选侧重合
        _cand(4, 1, [[0, 5], [0, 15], [0, 10]], A_RIGHT),     # 两侧数量不一致
    ])
    assert r.status_code == 400
    d = r.json()["detail"]
    pairs = {(e["field"], e["code"]) for e in d["errors"]}
    assert ("candidates[0].markers_candidate", "too_few_markers") in pairs
    assert ("candidates[1].markers_candidate", "duplicate_marker") in pairs
    assert ("candidates[2]", "marker_count_mismatch") in pairs


def test_invalid_fragment_id_collected(prepared):
    r = _evaluate(prepared["client"], prepared["v"]["latest"], 1, TARGET, [
        {"candidate_fragment_id": 0, "anchor_fragment_id": 1,
         "markers_candidate": B_LEFT, "markers_anchor": A_RIGHT},
        {"candidate_fragment_id": 2, "anchor_fragment_id": -1,
         "markers_candidate": B_LEFT, "markers_anchor": A_RIGHT},
    ])
    assert r.status_code == 400
    pairs = {(e["field"], e["code"]) for e in r.json()["detail"]["errors"]}
    assert ("candidates[0].candidate_fragment_id", "invalid_fragment_id") in pairs
    assert ("candidates[1].anchor_fragment_id", "invalid_fragment_id") in pairs


def test_non_finite_marker_rejected(prepared):
    import json as _json
    # 直接发原始 JSON 体（allow_nan），确保 NaN 能到达服务端校验
    body = _json.dumps({
        "anchor_id": 1, "target_contour": TARGET,
        "candidates": [_cand(2, 1, [[0, 5], [0, float("nan")]], A_RIGHT)],
    }, allow_nan=True)
    r = prepared["client"].post(
        f"/api/assembly/versions/{prepared['v']['latest']}/repair-candidates",
        content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "non_finite_coordinate"
    assert d["errors"][0]["field"] == "candidates[0].markers_candidate[1].y"


def test_malformed_marker_point_rejected_400(prepared):
    # 标记点不是二元数值数组：静态校验逐点收集，400 invalid_marker（整次拒绝）
    r = prepared["client"].post(
        f"/api/assembly/versions/{prepared['v']['latest']}/repair-candidates",
        json={"anchor_id": 1, "target_contour": TARGET,
              "candidates": [{"candidate_fragment_id": 2,
                              "anchor_fragment_id": 1,
                              "markers_candidate": [[0]],
                              "markers_anchor": A_RIGHT}]})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "invalid_candidates"
    assert d["errors"][0]["field"] == "candidates[0].markers_candidate[0]"
    assert d["errors"][0]["code"] == "invalid_marker"


def test_missing_candidates_field_422(prepared):
    # 结构问题（缺少 candidates 字段）由 pydantic 422 拒绝
    r = prepared["client"].post(
        f"/api/assembly/versions/{prepared['v']['latest']}/repair-candidates",
        json={"anchor_id": 1, "target_contour": TARGET})
    assert r.status_code == 422


def test_wrong_type_anchor_id_422(prepared):
    r = prepared["client"].post(
        f"/api/assembly/versions/{prepared['v']['latest']}/repair-candidates",
        json={"anchor_id": "x", "target_contour": TARGET,
              "candidates": [_cand(2, 1, B_LEFT, A_RIGHT)]})
    assert r.status_code == 422


# ---------------------------------------------------------------- 只读语义

def test_evaluation_is_read_only(prepared):
    c = prepared["client"]
    v = prepared["v"]["latest"]
    before_asm = c.get("/api/assembly").json()
    before_versions = c.get("/api/assembly/versions").json()
    r = _evaluate(c, v, 1, TARGET, [
        _cand(2, 1, B_LEFT, A_RIGHT),
        _cand(3, 1, B_LEFT, A_RIGHT_UP),
        _cand(2, 1, B_RIGHT, A_RIGHT),
    ])
    assert r.status_code == 200, r.text
    after_asm = c.get("/api/assembly").json()
    after_versions = c.get("/api/assembly/versions").json()
    assert after_asm == before_asm
    assert after_versions == before_versions
    assert after_versions["versions"][0]["version"] == v


# ---------------------------------------------------------------- 纯演算

def test_pure_evaluate_ranking_and_rotation():
    """直接构造快照演算：90° 旋转贴合 + 排名次序与零收益。"""
    snap = {
        "version": 9,
        "fragments": [
            {"id": 1, "name": "A", "contour": [[0, 0], [30, 0], [30, 30], [0, 30]],
             "x": 0.0, "y": 0.0, "theta": 0.0, "group_id": None},
            {"id": 2, "name": "B", "contour": [[0, 0], [20, 0], [20, 20], [0, 20]],
             "x": 0.0, "y": 0.0, "theta": 0.0, "group_id": None},
        ],
        "relations": [], "groups": [],
    }

    class _C:
        def __init__(self, cid, aid, mc, ma):
            self.candidate_fragment_id = cid
            self.anchor_fragment_id = aid
            self.markers_candidate = mc
            self.markers_anchor = ma

    # B 顶边两点 → A 右边两点：+90° 旋转，B 立于 A 右侧，仅共边无内部重叠
    cand = _C(2, 1, [[5, 20], [15, 20]], [[30, 5], [30, 15]])
    ev = cmod.evaluate_candidates(
        snap, 1, [(0, 0), (100, 0), (100, 60), (0, 60)], [cand])
    row = ev["rows"][0]
    assert row["eligible"] is True
    assert row["max_error"] == pytest.approx(0.0, abs=1e-9)
    # T 为 +90°：world' = R(90)·world + (50,0)
    import math
    assert abs(row["transform"][2] - math.pi / 2) < 1e-9
    after = row["after_poses"][2]
    assert after[0] == pytest.approx(50.0, abs=1e-9)
    assert after[1] == pytest.approx(0.0, abs=1e-9)
    assert after[2] == pytest.approx(math.pi / 2, abs=1e-9)
    # 旋转后 B 为 (50,0)-(50,20)-(30,20)-(30,0)：x∈[30,50]，净新增 400
    assert row["net_new_area"] == pytest.approx(400.0, abs=1e-9)
    assert row["rank"] == 1
