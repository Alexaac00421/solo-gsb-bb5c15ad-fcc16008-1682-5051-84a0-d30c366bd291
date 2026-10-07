"""候选补配评估 API（/api/assembly/versions/{v}/repair-candidates）测试。

数据准备（模块级全新库）：
- 建 4 片 40×60 矩形 A(#1)/B(#2)/C(#3)/D(#4)；
- v5 accept：A-B 共边拼接成锚组（世界范围 x∈[0,80]）；
- v6 accept：C-D 共边拼接成另一连通组（世界范围 x∈[0,80]，组内相对位姿）。

目标轮廓取 160×60 矩形（x∈[0,160]）：锚组覆盖左半 4800 mm²，
候选组整体接到 B 右缘后净新增 4800 mm²、剩余缺口 0。

随后全部用例只读评估，不再变更数据。纯演算（evaluate_candidates /
validate_candidate_markers）直接构造版本快照，覆盖整组刚体移动、
超差/碰撞只标记该候选、零收益照实、排名次序与整次拒绝。
"""
import os
import tempfile

_fd, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_PATH"] = _DB_PATH
os.environ["SEED_DEMO_DATA"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import candidates as cand_mod  # noqa: E402
from app import main  # noqa: E402
from app.main import app  # noqa: E402

RECT = [[0, 0], [40, 0], [40, 60], [0, 60]]
TARGET_160x60 = [[0, 0], [160, 0], [160, 60], [0, 60]]
TARGET_120x60 = [[0, 0], [120, 0], [120, 60], [0, 60]]


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


@pytest.fixture(scope="module")
def prepared(client):
    for name in ("A", "B", "C", "D"):
        r = client.post("/api/fragments", json={"name": name, "contour": RECT})
        assert r.status_code == 201, r.text
    v = client.get("/api/assembly").json()["version"]      # 4 片 → v4
    r = client.post("/api/relations", json={
        "fragment_a": 1, "fragment_b": 2,
        "markers_a": [[40, 0], [40, 60]],
        "markers_b": [[0, 0], [0, 60]],
        "expected_version": v})
    assert r.status_code == 201, r.text                    # v5：锚组 A+B
    r = client.post("/api/relations", json={
        "fragment_a": 3, "fragment_b": 4,
        "markers_a": [[40, 0], [40, 60]],
        "markers_b": [[0, 0], [0, 60]],
        "expected_version": v + 1})
    assert r.status_code == 201, r.text                    # v6：候选组 C+D
    # v7：再录入一片窄孤片 E（20×60），用于构造不同净收益的排名次序
    r = client.post("/api/fragments", json={
        "name": "E", "contour": [[0, 0], [20, 0], [20, 60], [0, 60]]})
    assert r.status_code == 201, r.text                    # v7
    return {"client": client, "version": v + 2,
            "version_with_e": v + 3}


def _post(client, version, body):
    return client.post(
        f"/api/assembly/versions/{version}/repair-candidates", json=body)


def _body(anchor=1, target=TARGET_160x60, cands=None):
    return {"anchor_id": anchor, "target_contour": target,
            "candidates": cands or []}


# 候选片 C(#3) 属 C+D 组：接到 B(#2) 右缘 → 整组移到 [80,160]，
# 零误差、无碰撞、组覆盖 4800、净新增 4800、剩余缺口 0
FIT_B_RIGHT = {
    "candidate_fragment_id": 3, "anchor_fragment_id": 2,
    "markers_candidate": [[0, 0], [0, 60]],
    "markers_anchor": [[40, 0], [40, 60]]}


# ---------------------------------------------------------------- 正常评估

def test_basic_metrics_and_ranking(prepared):
    """合格候选：零误差拟合到 B 右缘，净新增 2400、缺口闭合，排名第 1。"""
    r = _post(prepared["client"], prepared["version"],
              _body(cands=[FIT_B_RIGHT]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True
    assert b["version"] == prepared["version"]
    assert b["anchor"] == {"id": 1, "name": "A"}
    assert b["group"] == {"id": 1, "fragment_ids": [1, 2]}
    assert b["independent"] is False
    assert b["target_area_mm2"] == pytest.approx(9600.0)
    assert b["anchor_covered_area_mm2"] == pytest.approx(4800.0)
    assert b["anchor_gap_area_mm2"] == pytest.approx(4800.0)
    assert b["candidate_count"] == 1 and b["eligible_count"] == 1

    row = b["candidates"][0]
    assert row["eligible"] is True and row["rank"] == 1
    assert row["pair_errors_mm"] == [0.0, 0.0]
    assert row["max_error_mm"] == 0.0
    assert row["within_tolerance"] is True
    assert row["anchor_overlaps"] == []
    assert row["has_internal_overlap"] is False
    assert row["candidate_covered_area_mm2"] == pytest.approx(4800.0)
    assert row["net_new_coverage_mm2"] == pytest.approx(4800.0)
    assert row["remaining_gap_area_mm2"] == 0.0
    assert row["reject_reasons"] == []
    assert row["transform"] == {"tx_mm": 80.0, "ty_mm": 0.0, "theta_deg": 0.0}
    # 放置结果回显：候选组整体平移（C→80、D→120），组内相对位姿不变
    placed = row["placed_group"]
    assert [(p["fragment_id"], p["x_mm"]) for p in placed] == \
        [(3, 80.0), (4, 120.0)]
    assert row["candidate_group_fragment_ids"] == [3, 4]
    assert b["ranking"][0] == {
        "rank": 1, "index": 0, "candidate_fragment_id": 3,
        "candidate_fragment_name": "C",
        "net_new_coverage_mm2": 4800.0, "max_error_mm": 0.0,
        "remaining_gap_area_mm2": 0.0}


def test_target_120_closes_gap_completely(prepared):
    """目标 120×60：锚组占 [0,80]，候选组 [80,160] 仅 C 片 [80,120] 在
    目标内（净新增 2400），剩余缺口 0；D 片超出目标部分不计。"""
    r = _post(prepared["client"], prepared["version"],
              _body(target=TARGET_120x60, cands=[FIT_B_RIGHT]))
    b = r.json()
    row = b["candidates"][0]
    assert row["eligible"] is True
    assert row["candidate_covered_area_mm2"] == pytest.approx(2400.0)
    assert row["net_new_coverage_mm2"] == pytest.approx(2400.0)
    assert row["remaining_gap_area_mm2"] == 0.0
    clipped = {p["fragment_id"]: p["clipped_area_mm2"]
               for p in row["placed_group"]}
    assert clipped == {3: 2400.0, 4: 0.0}


def test_candidate_group_moves_as_whole(prepared):
    """候选片属于 C+D 组：整组平移，组内相对位姿保持（C→80，D→120）。"""
    body = _body(cands=[{
        "candidate_fragment_id": 3, "anchor_fragment_id": 2,
        "markers_candidate": [[0, 0], [0, 60]],
        "markers_anchor": [[40, 0], [40, 60]]}])
    r = _post(prepared["client"], prepared["version"], body)
    row = r.json()["candidates"][0]
    assert row["candidate_group_id"] == 3
    assert row["candidate_group_fragment_ids"] == [3, 4]
    xs = sorted((p["fragment_id"], p["x_mm"]) for p in row["placed_group"])
    assert xs == [(3, 80.0), (4, 120.0)]
    assert row["net_new_coverage_mm2"] == pytest.approx(4800.0)
    assert row["remaining_gap_area_mm2"] == 0.0


def test_specifying_other_member_moves_same_group(prepared):
    """用组内 D 片指定标记：变换仍作用于整组（D 右缘 → B 右缘世界 x=80，
    即候选组当前位置 → 整组不移动并与锚组完全重叠，碰撞只标记该候选）。"""
    body = _body(cands=[{
        "candidate_fragment_id": 4, "anchor_fragment_id": 2,
        "markers_candidate": [[40, 0], [40, 60]],
        "markers_anchor": [[40, 0], [40, 60]]}])
    r = _post(prepared["client"], prepared["version"], body)
    row = r.json()["candidates"][0]
    # 整组位姿都被演算（不是只动 D）：C/D 均保持原位
    xs = sorted((p["fragment_id"], p["x_mm"]) for p in row["placed_group"])
    assert xs == [(3, 0.0), (4, 40.0)]
    assert row["eligible"] is False
    assert row["has_internal_overlap"] is True
    assert sum(o["area_mm2"] for o in row["anchor_overlaps"]) == \
        pytest.approx(4800.0)


def test_ranking_order_net_gain_desc(prepared):
    """合格候选按净新增覆盖降序：候选组（净增 4800）在前，孤片 E（1200）在后。"""
    body = _body(cands=[
        {"candidate_fragment_id": 5, "anchor_fragment_id": 2,
         # 窄孤片 E 接到 B 右缘：[80,100]，净增 1200
         "markers_candidate": [[0, 0], [0, 60]],
         "markers_anchor": [[40, 0], [40, 60]]},
        FIT_B_RIGHT,                                   # C+D 组 → 净增 4800
    ])
    r = _post(prepared["client"], prepared["version_with_e"], body)
    assert r.status_code == 200, r.text
    ranks = [(x["rank"], x["index"], x["candidate_fragment_id"],
              x["net_new_coverage_mm2"])
             for x in r.json()["ranking"]]
    assert ranks == [(1, 1, 3, 4800.0), (2, 0, 5, 1200.0)]


def test_ranking_tie_break_max_error_then_id():
    """净新增相同时：先按最大误差升序；误差也相同再按候选片编号升序。

    两片独立候选都贴合到锚片**左缘**（落在锚组左侧，仅共边不碰撞），
    目标取远处小框 → 净新增均为 0：候选 3 零误差，候选 5 用第 3 对
    偏 1 mm 的标记制造 ≤1 mm 的最小二乘残差，仍合格。
    """
    anchor = _frag(1, group_id=1)
    f3 = _frag(3, group_id=None, contour=RECT)
    f5 = _frag(5, group_id=None,
               contour=[[0, 0], [20, 0], [20, 60], [0, 60]])
    snap = _snap([anchor, _frag(2, group_id=1), f3, f5])
    far_target = [(1000, 1000), (1020, 1000), (1020, 1060), (1000, 1060)]

    # 候选 3（提交序 1）：右缘两点 → 锚片左缘世界 x=0，零误差（片在 [-40,0]）
    s3 = {"index": 1, "candidate_fragment_id": 3, "anchor_fragment_id": 1,
          "markers_candidate": [(40.0, 0.0), (40.0, 60.0)],
          "markers_anchor": [(0.0, 0.0), (0.0, 60.0)],
          "candidate": f3, "anchor_fragment": anchor, "members": [f3]}
    # 候选 5（提交序 0）：右缘三点 → 锚片左缘，第 3 对偏 1 mm（残差 <1mm）
    s5 = {"index": 0, "candidate_fragment_id": 5, "anchor_fragment_id": 1,
          "markers_candidate": [(20.0, 0.0), (20.0, 60.0), (20.0, 30.0)],
          "markers_anchor": [(0.0, 0.0), (0.0, 60.0), (-1.0, 30.0)],
          "candidate": f5, "anchor_fragment": anchor, "members": [f5]}
    ev = cand_mod.evaluate_candidates(snap, 1, far_target, [s5, s3])
    by_id = {r["candidate_fragment_id"]: r for r in ev["rows"]}
    assert all(r["eligible"] for r in ev["rows"])
    assert all(r["net_new_coverage_mm2"] == 0.0 for r in ev["rows"])
    assert by_id[3]["max_error_mm"] == pytest.approx(0.0)
    assert 0.0 < by_id[5]["max_error_mm"] <= 1.0
    # 净新增同为 0：最大误差升序 → #3 在前（即使其提交序在后）
    assert [r["candidate_fragment_id"] for r in ev["ranked"]] == [3, 5]

    # 两片均零误差零收益：按候选片编号升序（3 在 5 前）
    s5b = {"index": 0, "candidate_fragment_id": 5, "anchor_fragment_id": 1,
           "markers_candidate": [(20.0, 0.0), (20.0, 60.0)],
           "markers_anchor": [(0.0, 0.0), (0.0, 60.0)],
           "candidate": f5, "anchor_fragment": anchor, "members": [f5]}
    ev2 = cand_mod.evaluate_candidates(snap, 1, far_target, [s5b, s3])
    assert all(r["eligible"] for r in ev2["rows"])
    assert [r["candidate_fragment_id"] for r in ev2["ranked"]] == [3, 5]


def test_zero_gain_eligible_shown(prepared):
    """合格但候选组全部落在目标外：净新增 0 照实显示，仍参与排名。"""
    # D 右缘 → A 左缘（世界 x=0）：整组移到 [-80,0]，与目标 [0,160] 仅共边
    body = _body(cands=[
        {"candidate_fragment_id": 4, "anchor_fragment_id": 1,
         "markers_candidate": [[40, 0], [40, 60]],
         "markers_anchor": [[0, 0], [0, 60]]},
    ])
    r = _post(prepared["client"], prepared["version"], body)
    b = r.json()
    row = b["candidates"][0]
    assert row["eligible"] is True
    assert row["pair_errors_mm"] == [0.0, 0.0]
    assert row["net_new_coverage_mm2"] == 0.0
    assert row["remaining_gap_area_mm2"] == pytest.approx(4800.0)
    assert b["ranking"][0]["net_new_coverage_mm2"] == 0.0


def test_overlap_only_marks_that_candidate(prepared):
    """单个候选碰撞 → eligible=false 且不进排名；同批其余候选照常评估。"""
    body = _body(cands=[
        FIT_B_RIGHT,                                   # 合格
        {"candidate_fragment_id": 3, "anchor_fragment_id": 1,
         # C 左缘 → A 右缘世界 x=40：C 与 B(#2) 完全重叠 2400
         "markers_candidate": [[0, 0], [0, 60]],
         "markers_anchor": [[40, 0], [40, 60]]},
    ])
    r = _post(prepared["client"], prepared["version"], body)
    b = r.json()
    assert b["candidate_count"] == 2 and b["eligible_count"] == 1
    good, bad = b["candidates"]
    assert good["eligible"] is True and good["rank"] == 1
    assert bad["eligible"] is False and bad["rank"] is None
    assert bad["has_internal_overlap"] is True
    assert bad["anchor_overlaps"][0]["candidate_fragment_id"] == 3
    assert bad["anchor_overlaps"][0]["anchor_fragment_id"] == 2
    assert bad["anchor_overlaps"][0]["area_mm2"] == pytest.approx(2400.0)
    assert any("碰撞" in s for s in bad["reject_reasons"])
    assert [x["index"] for x in b["ranking"]] == [0]


def test_pair_error_over_tolerance_marks_only(prepared):
    """第 2 对标记偏 10 mm：最小二乘后仍有 >1 mm 残差 → 仅该候选不合格。"""
    body = _body(cands=[
        FIT_B_RIGHT,                                   # 合格
        {"candidate_fragment_id": 3, "anchor_fragment_id": 2,
         "markers_candidate": [[0, 0], [0, 60], [40, 0]],
         "markers_anchor": [[40, 0], [40, 60], [30, 0]]},  # 第 3 对偏 10 mm
    ])
    r = _post(prepared["client"], prepared["version"], body)
    b = r.json()
    bad = b["candidates"][1]
    assert bad["eligible"] is False and bad["rank"] is None
    assert bad["within_tolerance"] is False
    assert bad["max_error_mm"] > 1.0
    assert any("误差超限" in s for s in bad["reject_reasons"])
    assert b["candidates"][0]["eligible"] is True
    assert [x["index"] for x in b["ranking"]] == [0]


def test_independent_anchor(prepared):
    """孤片作锚：锚组单片，候选可来自其他孤片。"""
    # #3/#4 已在 v6 成组；锚选孤片无 —— 用历史版 v4：全部孤片
    r = _post(prepared["client"], 4, _body(
        anchor=1, target=TARGET_120x60, cands=[{
            "candidate_fragment_id": 3, "anchor_fragment_id": 1,
            "markers_candidate": [[0, 0], [0, 60]],
            "markers_anchor": [[40, 0], [40, 60]]}]))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["historical"] is True
    assert b["independent"] is True and b["group"] is None
    assert b["anchor_group_fragment_ids"] == [1]
    row = b["candidates"][0]
    assert row["eligible"] is True
    assert row["net_new_coverage_mm2"] == pytest.approx(2400.0)


def test_read_only(prepared):
    c = prepared["client"]
    before_asm = c.get("/api/assembly").json()
    before_versions = c.get("/api/assembly/versions").json()
    r = _post(c, prepared["version"], _body(cands=[FIT_B_RIGHT]))
    assert r.status_code == 200
    after_asm = c.get("/api/assembly").json()
    after_versions = c.get("/api/assembly/versions").json()
    assert after_asm == before_asm
    assert after_versions == before_versions
    assert len(after_versions["versions"]) == \
        len(before_versions["versions"])   # 未产生新快照


# ---------------------------------------------------------------- 整次拒绝

def _reject(body, version=None, prepared_fixture=None, client=None):
    c = client or prepared_fixture["client"]
    ver = version if version is not None else prepared_fixture["version"]
    return _post(c, ver, body)


def test_empty_candidates_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[]))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "empty_candidates"


def test_invalid_fragment_id_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 0, "anchor_fragment_id": 2,
        "markers_candidate": [[0, 0], [0, 60]],
        "markers_anchor": [[40, 0], [40, 60]]}]))
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "invalid_candidates"
    assert d["errors"][0]["field"] == \
        "candidates[0].candidate_fragment_id"


