"""候选补配评估：在任一装配版本快照上，以锚片与世界坐标目标轮廓评估
若干「另一连通组陶片」候选的补配收益（世界坐标，毫米制）。

修复师在**任一装配版本**中选一片作为**锚片**（沿用补配区域评估的组锚
语义：取该版快照中锚片所在连通组，孤片按单片），直接在世界坐标系绘制
毫米制的**目标器形闭合轮廓**，并提交若干**候选**；每个候选指定：

- 另一连通组的一片陶片 ``candidate_fragment_id``（候选片所在整组为
  「候选组」，不得与锚组同组，孤片视为独立组）；
- 锚组内一片陶片 ``anchor_fragment_id``；
- 两片**按序对应**的边缘标记 ``markers_candidate`` /
  ``markers_anchor``（各自局部坐标、位于本片轮廓边缘，每侧 ≥2 处、
  数量一致、同片不重合）。

系统只取该版快照：**保持候选组内部相对位姿不变**，以两侧标记在世界坐标
下的对应点做整体**平移 + 旋转**的最小二乘刚体拟合（不允许缩放/镜像），
逐候选返回：

1. 逐对对应点误差 ``pair_errors_mm`` 与最大误差；
2. 拟合后候选组与**锚组**的轮廓内部重叠（允许共边/共点）；
3. 目标范围内相对锚组的**净新增覆盖面积** —— 候选组裁入目标范围后、
   不与锚组已覆盖部分重复的面积（零收益照实显示）；
4. **剩余缺口面积** —— 目标减去「锚组 + 拟合后候选组」并集后的缺口。

仅当**每对误差均 ≤ 1 mm 且与锚组无内部重叠**时候选才参与排名；合格候选
依次按**净新增覆盖降序、最大误差升序、候选片编号升序**排列。

单个候选拟合超差或碰撞**只标记该候选**（``eligible=false`` +
reject_reasons），不影响其余候选结果；但请求级问题 —— 候选片与锚片同组、
版本或陶片在该版缺失、目标轮廓或边缘标记无效 —— **整次拒绝**并逐字段
指出问题。

本模块只做**只读**演算：不改当前装配、关系、版本与快照，也不产生新版本。
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from . import geometry
from .repair import (
    RepairError,
    _group_members,
    _polygon_parts,
    validate_anchor,
    validate_target,
)

# 1 mm 限值比较的数值容差（与 trial.py 建拼校验一致）
PAIR_TOL_MM = 1e-9


class CandidateError(Exception):
    """请求级错误（整次拒绝），status 为对应 HTTP 状态码。

    errors 为逐字段错误明细：[{"field", "code", "message"}, ...]，
    field 形如 "candidates[0].markers_candidate[1]"，指出请求体字段。
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


# ---------------------------------------------------------------- 请求校验

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_candidate_ids(version: int, candidates) -> list[dict]:
    """静态校验候选列表的编号（与快照内容无关），不通过抛 CandidateError。

    - 列表为空 → 400 empty_candidates；
    - 候选片 / 锚组片编号非正整数 → 400 invalid_fragment_id；
    - 候选片与锚组片相同 → 400 same_fragment。

    收集全部字段问题后一次性拒绝；返回保持提交顺序、附原始标记的规范化清单。
    """
    if not candidates:
        raise CandidateError(
            400, "empty_candidates", "候选列表不能为空，整次拒绝",
            errors=[{"field": "candidates", "code": "empty_candidates",
                     "message": "至少需要提交一个候选"}])
    errors: list[dict] = []
    norm: list[dict] = []
    for i, c in enumerate(candidates):
        cf, af = c.candidate_fragment_id, c.anchor_fragment_id
        for name, fid in (("candidate_fragment_id", cf),
                          ("anchor_fragment_id", af)):
            if not _is_int(fid) or fid < 1:
                errors.append({
                    "field": f"candidates[{i}].{name}",
                    "code": "invalid_fragment_id",
                    "message": f"无效编号：碎片 id 必须为正整数（当前为 {fid!r}）"})
        if _is_int(cf) and _is_int(af) and cf == af:
            errors.append({
                "field": f"candidates[{i}]", "code": "same_fragment",
                "message": f"候选片与锚组片相同（#{cf}），无法向本组补配"})
        norm.append({
            "index": i,
            "candidate_fragment_id": cf if _is_int(cf) else None,
            "anchor_fragment_id": af if _is_int(af) else None,
            "markers_candidate": c.markers_candidate,
            "markers_anchor": c.markers_anchor,
        })
    if errors:
        raise CandidateError(
            400, "invalid_candidates",
            "候选编号校验失败，整次拒绝（未执行任何计算）",
            errors=errors)
    return norm


