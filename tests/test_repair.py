"""补配区域评估 API（/api/assembly/versions/{v}/repair-assessment）测试。

数据准备（模块级，只建一次，沿用 test_gaps 的场景）：
- v1 baseline：4 片演示陶片全部位于原点（相互独立、轮廓相互重叠）；
- v2 accept：A(#1)-B(#2) 以 3 对标记拼接，B 移到 (34, 0, 0)，
  A 右缘与 B 左缘共边（接触，无面积重叠），同属连通组 G1；C(#3)/D(#4)
  仍是独立碎片；
- v3 create：E(#5) 远处孤片 [[100,0],[110,0],[110,10],[100,10]]。

随后全部用例只读评估，不再变更数据。纯演算（evaluate_repair）直接构造
版本快照，覆盖孔洞拓扑、仅共点不合并、排序与裁剪语义。
"""
import os
import tempfile

_fd, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_PATH"] = _DB_PATH
os.environ["SEED_DEMO_DATA"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.main import app  # noqa: E402
from app.repair import evaluate_repair, validate_target  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 显式重置为全新库：与同进程内其他测试模块（test_gaps 等）的库隔离
    if os.path.exists(_DB_PATH):
        os.unlink(_DB_PATH)
    if main.db is not None:
        main.db.conn.close()
    main.SEED_DEMO_DATA = True
    main.DB_PATH = _DB_PATH
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def prepared(client):
    r = client.get("/api/assembly")
    assert r.status_code == 200 and r.json()["version"] == 1
    r = client.post("/api/relations", json={
        "fragment_a": 1, "fragment_b": 2,
        "markers_a": [[35, 0], [38, 15], [33, 30]],
        "markers_b": [[1, 0], [4, 15], [-1, 30]],
        "expected_version": 1,
    })
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2
    r = client.post("/api/fragments", json={
        "name": "陶片E",
        "contour": [[100, 0], [110, 0], [110, 10], [100, 10]],
    })
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 3
    return {"client": client, "v_baseline": 1, "v_accept": 2, "v_create": 3}


def _assess(client, version, anchor, contour):
    return client.post(
        f"/api/assembly/versions/{version}/repair-assessment",
        json={"anchor_id": anchor, "target_contour": contour})


TARGET_100x70 = [[0, 0], [100, 0], [100, 70], [0, 70]]


# ---------------------------------------------------------------- 正常评估

def test_group_assessment_areas(prepared):
    """连通组：先裁剪再并集，缺口 = 目标 − 并集，面积守恒。"""
    r = _assess(prepared["client"], 2, 1, TARGET_100x70)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True
    assert b["version"] == 2
    assert b["current_version"] == 3            # 可在历史版上评估当前版之后的快照
    assert b["historical"] is True
    assert b["source"] == "accept"
    assert b["anchor"] == {"id": 1, "name": "陶片A-左半"}
    assert b["independent"] is False
    assert b["fragment_ids"] == [1, 2]          # 仅锚片所在组；C/D 不在组内
    assert b["fragment_count"] == 2
    assert b["group"] == {"id": 1, "fragment_ids": [1, 2]}
    assert b["target_area_mm2"] == pytest.approx(7000.0)
    # A、B 共边无重叠：覆盖 = 两片面积之和；目标 7000 = 覆盖 4800 + 缺口 2200
    assert b["covered_area_mm2"] == pytest.approx(4800.0)
    assert b["gap_area_mm2"] == pytest.approx(2200.0)
    assert b["target_area_mm2"] == pytest.approx(
        b["covered_area_mm2"] + b["gap_area_mm2"])
    assert b["gap_count"] == 1
    g = b["gaps"][0]
    assert g["index"] == 1
    assert g["area_mm2"] == pytest.approx(2200.0)
    assert g["hole_count"] == 0
    assert len(g["outer_boundary"]) >= 4
    assert g["holes"] == []
    # 目标轮廓与组内放置结果回显（世界坐标）
    assert b["target_contour"][0] == [0.0, 0.0]
    assert {p["fragment_id"] for p in b["placed_fragments"]} == {1, 2}
    assert b["coverage_polygons"]
    assert sum(p["area_mm2"] for p in b["coverage_polygons"]) == \
        pytest.approx(4800.0)


def test_independent_anchor_single_fragment(prepared):
    """孤片按单片评估：group=null，组内只有锚片自己。"""
    # C 轮廓 [[0,0],[40,0],[35,25],[10,30]]，大目标框全部容纳
    c_poly_area = 900.0    # 鞋带公式：|40·25 + 35·30 − 10·25| / 2 = 900
    r = _assess(prepared["client"], 2, 3,
                [[0, 0], [100, 0], [100, 100], [0, 100]])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["independent"] is True
    assert b["group"] is None
    assert b["fragment_ids"] == [3]
    # 缺口面积 = 目标面积 − 单片覆盖面积（无其他组成员）
    assert b["target_area_mm2"] == pytest.approx(10000.0)
    assert b["covered_area_mm2"] == pytest.approx(c_poly_area, abs=1e-6)
    assert b["gap_area_mm2"] == pytest.approx(10000.0 - c_poly_area)


def test_full_coverage_returns_empty_gap_list(prepared):
    """目标与孤片轮廓完全重合：缺口面积 0、空清单。"""
    contour_c = [[0, 0], [40, 0], [35, 25], [10, 30]]
    r = _assess(prepared["client"], 2, 3, contour_c)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["covered_area_mm2"] == pytest.approx(b["target_area_mm2"])
    assert b["gap_area_mm2"] == 0.0
    assert b["gap_count"] == 0
    assert b["gaps"] == []


def test_historical_baseline_grouping(prepared):
    """v1 尚无拼接关系：A 在该版是孤片（版本相关，组按该版快照取）。"""
    r = _assess(prepared["client"], 1, 1,
                [[-10, -10], [60, -10], [60, 70], [-10, 70]])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["version"] == 1 and b["historical"] is True
    assert b["source"] == "baseline"
    assert b["independent"] is True
    assert b["fragment_ids"] == [1]      # v1 时 B 与 A 尚无关系


def test_target_outside_all_fragments(prepared):
    """目标完全不与组相交：覆盖为 0，缺口即目标本身（单个、无孔）。"""
    r = _assess(prepared["client"], 3, 5,
                [[200, 200], [210, 200], [210, 210], [200, 210]])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["covered_area_mm2"] == 0.0
    assert b["gap_area_mm2"] == pytest.approx(100.0)
    assert b["gap_count"] == 1
    assert b["gaps"][0]["hole_count"] == 0
    assert b["coverage_polygons"] == []


# ---------------------------------------------------------------- 纯演算拓扑

def _snap(frags, version=9):
    return {"version": version, "fragments": frags, "relations": [], "groups": []}


def _frag(fid, contour, group_id=1, x=0.0, y=0.0, theta=0.0):
    return {"id": fid, "name": f"F{fid}", "contour": contour,
            "x": x, "y": y, "theta": theta, "group_id": group_id,
            "created_at": "t"}


def test_covered_island_preserves_hole_topology():
    """目标范围内的独立已覆盖岛：缺口保留为带 1 个内孔的多边形。"""
    snap = _snap([_frag(1, [[2, 2], [3, 2], [3, 3], [2, 3]]),
                  _frag(2, [[20, 20], [21, 20], [21, 21], [20, 21]])])
    ev = evaluate_repair(snap, 1,
                         validate_target([[0, 0], [10, 0], [10, 10], [0, 10]]))
    assert ev["fragment_ids"] == [1, 2]
    assert ev["covered_area"] == pytest.approx(1.0)       # f2 完全在目标外
    assert ev["gap_area"] == pytest.approx(99.0)
    assert len(ev["gaps_raw"]) == 1
    g = ev["gaps_raw"][0]
    assert len(g["holes"]) == 1                           # 覆盖岛 → 内孔保留
    assert g["area"] == pytest.approx(99.0)


def test_point_touch_gaps_remain_separate():
    """两个缺口仅共点（无公共边）时不合并，按面积降序+中心坐标各列一条。"""
    # 下、上两个三角覆盖片仅在 (5,5) 共点；剩下左右两个三角缺口
    snap = _snap([_frag(1, [[0, 0], [10, 0], [5, 5]]),
                  _frag(2, [[0, 10], [10, 10], [5, 5]])])
    ev = evaluate_repair(snap, 1,
                         validate_target([[0, 0], [10, 0], [10, 10], [0, 10]]))
    assert ev["covered_area"] == pytest.approx(50.0)
    assert len(ev["gaps_raw"]) == 2                       # 没有被并成一个多边形
    areas = sorted(g["area"] for g in ev["gaps_raw"])
    assert areas == pytest.approx([25.0, 25.0])
    # 面积相同 → 按中心坐标 x 升序：左缺口在前
    assert ev["gaps_raw"][0]["center"][0] < \
        ev["gaps_raw"][1]["center"][0]


def test_clip_before_union_outside_not_counted():
    """碎片超出目标的部分不计覆盖：10×10 片与目标只有 1/4 相交 → 25 mm²。"""
    snap = _snap([_frag(1, [[-5, -5], [5, -5], [5, 5], [-5, 5]], group_id=None)])
    ev = evaluate_repair(snap, 1,
                         validate_target([[0, 0], [10, 0], [10, 10], [0, 10]]))
    assert ev["covered_area"] == pytest.approx(25.0)
    assert ev["gap_area"] == pytest.approx(75.0)


def test_overlap_inside_group_counted_once():
    """组内两片面积重叠时，并集只计一次覆盖；对角两个缺口仅共点不合并。"""
    snap = _snap([_frag(1, [[0, 0], [6, 0], [6, 6], [0, 6]]),
                  _frag(2, [[2, 2], [10, 2], [10, 10], [2, 10]])])
    ev = evaluate_repair(snap, 1,
                         validate_target([[0, 0], [10, 0], [10, 10], [0, 10]]))
    # A 6×6=36，B 8×8=64，重叠 4×4=16；并集 36+64−16=84（重叠只计一次），缺口 16。
    assert ev["covered_area"] == pytest.approx(84.0)
    assert ev["gap_area"] == pytest.approx(16.0)
    # 两个 8 mm² 角缺口仅在 (6,2)/(2,6) 一带不相连 → 各自独立、不合并
    assert len(ev["gaps_raw"]) == 2
    assert sorted(g["area"] for g in ev["gaps_raw"]) == [8.0, 8.0]


def test_gaps_sorted_by_area_descending():
    """两条横条覆盖把目标分成三个缺口：30 / 20 / 10 mm² 按面积降序。"""
    snap = _snap([_frag(1, [[0, 3], [10, 3], [10, 4], [0, 4]]),
                  _frag(2, [[0, 6], [10, 6], [10, 9], [0, 9]])])
    ev = evaluate_repair(snap, 1,
                         validate_target([[0, 0], [10, 0], [10, 10], [0, 10]]))
    assert len(ev["gaps_raw"]) == 3
    assert [g["area"] for g in ev["gaps_raw"]] == \
        pytest.approx([30.0, 20.0, 10.0])


# ---------------------------------------------------------------- 只读语义

def test_assessment_is_read_only(prepared):
    c = prepared["client"]
    before_asm = c.get("/api/assembly").json()
    before_versions = c.get("/api/assembly/versions").json()
    r = _assess(c, 2, 1, TARGET_100x70)
    assert r.status_code == 200, r.text
    after_asm = c.get("/api/assembly").json()
    after_versions = c.get("/api/assembly/versions").json()
    assert after_asm == before_asm
    assert after_versions == before_versions
    assert len(after_versions["versions"]) == 3          # 未产生新快照


# ---------------------------------------------------------------- 整次拒绝

def test_self_intersecting_target_rejected(prepared):
    bowtie = [[0, 0], [10, 10], [10, 0], [0, 10]]
    r = _assess(prepared["client"], 2, 1, bowtie)
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "invalid_target_contour"
    assert d["errors"][0]["field"] == "target_contour"


def test_zero_area_target_rejected(prepared):
    r = _assess(prepared["client"], 2, 1,
                [[0, 0], [10, 0], [20, 0]])             # 三点共线
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_target_contour"


def test_non_finite_coordinates_rejected(prepared):
    r = _assess(prepared["client"], 2, 1,
                [[0, 0], [float("nan"), 0], [10, 10]])
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "non_finite_coordinate"
    assert d["errors"][0] == {
        "field": "target_contour[1].x",
        "code": "non_finite_coordinate",
        "message": d["errors"][0]["message"]}
    assert "非有限数" in d["errors"][0]["message"]


def test_infinity_coordinate_rejected(prepared):
    r = _assess(prepared["client"], 2, 1,
                [[0, 0], [1e999, 0], [10, 10]])
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "non_finite_coordinate"


def test_malformed_point_and_nan_collected_together(prepared):
    r = _assess(prepared["client"], 2, 1,
                [[0, 0], [10], [10, float("inf")]])
    assert r.status_code == 400
    fields = [e["field"] for e in r.json()["detail"]["errors"]]
    assert "target_contour[1]" in fields
    assert "target_contour[2].y" in fields


def test_invalid_anchor_id_rejected(prepared):
    r = _assess(prepared["client"], 2, 0, TARGET_100x70)
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "invalid_anchor"
    assert d["errors"][0]["field"] == "anchor_id"


def test_anchor_not_found_in_version_rejected(prepared):
    r = _assess(prepared["client"], 2, 99, TARGET_100x70)
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "anchor_not_found"
    assert d["version"] == 2
    assert d["errors"][0]["field"] == "anchor_id"


def test_anchor_deleted_in_historical_version(prepared):
    """锚片存在于当前版、但不存在于所选历史版时仍拒绝（#5 是 v3 才录入）。"""
    r = _assess(prepared["client"], 2, 5, TARGET_100x70)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "anchor_not_found"


def test_version_not_found(prepared):
    r = _assess(prepared["client"], 999, 1, TARGET_100x70)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "version_not_found"


def test_too_few_vertices_422(prepared):
    r = _assess(prepared["client"], 2, 1, [[0, 0], [10, 0]])
    assert r.status_code == 422


def test_malformed_body_422(prepared):
    r = prepared["client"].post(
        "/api/assembly/versions/2/repair-assessment",
        json={"anchor_id": "x", "target_contour": [[0, 0], [1, 0], [1, 1]]})
    assert r.status_code == 422