def test_same_fragment_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 3, "anchor_fragment_id": 3,
        "markers_candidate": [[0, 0], [0, 60]],
        "markers_anchor": [[0, 0], [0, 60]]}]))
    assert r.status_code == 400
    assert r.json()["detail"]["errors"][0]["code"] == "same_fragment"


def test_same_group_candidate_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 2, "anchor_fragment_id": 1,
        "markers_candidate": [[0, 0], [0, 60]],
        "markers_anchor": [[40, 0], [40, 60]]}]))
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "invalid_candidates"
    assert d["errors"][0]["code"] == "same_group_candidate"


def test_anchor_fragment_not_in_anchor_group_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 3, "anchor_fragment_id": 4,   # 4 不在锚组
        "markers_candidate": [[0, 0], [0, 60]],
        "markers_anchor": [[40, 0], [40, 60]]}]))
    assert r.status_code == 400
    assert r.json()["detail"]["errors"][0]["code"] == \
        "anchor_fragment_group_mismatch"


def test_missing_candidate_fragment_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 99, "anchor_fragment_id": 2,
        "markers_candidate": [[0, 0], [0, 60]],
        "markers_anchor": [[40, 0], [40, 60]]}]))
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "fragment_not_found"
    assert d["errors"][0]["field"] == \
        "candidates[0].candidate_fragment_id"