def _validate_edge_markers(raw, contour, field: str,
                           label: str, errors: list[dict]
                           ) -> list[tuple[float, float]] | None:
    """校验单侧边缘标记：坐标有限、≥2 处、同片不重合、位于轮廓边缘。

    全部问题追加到 errors（field 为该侧标记字段前缀）；点列不足以继续
    校验时返回 None。
    """
    pts: list[tuple[float, float]] = []
    for j, m in enumerate(raw):
        if not isinstance(m, (list, tuple)) or len(m) != 2:
            errors.append({
                "field": f"{field}[{j}]", "code": "invalid_marker",
                "message": f"{label}第 {j + 1} 处标记必须是恰好两个数值的坐标"
                           " [x, y]"})
            continue
        try:
            fx, fy = float(m[0]), float(m[1])
        except (TypeError, ValueError):
            errors.append({
                "field": f"{field}[{j}]", "code": "invalid_marker",
                "message": f"{label}第 {j + 1} 处标记坐标必须为数值"})
            continue
        if not math.isfinite(fx) or not math.isfinite(fy):
            errors.append({
                "field": f"{field}[{j}]",
                "code": "non_finite_coordinate",
                "message": f"{label}第 {j + 1} 处标记含非有限数（NaN / "
                           "Infinity 不允许）"})
            continue
        pts.append((fx, fy))
    if len(pts) < 2:
        errors.append({
            "field": field, "code": "invalid_markers",
            "message": f"每片陶片至少需要 2 处对应边缘标记（{label}当前 "
                       f"{len(pts)} 处有效标记）"})
        return None
    for u in range(len(pts)):
        for v in range(u + 1, len(pts)):
            if geometry.dist(pts[u], pts[v]) < geometry.MARKER_DIST_TOL:
                errors.append({
                    "field": field, "code": "duplicate_marker",
                    "message": f"标记重合：{label}第 {u + 1} 处与第 "
                               f"{v + 1} 处标记位置相同"})
    boundary = LineString(list(contour) + [contour[0]])
    off = [j + 1 for j, p in enumerate(pts)
           if boundary.distance(Point(p)) > geometry.EDGE_TOL_MM]
    if off:
        errors.append({
            "field": field, "code": "marker_not_on_edge",
            "message": f"{label}标记必须位于本片轮廓边缘：第 "
                       f"{'、'.join(str(n) for n in off)} 处标记不在轮廓边缘上"})
    return pts if not any(e["field"] == field for e in errors) else None


def _group_members_from(frags: dict, f: dict) -> list[dict]:
    """候选片所在连通组全部成员；孤片只返回其自己。"""
    gid = f.get("group_id")
    if gid is None:
        return [f]
    return [g for g in frags.values() if g.get("group_id") == gid]


