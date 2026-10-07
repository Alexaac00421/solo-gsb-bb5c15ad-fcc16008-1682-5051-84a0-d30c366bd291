"""补配区域评估：在任一装配版本快照上，以组锚片圈定目标器形轮廓，
计算锚片所在连通组对目标范围的覆盖与缺口（世界坐标，毫米制）。

修复师在**任一装配版本**中选一片作为**组锚**，并直接在世界坐标系绘制
毫米制的**目标器形闭合轮廓**（待补配区域），系统取该版本不可改快照中
锚片所在连通组（孤片按单片一组），按该版轮廓与位姿放置组内全部陶片：

1. **先裁入目标范围**：每片世界轮廓与目标区域求交，超出目标的部分不计；
2. **再取几何并集**：裁剪结果做几何并集，片间重叠区域只计一次覆盖；
3. **目标减去并集得到缺口**：``target − union`` 即待补配缺口；缺口天然
   保留孔洞拓扑 —— 目标范围内独立的已覆盖岛表现为缺口多边形的内孔；
4. 仅共点（无公共边/面积）的缺口不合并，按各自独立的正面积缺口列出。

返回目标面积、已覆盖面积、缺口总面积，以及每个正面积缺口的外边界、
内孔边界与面积；缺口按面积降序、面积相同时按中心坐标（centroid 的 x、
y）升序排序。无正面积缺口时返回空清单与零缺口面积。

本模块只做**只读**演算：不改当前装配、位姿、关系与快照，也不产生新版本。
请求级校验整次拒绝并逐字段指出原因：目标轮廓坐标非有限数、轮廓自交/
面积为零/顶点不足、锚片编号非法、版本快照缺失、锚片在该版不存在。
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon
from shapely.ops import unary_union

from . import geometry
from .geometry import GeometryError

# 正面积缺口判定容差（mm²）：与轮廓内部重叠判定一致，小于该值视为零面积
POSITIVE_AREA_TOL = 1e-6


class RepairError(Exception):
    """请求级错误（整次拒绝），status 为对应 HTTP 状态码。

    errors 为逐字段错误明细：[{"field", "code", "message"}, ...]，
    field 形如 "target_contour[2].y"，指出请求体中出问题的字段。
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

def validate_anchor(anchor_id) -> None:
    """锚片编号必须为正整数；非法抛 RepairError(400 invalid_anchor)。"""
    if not isinstance(anchor_id, int) or isinstance(anchor_id, bool) \
            or anchor_id < 1:
        raise RepairError(
            400, "invalid_anchor",
            f"锚片编号非法（必须为正整数，当前为 {anchor_id!r}）",
            errors=[{"field": "anchor_id", "code": "invalid_anchor",
                     "message": "锚片 id 必须为正整数"}])


def validate_target(raw) -> list[tuple[float, float]]:
    """校验目标器形闭合轮廓（世界坐标，毫米），返回规范化顶点。

    逐点收集全部问题后一次性拒绝：
    - 顶点不是恰好两个数值 → invalid_point（field=target_contour[i]）；
    - 坐标为 NaN / ±Infinity → non_finite_coordinate
      （field=target_contour[i].x / .y）；
    - 顶点不足 / 面积为零 / 自交 → invalid_target_contour。
    """
    errors: list[dict] = []
    pts: list[tuple[float, float]] = []
    non_finite = False
    for i, p in enumerate(raw):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            errors.append({
                "field": f"target_contour[{i}]", "code": "invalid_point",
                "message": "目标轮廓每个顶点必须是恰好两个数值的坐标 [x, y]"})
            continue
        fx, fy = float(p[0]), float(p[1])
        if not math.isfinite(fx):
            non_finite = True
            errors.append({
                "field": f"target_contour[{i}].x",
                "code": "non_finite_coordinate",
                "message": f"目标轮廓第 {i + 1} 个顶点 x 坐标非有限数"
                           f"（NaN / Infinity 不允许）"})
        if not math.isfinite(fy):
            non_finite = True
            errors.append({
                "field": f"target_contour[{i}].y",
                "code": "non_finite_coordinate",
                "message": f"目标轮廓第 {i + 1} 个顶点 y 坐标非有限数"
                           f"（NaN / Infinity 不允许）"})
        if math.isfinite(fx) and math.isfinite(fy):
            pts.append((fx, fy))
    if errors:
        raise RepairError(
            400,
            "non_finite_coordinate" if non_finite else "invalid_target_contour",
            "目标器形轮廓坐标存在非有限数，整次拒绝（未执行任何计算）"
            if non_finite else
            "目标器形轮廓顶点格式非法，整次拒绝（未执行任何计算）",
            errors=errors)
    try:
        return geometry.validate_contour(pts)
    except GeometryError as e:
        raise RepairError(
            400, "invalid_target_contour",
            f"目标器形轮廓无效，整次拒绝：{e}",
            errors=[{"field": "target_contour",
                     "code": "invalid_target_contour", "message": str(e)}])