def test_missing_anchor_fragment_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 3, "anchor_fragment_id": 88,
        "markers_candidate": [[0, 0], [0, 60]],
        "markers_anchor": [[40, 0], [40, 60]]}]))
    assert r.status_code == 404
    assert r.json()["detail"]["errors"][0]["field"] == \
        "candidates[0].anchor_fragment_id"


def test_anchor_not_found_rejected(prepared):
    r = _post(prepared["client"], prepared["version"],
              _body(anchor=77, cands=[FIT_B_RIGHT]))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "anchor_not_found"


def test_version_not_found(prepared):
    r = _post(prepared["client"], 999, _body(cands=[FIT_B_RIGHT]))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "version_not_found"


def test_invalid_target_contour_rejected(prepared):
    r = _post(prepared["client"], prepared["version"],
              _body(target=[[0, 0], [10, 10], [10, 0], [0, 10]],
                    cands=[FIT_B_RIGHT]))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_target_contour"


def test_invalid_anchor_id_rejected(prepared):
    r = _post(prepared["client"], prepared["version"],
              _body(anchor=-1, cands=[FIT_B_RIGHT]))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_anchor"


def test_marker_not_on_edge_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 3, "anchor_fragment_id": 2,
        "markers_candidate": [[5, 5], [5, 50]],                # 片内点
        "markers_anchor": [[40, 0], [40, 60]]}]))
    assert r.status_code == 400
    codes = [e["code"] for e in r.json()["detail"]["errors"]]
    assert "marker_not_on_edge" in codes
    assert all("candidates[0].markers_candidate" in e["field"]
               for e in r.json()["detail"]["errors"])