def validate_candidate_markers(candidates: list[dict], frags: dict,
                               anchor: dict, snap_version: int) -> list[dict]:
    """结合快照逐候选校验：陶片存在性、分组归属与两侧边缘标记。

    任一请求级问题即抛 CandidateError 整次拒绝（逐字段列出全部问题）：
    - 候选片 / 锚组片在该版快照缺失 → 404 fragment_not_found；
    - 候选片属于锚组（含候选片即锚片所在组）→ 400 same_group_candidate；
    - 锚组片不属于锚组 → 400 anchor_fragment_group_mismatch；
    - 两侧标记数量不一致 / 不足 2 处 / 同片重合 / 不在轮廓边缘 /
      坐标非有限 / 格式非法 → 400 invalid_candidates。
    返回保持提交顺序的规范化候选清单。
    """
    anchor_gid = anchor.get("group_id")
    group_desc = f"连通组 #{anchor_gid}" if anchor_gid is not None else "孤片组"
    errors: list[dict] = []
    missing: list[dict] = []
    norm: list[dict] = []
    for c in candidates:
        i = c["index"]
        cf_id, af_id = c["candidate_fragment_id"], c["anchor_fragment_id"]
        cf, af = frags.get(cf_id), frags.get(af_id)
        if cf is None:
            missing.append({
                "field": f"candidates[{i}].candidate_fragment_id",
                "code": "fragment_not_found",
                "message": f"候选片 #{cf_id} 在版本 v{snap_version} 快照中不存在"})
        if af is None:
            missing.append({
                "field": f"candidates[{i}].anchor_fragment_id",
                "code": "fragment_not_found",
                "message": f"锚组片 #{af_id} 在版本 v{snap_version} 快照中不存在"})
        if cf is None or af is None:
            continue

        cgid = cf.get("group_id")
        if cgid is not None and cgid == anchor_gid:
            errors.append({
                "field": f"candidates[{i}].candidate_fragment_id",
                "code": "same_group_candidate",
                "message": f"候选片 #{cf_id} 与锚片同属连通组 #{cgid}，候选片"
                           "必须指定另一连通组的陶片（孤片视为独立组）"})
            continue
        if af.get("group_id") != anchor_gid:
            errors.append({
                "field": f"candidates[{i}].anchor_fragment_id",
                "code": "anchor_fragment_group_mismatch",
                "message": f"锚组片 #{af_id} 不属于锚片所在{group_desc}"})
            continue

        markers_c = _validate_edge_markers(
            c["markers_candidate"], cf["contour"],
            f"candidates[{i}].markers_candidate", "候选片", errors)
        markers_a = _validate_edge_markers(
            c["markers_anchor"], af["contour"],
            f"candidates[{i}].markers_anchor", "锚组片", errors)
        if (markers_c is not None and markers_a is not None
                and len(markers_c) != len(markers_a)):
            errors.append({
                "field": f"candidates[{i}]", "code": "marker_count_mismatch",
                "message": f"两侧标记数量必须一致（候选片 {len(markers_c)} 对 "
                           f"锚组片 {len(markers_a)}）"})
            continue
        if markers_c is None or markers_a is None:
            continue

        members = sorted(_group_members_from(frags, cf),
                         key=lambda f: int(f["id"]))
        norm.append({
            "index": i,
            "candidate_fragment_id": cf_id,
            "anchor_fragment_id": af_id,
            "markers_candidate": markers_c,
            "markers_anchor": markers_a,
            "candidate": cf,
            "anchor_fragment": af,
            "members": members,
        })

    if missing:
        # 缺片为对象不存在问题：先于其余 400 字段问题整次拒绝
        raise CandidateError(
            404, "fragment_not_found",
            f"版本 v{snap_version} 快照中缺少候选所需陶片，整次拒绝",
            errors=missing, extra={"version": snap_version})
    if errors:
        raise CandidateError(
            400, "invalid_candidates",
            "候选边缘标记 / 分组归属校验失败，整次拒绝（未执行任何拟合）",
            errors=errors)
    return norm


# ---------------------------------------------------------------- 逐候选演算

def _pose_of(f: dict) -> tuple[float, float, float]:
    return (f["x"], f["y"], f["theta"])