# ---------------------------------------------------------------- 几何工具

def _polygon_parts(geom) -> list[Polygon]:
    """从（多）多边形几何中摊平取出 Polygon 部件；忽略空几何与零面积的
    线 / 点接触部件（裁剪时两片仅共点会产生面积为 0 的几何）。"""
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom] if geom.area > POSITIVE_AREA_TOL else []
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if g.area > POSITIVE_AREA_TOL]
    # 防御：差异 / 相交理论上只产生（多）多边形；其余类型只取多边形部件
    out = []
    for g in getattr(geom, "geoms", []):
        out.extend(_polygon_parts(g))
    return out


def _ring_coords(ring) -> list[list[float]]:
    """环坐标，去掉末尾与起点重复的闭合点。"""
    return [[x, y] for x, y in list(ring.coords)[:-1]]


def _poly_record(poly: Polygon) -> dict:
    """多边形 → 外边界 / 内孔边界 / 面积 / 中心（未舍入）。"""
    c = poly.centroid
    return {
        "area": poly.area,
        "center": (c.x, c.y),
        "outer_boundary": _ring_coords(poly.exterior),
        "holes": [_ring_coords(r) for r in poly.interiors],
    }


# ---------------------------------------------------------------- 补配演算

def _group_members(snap: dict, anchor: dict) -> list[dict]:
    """锚片所在连通组的全部成员（按 id 排序）；孤片只返回锚片自己。

    连通组直接取版本快照固化的 group_id（与快照 groups、历史恢复后重算
    结果一致）：同组共享 group_id，group_id 为 NULL 表示孤片。
    """
    gid = anchor.get("group_id")
    if gid is None:
        return [anchor]
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
        for i, p in enumerate(f["contour"]):
            for axis, val in zip(("x", "y"), p):
                if not math.isfinite(float(val)):
                    errors.append({
                        "field": f"fragments[{fid}].contour[{i}].{axis}",
                        "code": "non_finite_coordinate",
                        "message": f"碎片 #{fid} 轮廓第 {i + 1} 个顶点 "
                                   f"{axis} 坐标非有限数"})
    if errors:
        raise RepairError(
            400, "non_finite_coordinate",
            f"版本 v{snap_version} 快照中锚片所在组存在非有限坐标，"
            "整次拒绝（未执行任何计算）",
            errors=errors, extra={"version": snap_version})


