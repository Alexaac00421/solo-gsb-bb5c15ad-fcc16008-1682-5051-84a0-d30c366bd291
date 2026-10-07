"""补配候选组评估：在任一装配版本快照上，以锚片、世界坐标目标轮廓与若干
候选片为输入，整体平移+旋转候选片所在的另一连通组，评估其补配收益。

修复师在**任一装配版本**中选锚片并绘制世界坐标毫米制的**目标器形闭合
轮廓**，一次提交若干候选；每个候选指定：

- ``candidate_fragment_id``：**另一连通组**的一片陶片（候选片）；
- ``anchor_fragment_id``：**锚组内**的一片陶片（锚侧标记片，可即锚片）；
- 两侧**按序一一对应**的边缘标记（各 ≥2 处、数量一致、同片不重合、
  位于该片在该版轮廓的边缘，标记为局部坐标）。

系统只取该版快照，锚组保持该版位姿不动，保持候选组内部相对位姿不变，
对候选组整体施加一次平移+旋转（世界坐标最小二乘拟合标记，沿用历史拼接
/校准的刚体配准语义），逐候选返回：

1. 逐对对应点误差与最大误差（mm）；
2. 移动后候选组与**锚组**的轮廓内部重叠明细（允许共边/共点）；
3. 目标范围内相对锚组覆盖的**净新增覆盖面积**——候选组各片先裁入目标
   范围再取并集，减去锚组在目标内的覆盖并集（与锚组重叠/超出目标均不计）；
4. **剩余缺口面积**——目标减去锚组与候选组覆盖并集后的面积。

仅当**每对误差均 ≤ 1 mm 且与锚组无内部重叠**的候选才参与排名，依次按
净新增覆盖降序、最大误差升序、候选片编号升序排列；零收益候选照实显示。

本模块只做**只读**演算：不改当前装配、关系、版本与快照，也不产生新版本。
请求级校验（同组候选、缺失版本/陶片、无效目标轮廓/边缘标记等）整次拒绝
并逐字段指出原因；单个候选的拟合超差或碰撞只标记该候选（``eligible=false``
并给出原因），不影响其余候选结果。
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from . import geometry
from .repair import RepairError, _polygon_parts

# 复用既有的数据语义：1 mm 对应点误差上限（边缘/重合容差取 geometry 常量）
MAX_ERROR_MM = geometry.MAX_ERROR_MM
EDGE_TOL_MM = geometry.EDGE_TOL_MM

# 单候选被排除出排名的原因码
REASON_ERROR_EXCEEDS_1MM = "error_exceeds_1mm"
REASON_INTERNAL_OVERLAP = "internal_overlap"


class CandidateError(Exception):
    """请求级错误（整次拒绝），status 为对应 HTTP 状态码。

    errors 为逐字段错误明细：[{"field", "code", "message"}, ...]，
    field 形如 "candidates[0].markers_candidate[2]"。
    """

    def __init__(self, status: int, code: str, message: str,
                 errors: list[dict] | None = None, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.errors = errors or []
        self.extra = extra or {}

    def detail(self) -> dict:
        d = {"message": self.message, "code": self.code}
        if self.errors:
            d["errors"] = self.errors
        d.update(self.extra)
        return d


# ---------------------------------------------------------------- 请求静态校验

def _field(i: int, name: str | None = None) -> str:
    return f"candidates[{i}]" if name is None else f"candidates[{i}].{name}"


def validate_candidates_static(candidates) -> None:
    """与快照内容无关的静态校验，不通过抛 CandidateError(400)。

    收集全部字段问题后一次性拒绝（整次拒绝、逐字段指出）：候选为空、
    编号非正整数、标记点不是两个数值/含 NaN±Infinity、每侧不足 2 处、
    两侧数量不一致、同片标记重合。
    """
    if not candidates:
        raise CandidateError(
            400, "empty_candidates", "候选列表不能为空，整次拒绝",
            errors=[{"field": "candidates", "code": "empty_candidates",
                     "message": "至少需要提交一个补配候选"}])

    errors: list[dict] = []
    non_finite = False
    for i, c in enumerate(candidates):
        cid, aid = c.candidate_fragment_id, c.anchor_fragment_id
        for name, fid in (("candidate_fragment_id", cid),
                          ("anchor_fragment_id", aid)):
            if not isinstance(fid, int) or isinstance(fid, bool) or fid < 1:
                errors.append({
                    "field": _field(i, name), "code": "invalid_fragment_id",
                    "message": f"无效编号：碎片 id 必须为正整数（当前为 {fid}）"})

        malformed = False
        sides = []
        for side_name, markers in (("markers_candidate", c.markers_candidate),
                                  ("markers_anchor", c.markers_anchor)):
            pts: list[tuple[float, float]] = []
            for j, m in enumerate(markers):
                if not isinstance(m, (list, tuple)) or len(m) != 2:
                    malformed = True
                    errors.append({
                        "field": f"{_field(i, side_name)}[{j}]",
                        "code": "invalid_marker",
                        "message": "每个边缘标记必须是恰好两个数值的坐标 [x, y]"})
                    continue
                fx, fy = float(m[0]), float(m[1])
                bad_axis = False
                if not math.isfinite(fx):
                    non_finite = True
                    bad_axis = True
                    errors.append({
                        "field": f"{_field(i, side_name)}[{j}].x",
                        "code": "non_finite_coordinate",
                        "message": f"候选 {i + 1} 第 {j + 1} 处标记 x 坐标"
                                   "为非有限数（NaN / Infinity 不允许）"})
                if not math.isfinite(fy):
                    non_finite = True
                    bad_axis = True
                    errors.append({
                        "field": f"{_field(i, side_name)}[{j}].y",
                        "code": "non_finite_coordinate",
                        "message": f"候选 {i + 1} 第 {j + 1} 处标记 y 坐标"
                                   "为非有限数（NaN / Infinity 不允许）"})
                if not bad_axis:
                    pts.append((fx, fy))
            sides.append(pts)

        if malformed:
            continue
        pts_c, pts_a = sides
        for side_name, pts in (("markers_candidate", pts_c),
                               ("markers_anchor", pts_a)):
            if len(pts) < 2:
                errors.append({
                    "field": _field(i, side_name), "code": "too_few_markers",
                    "message": f"每侧至少需要 2 处边缘标记（当前 {len(pts)} 处）"})
                continue
            for u in range(len(pts)):
                for v in range(u + 1, len(pts)):
                    if geometry.dist(pts[u], pts[v]) < geometry.MARKER_DIST_TOL:
                        errors.append({
                            "field": _field(i, side_name),
                            "code": "duplicate_marker",
                            "message": f"标记重合：第 {u + 1} 处与第 {v + 1} 处"
                                       "标记位置相同"})
        if len(pts_c) >= 2 and len(pts_a) >= 2 and len(pts_c) != len(pts_a):
            errors.append({
                "field": _field(i), "code": "marker_count_mismatch",
                "message": f"两侧标记数量必须一致（候选侧 {len(pts_c)} 对"
                           f"锚侧 {len(pts_a)}），按序一一对应"})

    if errors:
        code = "non_finite_coordinate" if non_finite else "invalid_candidates"
        message = ("候选标记含非有限数坐标，整次拒绝（未执行任何拟合）"
                   if non_finite else
                   "候选校验失败，整次拒绝（未执行任何拟合）")
        raise CandidateError(400, code, message, errors=errors)


# ---------------------------------------------------------------- 快照级校验

def _group_members(snap: dict, frag: dict) -> list[dict]:
    """碎片所在连通组的全部成员（按 id 排序）；孤片只返回其自己。

    与补配评估/历史恢复一致：连通组直接取版本快照固化的 group_id。
    """
    gid = frag.get("group_id")
    if gid is None:
        return [frag]
    members = [f for f in snap["fragments"] if f.get("group_id") == gid]
    return sorted(members, key=lambda f: int(f["id"]))


def _check_finite_members(members: list[dict], snap_version: int) -> None:
    """防御性检查组内陶片轮廓/位姿不含非有限坐标（正常快照不应出现）。"""
    errors: list[dict] = []
    for f in members:
        fid = int(f["id"])
        for name, val in (("x", f["x"]), ("y", f["y"]), ("theta", f["theta"])):
            if not math.isfinite(float(val)):
                errors.append({
                    "field": f"fragments[{fid}].{name}",
                    "code": "non_finite_coordinate",
                    "message": f"碎片 #{fid} 位姿 {name} 非有限数"})
        for k, p in enumerate(f["contour"]):
            for axis, val in zip(("x", "y"), p):
                if not math.isfinite(float(val)):
                    errors.append({
                        "field": f"fragments[{fid}].contour[{k}].{axis}",
                        "code": "non_finite_coordinate",
                        "message": f"碎片 #{fid} 轮廓第 {k + 1} 个顶点 "
                                   f"{axis} 坐标非有限数"})
    if errors:
        raise CandidateError(
            400, "non_finite_coordinate",
            f"版本 v{snap_version} 快照中存在非有限坐标，整次拒绝"
            "（未执行任何拟合）",
            errors=errors, extra={"version": snap_version})


def _on_edge_errors(i: int, side_name: str, pts, frag: dict) -> list[dict]:
    """逐点检查标记是否位于该碎片在该版快照的轮廓边缘，返回逐标记错误。"""
    out: list[dict] = []
    boundary = LineString(list(frag["contour"]) + [frag["contour"][0]])
    for j, p in enumerate(pts):
        if boundary.distance(Point(p)) > EDGE_TOL_MM:
            out.append({
                "field": f"{_field(i, side_name)}[{j}]",
                "code": "marker_not_on_edge",
                "message": f"第 {j + 1} 处标记不在碎片 #{frag['id']} "
                           "该版轮廓的边缘上（须为该片局部坐标的边缘点）"})
    return out


def evaluate_candidates(snap: dict, anchor_id: int,
                        target_points: list[tuple[float, float]],
                        candidates) -> dict:
    """在版本快照上执行全部候选的补配评估（纯只读演算，不写库）。

    锚片在该版不存在 → RepairError(404 anchor_not_found)；候选引用的陶片
    缺失 → CandidateError(404 fragment_not_found)；同组候选、锚侧片不在
    锚组、标记不在边缘 → CandidateError(400 invalid_candidates)。
    单个候选的拟合超差/碰撞不抛错，只在结果行中标记。
    """
    version = int(snap["version"])
    frags = {int(f["id"]): f for f in snap["fragments"]}

    anchor = frags.get(int(anchor_id))
    if anchor is None:
        raise RepairError(
            404, "anchor_not_found",
            f"锚片 #{anchor_id} 在版本 v{version} 快照中不存在，"
            "整次拒绝（未执行任何拟合）",
            errors=[{"field": "anchor_id", "code": "anchor_not_found",
                     "message": f"碎片 #{anchor_id} 不存在于版本 v{version}"}],
            extra={"version": version, "anchor_id": anchor_id})

    # 快照中缺失的候选引用陶片：逐字段收集后一次性 404
    missing: list[dict] = []
    for i, c in enumerate(candidates):
        for name, fid in (("candidate_fragment_id", c.candidate_fragment_id),
                          ("anchor_fragment_id", c.anchor_fragment_id)):
            if fid not in frags:
                missing.append({
                    "field": _field(i, name), "code": "fragment_not_found",
                    "message": f"碎片 #{fid} 在版本 v{version} 快照中不存在"})
    if missing:
        raise CandidateError(
            404, "fragment_not_found",
            f"版本 v{version} 快照中缺少候选所需陶片，整次拒绝",
            errors=missing, extra={"version": version})

    anchor_members = _group_members(snap, anchor)
    anchor_ids = {int(f["id"]) for f in anchor_members}
    _check_finite_members(anchor_members, version)

    errors: list[dict] = []
    checked_groups: set = set()
    for i, c in enumerate(candidates):
        cand_frag = frags[c.candidate_fragment_id]
        anchor_side_frag = frags[c.anchor_fragment_id]

        # 另一连通组：候选片不得属于锚片所在组（孤片锚时也不得即锚片自己）
        if c.candidate_fragment_id in anchor_ids:
            errors.append({
                "field": _field(i, "candidate_fragment_id"),
                "code": "same_group",
                "message": f"候选片 #{c.candidate_fragment_id} 与锚片 "
                           f"#{anchor_id} 属同一连通组，候选必须指定另一连通组的陶片"})

        # 锚组内一片：锚侧标记片必须属于锚片所在连通组
        if c.anchor_fragment_id not in anchor_ids:
            errors.append({
                "field": _field(i, "anchor_fragment_id"),
                "code": "anchor_fragment_not_in_anchor_group",
                "message": f"碎片 #{c.anchor_fragment_id} 不在锚片 "
                           f"#{anchor_id} 所在连通组内，不能作为锚侧标记片"})

        # 标记必须位于各自陶片在该版快照的轮廓边缘（局部坐标）
        pts_c = [(float(m[0]), float(m[1])) for m in c.markers_candidate]
        pts_a = [(float(m[0]), float(m[1])) for m in c.markers_anchor]
        errors.extend(_on_edge_errors(i, "markers_candidate", pts_c, cand_frag))
        errors.extend(_on_edge_errors(i, "markers_anchor", pts_a,
                                      anchor_side_frag))

        gid = cand_frag.get("group_id")
        if gid not in checked_groups:
            checked_groups.add(gid)
            _check_finite_members(_group_members(snap, cand_frag), version)

    if errors:
        raise CandidateError(
            400, "invalid_candidates",
            "候选组/边缘标记校验失败，整次拒绝（未执行任何拟合）",
            errors=errors, extra={"version": version})

    target = Polygon(target_points)

    # 锚组在目标范围内的覆盖并集（先逐片裁入，再取并集）——全部候选共用
    anchor_polys: list[Polygon] = []
    anchor_placed: list[dict] = []
    for f in anchor_members:
        wc = geometry.contour_world(
            f["contour"], (f["x"], f["y"], f["theta"]))
        poly = Polygon(wc)
        anchor_placed.append({"id": int(f["id"]), "name": f["name"],
                              "poly": poly})
        anchor_polys.extend(_polygon_parts(poly.intersection(target)))
    anchor_coverage = (unary_union(anchor_polys) if anchor_polys
                       else Polygon())

    rows = [
        _evaluate_one(snap, i, c, frags, target, anchor_placed,
                      anchor_coverage)
        for i, c in enumerate(candidates)
    ]

    # 排名：仅合格候选参与；净新增覆盖降序 → 最大误差升序 → 候选片编号升序
    eligible_rows = [r for r in rows if r["eligible"]]
    eligible_rows.sort(key=lambda r: (-r["net_new_area"],
                                      r["max_error"], r["candidate_fragment_id"]))
    for rank, r in enumerate(eligible_rows, start=1):
        r["rank"] = rank

    return {
        "version": version,
        "anchor": anchor,
        "anchor_members": anchor_members,
        "target": target,
        "anchor_coverage": anchor_coverage,
        "rows": rows,
    }


# ---------------------------------------------------------------- 单候选演算

def _evaluate_one(snap: dict, i: int, c, frags: dict[int, dict],
                  target: Polygon, anchor_placed: list[dict],
                  anchor_coverage) -> dict:
    """整体平移+旋转候选组一次，计算单个候选的误差/碰撞/覆盖（只读）。"""
    cand_frag = frags[c.candidate_fragment_id]
    anchor_side_frag = frags[c.anchor_fragment_id]
    cand_members = _group_members(snap, cand_frag)

    markers_c = [(float(m[0]), float(m[1])) for m in c.markers_candidate]
    markers_a = [(float(m[0]), float(m[1])) for m in c.markers_anchor]

    pose_c = (cand_frag["x"], cand_frag["y"], cand_frag["theta"])
    pose_a = (anchor_side_frag["x"], anchor_side_frag["y"],
              anchor_side_frag["theta"])

    # 世界坐标最小二乘：候选侧标记（候选片该版位姿）整体刚体贴到锚侧标记
    src = [geometry.apply_pose(m, pose_c) for m in markers_c]
    dst = [geometry.apply_pose(m, pose_a) for m in markers_a]
    transform = geometry.fit_rigid(src, dst)

    before_poses = {int(f["id"]): (f["x"], f["y"], f["theta"])
                    for f in cand_members}
    # 保持候选组内部相对位姿：同一 T 复合到组内每片的该版位姿
    after_poses = {fid: geometry.compose_pose(transform, pose)
                   for fid, pose in before_poses.items()}
    moved_cand_pose = after_poses[c.candidate_fragment_id]

    pair_errors = [
        geometry.dist(geometry.apply_pose(mc, moved_cand_pose),
                      geometry.apply_pose(ma, pose_a))
        for mc, ma in zip(markers_c, markers_a)
    ]
    max_error = max(pair_errors, default=0.0)
    within = all(e <= MAX_ERROR_MM + 1e-9 for e in pair_errors)

    # 移动后候选组各片世界轮廓
    moved_placed = []
    for f in cand_members:
        fid = int(f["id"])
        wc = geometry.contour_world(f["contour"], after_poses[fid])
        moved_placed.append({"id": fid, "name": f["name"], "poly": Polygon(wc)})

    # 与锚组的轮廓内部重叠（允许共边/共点；候选组内部相对位姿不变，无需检查）
    overlaps = []
    for mc in moved_placed:
        for ar in anchor_placed:
            area = mc["poly"].intersection(ar["poly"]).area
            if area > geometry.OVERLAP_AREA_TOL:
                overlaps.append({
                    "candidate_fragment_id": mc["id"],
                    "anchor_fragment_id": ar["id"],
                    "candidate_name": mc["name"],
                    "anchor_name": ar["name"],
                    "area_mm2": area,
                })

    # 候选组在目标范围内的覆盖：先逐片裁入目标，再取并集
    cand_parts: list[Polygon] = []
    clipped_areas: dict[int, float] = {}
    for mc in moved_placed:
        clipped = mc["poly"].intersection(target)
        clipped_areas[mc["id"]] = clipped.area
        cand_parts.extend(_polygon_parts(clipped))
    cand_coverage = unary_union(cand_parts) if cand_parts else Polygon()

    # 净新增覆盖：候选覆盖中不属于锚组覆盖的部分（候选覆盖已裁入目标）
    net_new_geom = cand_coverage.difference(anchor_coverage)
    net_new_area = net_new_geom.area
    # 剩余缺口：目标 − 锚组 ∪ 候选组覆盖并集
    remaining_gap = target.difference(
        unary_union([anchor_coverage, cand_coverage]))
    remaining_area = remaining_gap.area

    rejection: list[str] = []
    if not within:
        rejection.append(REASON_ERROR_EXCEEDS_1MM)
    if overlaps:
        rejection.append(REASON_INTERNAL_OVERLAP)

    return {
        "index": i + 1,
        "candidate_fragment_id": int(c.candidate_fragment_id),
        "candidate_name": cand_frag["name"],
        "anchor_fragment_id": int(c.anchor_fragment_id),
        "anchor_side_name": anchor_side_frag["name"],
        "candidate_group_id": cand_frag.get("group_id"),
        "candidate_group_ids": [int(f["id"]) for f in cand_members],
        "pair_errors": pair_errors,
        "max_error": max_error,
        "within_tolerance": within,
        "overlaps": overlaps,
        "rejection_reasons": rejection,
        "eligible": not rejection,
        "rank": None,
        "transform": transform,
        "before_poses": before_poses,
        "after_poses": after_poses,
        "moved": moved_placed,
        "clipped_areas": clipped_areas,
        "markers_candidate_world": [
            geometry.apply_pose(m, moved_cand_pose) for m in markers_c],
        "markers_anchor_world": dst,
        "anchor_covered_area": anchor_coverage.area,
        "candidate_clipped_area": cand_coverage.area,
        "net_new_area": net_new_area,
        "remaining_gap_area": remaining_area,
    }


# ---------------------------------------------------------------- 响应整理

def _r4(v: float) -> float:
    v = round(float(v), 4)
    return 0.0 if v == 0 else v   # 归一 -0.0


def _r4_ring(ring) -> list[list[float]]:
    return [[_r4(x), _r4(y)] for x, y in ring]


def _ring_coords(ring) -> list[list[float]]:
    return [[x, y] for x, y in list(ring.coords)[:-1]]


def _format_polygon(poly: Polygon) -> dict:
    return {
        "outer_boundary": _r4_ring(_ring_coords(poly.exterior)),
        "holes": [_r4_ring(_ring_coords(r)) for r in poly.interiors],
        "area_mm2": round(poly.area, 6),
    }


def _pose_out(pose) -> dict:
    return {"x": _r4(pose[0]), "y": _r4(pose[1]),
            "theta_deg": _r4(math.degrees(pose[2]))}


def build_response(version: int, source: str, source_label_text: str,
                   created_at: str, current_version: int,
                   anchor_id: int, target_points, ev: dict) -> dict:
    """整理 POST .../repair-candidates 响应体（坐标/误差 4 位、面积 6 位）。"""
    anchor = ev["anchor"]
    anchor_gid = anchor.get("group_id")
    anchor_ids = [int(f["id"]) for f in ev["anchor_members"]]

    candidates_out = []
    for r in ev["rows"]:
        tx, ty, th = r["transform"]
        moved_poses = []
        for fid in r["candidate_group_ids"]:
            moved_poses.append({
                "fragment_id": fid,
                "name": next(m["name"] for m in r["moved"] if m["id"] == fid),
                "before": _pose_out(r["before_poses"][fid]),
                "after": _pose_out(r["after_poses"][fid]),
            })
        candidates_out.append({
            "index": r["index"],
            "candidate_fragment_id": r["candidate_fragment_id"],
            "candidate_name": r["candidate_name"],
            "anchor_fragment_id": r["anchor_fragment_id"],
            "anchor_side_fragment_name": r["anchor_side_name"],
            "candidate_group": (None if r["candidate_group_id"] is None
                                else {"id": r["candidate_group_id"],
                                      "fragment_ids": r["candidate_group_ids"]}),
            "moved_fragment_ids": r["candidate_group_ids"],
            "pair_count": len(r["pair_errors"]),
            "pair_errors_mm": [_r4(e) for e in r["pair_errors"]],
            "max_error_mm": _r4(r["max_error"]),
            "all_pair_errors_within_1mm": r["within_tolerance"],
            "overlaps": [
                {"candidate_fragment_id": o["candidate_fragment_id"],
                 "anchor_fragment_id": o["anchor_fragment_id"],
                 "candidate_name": o["candidate_name"],
                 "anchor_name": o["anchor_name"],
                 "area_mm2": round(o["area_mm2"], 6)}
                for o in r["overlaps"]],
            "has_internal_overlap": bool(r["overlaps"]),
            "eligible": r["eligible"],
            "rejection_reasons": r["rejection_reasons"],
            "rank": r["rank"],
            "transform": {"tx_mm": _r4(tx), "ty_mm": _r4(ty),
                          "theta_deg": _r4(math.degrees(th))},
            "moved_poses": moved_poses,
            "markers_world": {
                "anchor": [[_r4(x), _r4(y)] for x, y in
                           r["markers_anchor_world"]],
                "candidate": [[_r4(x), _r4(y)] for x, y in
                              r["markers_candidate_world"]],
            },
            "target_area_mm2": round(ev["target"].area, 6),
            "anchor_covered_area_mm2": round(r["anchor_covered_area"], 6),
            "candidate_clipped_area_mm2": round(r["candidate_clipped_area"], 6),
            "net_new_coverage_mm2": round(r["net_new_area"], 6),
            "remaining_gap_area_mm2": round(r["remaining_gap_area"], 6),
        })

    ranking = [
        {"rank": row["rank"], "candidate_index": row["index"],
         "candidate_fragment_id": row["candidate_fragment_id"],
         "candidate_name": row["candidate_name"],
         "anchor_fragment_id": row["anchor_fragment_id"],
         "net_new_coverage_mm2": round(row["net_new_area"], 6),
         "max_error_mm": _r4(row["max_error"])}
        for row in sorted((r for r in ev["rows"] if r["eligible"]),
                          key=lambda r: r["rank"])
    ]

    return {
        "ok": True,
        "version": version,
        "source": source,
        "source_label": source_label_text,
        "snapshot_created_at": created_at,
        "current_version": current_version,
        "historical": version != current_version,
        "anchor": {"id": int(anchor_id), "name": anchor["name"]},
        "group": (None if anchor_gid is None
                  else {"id": anchor_gid, "fragment_ids": anchor_ids}),
        "independent": anchor_gid is None,
        "anchor_fragment_ids": anchor_ids,
        "target_contour": [[_r4(x), _r4(y)] for x, y in target_points],
        "target_area_mm2": round(ev["target"].area, 6),
        "anchor_covered_area_mm2": round(ev["anchor_coverage"].area, 6),
        "anchor_coverage_polygons": [
            _format_polygon(p) for p in _polygon_parts(ev["anchor_coverage"])],
        "candidate_count": len(candidates_out),
        "eligible_count": len(ranking),
        "ranking": ranking,
        "candidates": candidates_out,
    }