def evaluate_one(anchor_polys: list, anchor_union,
                 target: Polygon, spec: dict) -> dict:
    """评估单个候选（纯只读）。单个候选超差/碰撞只在本行标记，不抛出。

    保持候选组内部相对位姿：把候选侧标记的当前世界坐标整体刚体拟合到锚组
    片标记的当前世界坐标，再把同一变换 T 复合到组内每片的快照位姿上。
    """
    cand_f = spec["candidate"]
    anchor_f = spec["anchor_fragment"]
    members = spec["members"]
    mc, ma = spec["markers_candidate"], spec["markers_anchor"]

    pose_c, pose_a = _pose_of(cand_f), _pose_of(anchor_f)

    # 拟合在世界坐标进行：src=候选侧标记当前世界点，dst=锚侧标记当前世界点
    src = [geometry.apply_pose(p, pose_c) for p in mc]
    dst = [geometry.apply_pose(p, pose_a) for p in ma]
    tx, ty, phi = geometry.fit_rigid(src, dst)

    new_poses = {
        int(f["id"]): geometry.compose_pose((tx, ty, phi), _pose_of(f))
        for f in members
    }

    # 逐对对应点误差（mm）
    new_pose_c = new_poses[int(cand_f["id"])]
    errors = [
        geometry.dist(geometry.apply_pose(p, new_pose_c),
                      geometry.apply_pose(q, pose_a))
        for p, q in zip(mc, ma)
    ]
    max_error = max(errors)

    # 候选组按拟合位姿放置（世界坐标）
    cand_polys: list[Polygon] = []
    member_rows = []
    for f in members:
        fid = int(f["id"])
        pose = new_poses[fid]
        wc = geometry.contour_world(f["contour"], pose)
        cand_polys.append(Polygon(wc))
        member_rows.append({
            "fragment_id": fid,
            "name": f["name"],
            "x": pose[0], "y": pose[1],
            "theta_deg": math.degrees(pose[2]),
            "contour_world": wc,
        })

    # 与锚组的轮廓内部重叠（候选组成员 × 锚组成员逐对，允许共边/共点）
    overlaps = []
    for f, cpoly in zip(members, cand_polys):
        for apoly, aid, aname in anchor_polys:
            area = apoly.intersection(cpoly).area
            if area > geometry.OVERLAP_AREA_TOL:
                overlaps.append({
                    "candidate_fragment_id": int(f["id"]),
                    "anchor_fragment_id": int(aid),
                    "candidate_name": f["name"],
                    "anchor_name": aname,
                    "area_mm2": area,
                })

    # 覆盖演算（沿用补配评估：先逐片裁入目标范围，再取几何并集）
    clipped: list[Polygon] = []
    clipped_areas: dict[int, float] = {}
    for f, cpoly in zip(members, cand_polys):
        inter = cpoly.intersection(target)
        clipped_areas[int(f["id"])] = inter.area
        clipped.extend(_polygon_parts(inter))
    cand_union = unary_union(clipped) if clipped else Polygon()
    if anchor_union.is_empty:
        combined = cand_union
    else:
        combined = unary_union([anchor_union, cand_union])
    gap_area = sum(p.area for p in _polygon_parts(target.difference(combined)))

    # 净新增：候选组在目标内、且未被锚组覆盖的面积（零收益照实为 0）
    net_new = cand_union.difference(anchor_union).area

    reasons: list[str] = []
    for idx, e in enumerate(errors):
        if e > geometry.MAX_ERROR_MM + PAIR_TOL_MM:
            reasons.append(
                f"误差超限：第 {idx + 1} 对对应点误差 {e:.4f} mm，"
                f"超过 {geometry.MAX_ERROR_MM:.0f} mm 限值")
    for o in overlaps:
        reasons.append(
            f"碰撞：候选组「{o['candidate_name']}」与锚组"
            f"「{o['anchor_name']}」轮廓内部重叠 {o['area_mm2']:.4f} mm²")

    return {
        "index": spec["index"],
        "candidate_fragment_id": int(cand_f["id"]),
        "candidate_fragment_name": cand_f["name"],
        "candidate_group_id": cand_f.get("group_id"),
        "candidate_group_fragment_ids": [int(f["id"]) for f in members],
        "anchor_fragment_id": int(anchor_f["id"]),
        "anchor_fragment_name": anchor_f["name"],
        "marker_pair_count": len(mc),
        "transform": {"tx": tx, "ty": ty, "theta_deg": math.degrees(phi)},
        "pair_errors_mm": errors,
        "max_error_mm": max_error,
        "within_tolerance": max_error <= geometry.MAX_ERROR_MM + PAIR_TOL_MM,
        "anchor_overlaps": overlaps,
        "has_internal_overlap": bool(overlaps),
        "candidate_covered_area_mm2": cand_union.area,
        "net_new_coverage_mm2": net_new,
        "remaining_gap_area_mm2": gap_area,
        "eligible": not reasons,
        "reject_reasons": reasons,
        "members": member_rows,
        "clipped_areas": clipped_areas,
    }