def evaluate_repair(snap: dict, anchor_id: int,
                    target_points: list[tuple[float, float]]) -> dict:
    """在版本快照上执行补配区域评估（纯只读演算，不写库）。

    锚片在该版不存在 → RepairError(404 anchor_not_found)；返回未舍入的
    演算结果（面积/坐标为 float，边界为坐标列表）。
    """
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

    members = _group_members(snap, anchor)
    _check_finite_members(members, int(snap["version"]))

    target = Polygon(target_points)
    placed: list[dict] = []
    clipped_parts: list[Polygon] = []
    for f in members:
        wc = geometry.contour_world(
            f["contour"], (f["x"], f["y"], f["theta"]))
        world_poly = Polygon(wc)
        clipped = world_poly.intersection(target)
        parts = _polygon_parts(clipped)
        clipped_parts.extend(parts)
        placed.append({
            "id": int(f["id"]),
            "name": f["name"],
            "world_contour": wc,
            "clipped_area": clipped.area,
        })

    # 先裁入目标范围，再取几何并集：超出目标部分不计，片间重叠只扣一次
    coverage = unary_union(clipped_parts) if clipped_parts else Polygon()
    gap_geom = target.difference(coverage)

    gap_polys = _polygon_parts(gap_geom)
    gaps = [_poly_record(p) for p in gap_polys]
    # 按面积降序；面积相同时按中心坐标（x、y）升序
    gaps.sort(key=lambda g: (-g["area"], g["center"][0], g["center"][1]))

    return {
        "version": int(snap["version"]),
        "anchor_id": int(anchor_id),
        "anchor_name": anchor["name"],
        "group_id": anchor.get("group_id"),
        "fragment_ids": [p["id"] for p in placed],
        "target_points": target_points,
        "target_area": target.area,
        "placed": placed,
        "coverage": coverage,
        "covered_area": coverage.area,
        "gaps_raw": gaps,
        "gap_area": sum(g["area"] for g in gaps),
    }


# ---------------------------------------------------------------- 响应整理

def _r4(v: float) -> float:
    v = round(float(v), 4)
    return 0.0 if v == 0 else v   # 归一 -0.0


def _r4_ring(ring) -> list[list[float]]:
    return [[_r4(x), _r4(y)] for x, y in ring]


def _format_polygon(poly: Polygon) -> dict:
    return {
        "outer_boundary": _r4_ring(_ring_coords(poly.exterior)),
        "holes": [_r4_ring(_ring_coords(r)) for r in poly.interiors],
        "area_mm2": round(poly.area, 6),
    }


def build_response(version: int, source: str, source_label_text: str,
                   created_at: str, current_version: int,
                   ev: dict) -> dict:
    """整理 POST .../repair-assessment 响应体（坐标 4 位、面积 6 位小数）。"""
    gaps_out = []
    for i, g in enumerate(ev["gaps_raw"], start=1):
        gaps_out.append({
            "index": i,
            "area_mm2": round(g["area"], 6),
            "center_mm": [_r4(g["center"][0]), _r4(g["center"][1])],
            "outer_boundary": _r4_ring(g["outer_boundary"]),
            "holes": [_r4_ring(h) for h in g["holes"]],
            "hole_count": len(g["holes"]),
        })
    coverage_out = [_format_polygon(p) for p in _polygon_parts(ev["coverage"])]
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
                  else {"id": ev["group_id"], "fragment_ids": ev["fragment_ids"]}),
        "independent": ev["group_id"] is None,
        "fragment_count": len(ev["fragment_ids"]),
        "fragment_ids": ev["fragment_ids"],
        "target_contour": [[_r4(x), _r4(y)] for x, y in ev["target_points"]],
        "target_area_mm2": round(ev["target_area"], 6),
        "covered_area_mm2": round(ev["covered_area"], 6),
        "gap_area_mm2": round(ev["gap_area"], 6),
        "gap_count": len(gaps_out),
        "gaps": gaps_out,
        "coverage_polygons": coverage_out,
        "placed_fragments": [
            {"fragment_id": p["id"], "name": p["name"],
             "clipped_area_mm2": round(p["clipped_area"], 6),
             "contour_world": [[_r4(x), _r4(y)] for x, y in p["world_contour"]]}
            for p in ev["placed"]
        ],
    }