def test_marker_count_mismatch_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 3, "anchor_fragment_id": 2,
        "markers_candidate": [[0, 0], [0, 60], [40, 0]],
        "markers_anchor": [[40, 0], [40, 60]]}]))
    assert r.status_code == 400
    assert r.json()["detail"]["errors"][0]["code"] == \
        "marker_count_mismatch"


def test_too_few_markers_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 3, "anchor_fragment_id": 2,
        "markers_candidate": [[0, 0]],
        "markers_anchor": [[40, 0]]}]))
    assert r.status_code == 400
    codes = [e["code"] for e in r.json()["detail"]["errors"]]
    assert "invalid_markers" in codes


def test_duplicate_marker_rejected(prepared):
    r = _post(prepared["client"], prepared["version"], _body(cands=[{
        "candidate_fragment_id": 3, "anchor_fragment_id": 2,
        "markers_candidate": [[0, 0], [0, 0]],
        "markers_anchor": [[40, 0], [40, 60]]}]))
    assert r.status_code == 400
    codes = [e["code"] for e in r.json()["detail"]["errors"]]
    assert "duplicate_marker" in codes


def test_all_field_errors_collected(prepared):
    """多个候选的多类问题一次全部逐字段指出（整次拒绝、不执行拟合）。"""
    r = _post(prepared["client"], prepared["version"], _body(cands=[
        {"candidate_fragment_id": 2, "anchor_fragment_id": 1,       # 同组
         "markers_candidate": [[0, 0], [0, 60]],
         "markers_anchor": [[40, 0], [40, 60]]},
        {"candidate_fragment_id": 3, "anchor_fragment_id": 2,
         "markers_candidate": [[1, 1]],                            # 不足 2 处
         "markers_anchor": [[40, 0]]},
    ]))
    assert r.status_code == 400
    fields = [e["field"] for e in r.json()["detail"]["errors"]]
    assert "candidates[0].candidate_fragment_id" in fields
    assert "candidates[1].markers_candidate" in fields
    assert "candidates[1].markers_anchor" in fields