def get_anchor(snap: dict, anchor_id: int) -> tuple[dict, dict]:
    """取版本快照中的锚片及其余陶片索引；不存在抛 404 anchor_not_found。"""
    frags = {int(f["id"]): f for f in snap["fragments"]}
    anchor = frags.get(int(anchor_id))
    if anchor is None:
        raise RepairError(
            404, "anchor_not_found",
            f"锚片 #{anchor_id} 在版本 v{snap['version']} 快照中不存在，"
            "整次拒绝（未执行任何计算）",
            errors=[{"field": "anchor_id", "code": "anchor_not_found",
                     "message": f"碎片 #{anchor_id} 不存在于版本 "
                                f"v{snap['version']}"}],
            extra={"version": snap["version"], "anchor_id": anchor_id})
    return frags, anchor


def evaluate_candidates(snap: dict, anchor_id: int,
                        target_points: list[tuple[float, float]],
                        specs: list[dict]) -> dict:
    """在版本快照上评估全部候选（纯只读演算，不写库）。

    锚片在该版不存在 → RepairError(404 anchor_not_found)（与补配评估一致）；
    逐候选的超差/碰撞不抛出，只在该行 eligible/reject_reasons 标记。
    """
    frags, anchor = get_anchor(snap, anchor_id)

    anchor_members = _group_members(snap, anchor)
    target = Polygon(target_points)

    # 锚组在快照位姿下的世界轮廓与目标内覆盖（补配评估同一语义）
    anchor_polys: list[tuple] = []
    anchor_clipped: list[Polygon] = []
    anchor_placed = []
    for f in anchor_members:
        wc = geometry.contour_world(f["contour"], _pose_of(f))
        poly = Polygon(wc)
        anchor_polys.append((poly, int(f["id"]), f["name"]))
        inter = poly.intersection(target)
        anchor_clipped.extend(_polygon_parts(inter))
        anchor_placed.append({
            "fragment_id": int(f["id"]), "name": f["name"],
            "contour_world": wc,
            "clipped_area_mm2": inter.area,
        })
    anchor_union = unary_union(anchor_clipped) if anchor_clipped else Polygon()
    anchor_gap = sum(p.area for p in _polygon_parts(
        target.difference(anchor_union)))

    rows = [evaluate_one(anchor_polys, anchor_union, target, spec)
            for spec in specs]

    # 排名：仅合格候选；净新增覆盖降序 → 最大误差升序 → 候选片编号升序
    ranked = sorted(
        (r for r in rows if r["eligible"]),
        key=lambda r: (-r["net_new_coverage_mm2"],
                       r["max_error_mm"], r["candidate_fragment_id"]))

    return {
        "version": int(snap["version"]),
        "anchor_id": int(anchor_id),
        "anchor_name": anchor["name"],
        "group_id": anchor.get("group_id"),
        "anchor_fragment_ids": [int(f["id"]) for f in anchor_members],
        "target_points": target_points,
        "target_area": target.area,
        "anchor_covered_area": anchor_union.area,
        "anchor_gap_area": anchor_gap,
        "anchor_placed": anchor_placed,
        "rows": rows,
        "ranked": ranked,
    }


# ---------------------------------------------------------------- 响应整理

def _r4(v: float) -> float:
    v = round(float(v), 4)
    return 0.0 if v == 0 else v