def test_one_bad_candidate_rejects_entire_request(prepared):
    """即使候选 0 合格，候选 1 标记无效仍整次拒绝（不返回任何候选结果）。"""
    r = _post(prepared["client"], prepared["version"], _body(cands=[
        FIT_B_RIGHT,
        {"candidate_fragment_id": 3, "anchor_fragment_id": 2,
         "markers_candidate": [[0, 0], [9, 9]],
         "markers_anchor": [[40, 0], [40, 60]]}]))                 # (9,9) 不在边缘
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_candidates"


def test_missing_version_before_marker_check(prepared):
    """版本快照不存在：携带合法候选时仍 404 version_not_found
    （空列表的静态校验先于版本加载，符合间隙查询同一约定）。"""
    r = _post(prepared["client"], 999, _body(cands=[FIT_B_RIGHT]))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "version_not_found"


def test_malformed_body_422(prepared):
    r = prepared["client"].post(
        f"/api/assembly/versions/{prepared['version']}/repair-candidates",
        json={"anchor_id": 1, "target_contour": [[0, 0], [1, 0]]})
    assert r.status_code == 422


# ---------------------------------------------------------------- 纯演算

def _snap(frags, version=9):
    return {"version": version, "fragments": frags, "relations": [], "groups": []}


def _frag(fid, contour=RECT, group_id=3, x=0.0, y=0.0, theta=0.0):
    return {"id": fid, "name": f"F{fid}", "contour": contour,
            "x": x, "y": y, "theta": theta, "group_id": group_id,
            "created_at": "t"}


def test_pure_rotation_preserved():
    """180° 旋转拟合：右缘上/下标记反向对应锚片左缘下/上，误差为零。

    锚片占 [0,40]×[0,60]，旋转后的候选片落在锚片上（180° 关于锚片中心
    对称），必然碰撞 —— 本用例只验证旋转变换本身（误差为零、θ=±180°），
    碰撞标记单独在 API 用例覆盖。
    """
    anchor = _frag(1, group_id=1, x=0, y=0)
    cand = _frag(3, group_id=None, x=0, y=0)
    snap = _snap([anchor, cand])
    spec = {
        "index": 0, "candidate_fragment_id": 3, "anchor_fragment_id": 1,
        "markers_candidate": [(40.0, 0.0), (40.0, 60.0)],
        "markers_anchor": [(0.0, 60.0), (0.0, 0.0)],
        "candidate": cand, "anchor_fragment": anchor, "members": [cand]}
    ev = cand_mod.evaluate_candidates(
        snap, 1, [(-40, 0), (40, 0), (40, 60), (-40, 60)], [spec])
    row = ev["rows"][0]
    assert row["pair_errors_mm"] == pytest.approx([0.0, 0.0], abs=1e-6)
    assert abs(abs(row["transform"]["theta_deg"]) - 180.0) < 1e-6