def format_row(row: dict, rank: int | None) -> dict:
    """逐候选响应（坐标 4 位、面积 6 位小数；不合格 rank=null）。"""
    return {
        "index": row["index"],
        "rank": rank,
        "eligible": row["eligible"],
        "candidate_fragment_id": row["candidate_fragment_id"],
        "candidate_fragment_name": row["candidate_fragment_name"],
        "candidate_group_id": row["candidate_group_id"],
        "candidate_group_fragment_ids":
            row["candidate_group_fragment_ids"],
        "anchor_fragment_id": row["anchor_fragment_id"],
        "anchor_fragment_name": row["anchor_fragment_name"],
        "marker_pair_count": row["marker_pair_count"],
        "transform": {
            "tx_mm": _r4(row["transform"]["tx"]),
            "ty_mm": _r4(row["transform"]["ty"]),
            "theta_deg": _r4(row["transform"]["theta_deg"]),
        },
        "pair_errors_mm": [_r4(e) for e in row["pair_errors_mm"]],
        "max_error_mm": _r4(row["max_error_mm"]),
        "within_tolerance": row["within_tolerance"],
        "has_internal_overlap": row["has_internal_overlap"],
        "anchor_overlaps": [
            {"candidate_fragment_id": o["candidate_fragment_id"],
             "anchor_fragment_id": o["anchor_fragment_id"],
             "candidate_name": o["candidate_name"],
             "anchor_name": o["anchor_name"],
             "area_mm2": round(o["area_mm2"], 6)}
            for o in row["anchor_overlaps"]
        ],
        "candidate_covered_area_mm2":
            round(row["candidate_covered_area_mm2"], 6),
        "net_new_coverage_mm2": round(row["net_new_coverage_mm2"], 6),
        "remaining_gap_area_mm2": round(row["remaining_gap_area_mm2"], 6),
        "reject_reasons": row["reject_reasons"],
        "placed_group": [
            {"fragment_id": m["fragment_id"], "name": m["name"],
             "x_mm": _r4(m["x"]), "y_mm": _r4(m["y"]),
             "theta_deg": _r4(m["theta_deg"]),
             "clipped_area_mm2": round(
                 row["clipped_areas"][m["fragment_id"]], 6),
             "contour_world": [[_r4(x), _r4(y)] for x, y
                               in m["contour_world"]]}
            for m in row["members"]
        ],
    }


def build_response(version: int, source: str, source_label_text: str,
                   created_at: str, current_version: int,
                   ev: dict) -> dict:
    """整理 POST .../repair-candidates 响应体。"""
    rank_of = {id(r): i + 1 for i, r in enumerate(ev["ranked"])}
    candidates_out = [format_row(r, rank_of.get(id(r))) for r in ev["rows"]]
    ranking_out = [
        {"rank": rank_of[id(r)],
         "index": r["index"],
         "candidate_fragment_id": r["candidate_fragment_id"],
         "candidate_fragment_name": r["candidate_fragment_name"],
         "net_new_coverage_mm2": round(r["net_new_coverage_mm2"], 6),
         "max_error_mm": _r4(r["max_error_mm"]),
         "remaining_gap_area_mm2": round(r["remaining_gap_area_mm2"], 6)}
        for r in ev["ranked"]
    ]
    return {
        "ok": True,
        "version": version,
        "source": source,
        "source_label": source_label_text,
        "snapshot_created_at": created_at,
        "current_version": current_version,
        "historical": version != current_version,
        "anchor": {"id": ev["anchor_id"], "name": ev["anchor_name"]},
        "group": (None if ev["group_id"] is None
                  else {"id": ev["group_id"],
                        "fragment_ids": ev["anchor_fragment_ids"]}),
        "independent": ev["group_id"] is None,
        "anchor_group_fragment_ids": ev["anchor_fragment_ids"],
        "target_contour": [[_r4(x), _r4(y)] for x, y in ev["target_points"]],
        "target_area_mm2": round(ev["target_area"], 6),
        "anchor_covered_area_mm2": round(ev["anchor_covered_area"], 6),
        "anchor_gap_area_mm2": round(ev["anchor_gap_area"], 6),
        "candidate_count": len(candidates_out),
        "eligible_count": len(ranking_out),
        "candidates": candidates_out,
        "ranking": ranking_out,
    }