def test_pure_anchor_not_found():
    from app.repair import RepairError
    snap = _snap([_frag(1, group_id=None)])
    with pytest.raises(RepairError) as ei:
        cand_mod.evaluate_candidates(snap, 9, [(0, 0), (1, 0), (1, 1)], [])
    assert ei.value.status == 404 and ei.value.code == "anchor_not_found"


def test_validate_markers_group_rules():
    """validate_candidate_markers：同组 / 非锚组 / 缺片 / 越界标记分类拒绝。"""
    snap = _snap([
        _frag(1, group_id=1), _frag(2, group_id=1),
        _frag(3, group_id=3), _frag(4, group_id=3)])
    frags = {f["id"]: f for f in snap["fragments"]}
    anchor = frags[1]

    def run(raw):
        ids = [{
            "index": i,
            "candidate_fragment_id": c["cf"], "anchor_fragment_id": c["af"],
            "markers_candidate": c["mc"], "markers_anchor": c["ma"],
        } for i, c in enumerate(raw)]
        return cand_mod.validate_candidate_markers(ids, frags, anchor, 9)

    edge_l = [[0, 0], [0, 60]]
    edge_r = [[40, 0], [40, 60]]
    # 全部合法：返回 2 条规范化候选（允许重复候选片）
    specs = run([{"cf": 3, "af": 1, "mc": edge_l, "ma": edge_r},
                 {"cf": 4, "af": 2, "mc": edge_r, "ma": edge_r}])
    assert len(specs) == 2

    with pytest.raises(cand_mod.CandidateError) as ei:
        run([{"cf": 2, "af": 1, "mc": edge_l, "ma": edge_r}])
    assert ei.value.status == 400
    assert ei.value.errors[0]["code"] == "same_group_candidate"

    with pytest.raises(cand_mod.CandidateError) as ei:
        run([{"cf": 3, "af": 4, "mc": edge_l, "ma": edge_r}])
    assert ei.value.errors[0]["code"] == "anchor_fragment_group_mismatch"

    with pytest.raises(cand_mod.CandidateError) as ei:
        run([{"cf": 99, "af": 1, "mc": edge_l, "ma": edge_r}])
    assert ei.value.status == 404
    assert ei.value.code == "fragment_not_found"
