"""SQLite 持久化层：碎片、拼接关系、位姿、全局版本号与不可改版本快照。

位姿 (x, y, theta) 表示碎片局部坐标 → 世界坐标的刚体变换：
    world = R(theta) · local + (x, y)
group_id 为 NULL 表示孤片（独立状态）；同组碎片共享同一 group_id
（取组内最小碎片 id，撤销关系后按剩余关系重算连通分量得到）。

每次成功变更（录入/删除碎片、采纳/撤销拼接、试拼确认、轮廓修订确认、
锚点整体校准确认、版本恢复）都在同一事务内把完整装配状态（碎片、关系、
连通组）写入 snapshots 表，版本号即快照主键，快照一经写入不再修改或删除；
服务启动且尚无快照时，把当前状态记为基线快照。预览与失败操作不写快照。

装配提交边界（多实例共用同一数据库文件时同样成立）：
- ``read_tx``     只读边界：进程锁 + 单条读事务，保证一次请求内多语句读到
                  同一版本，且与并发写事务按提交顺序互斥；
- ``write_tx``    写边界：进程锁 + ``BEGIN IMMEDIATE`` 立即取得库级写锁，
                  多个服务实例的写操作在 SQLite 层按拿锁顺序串行提交；
- ``assembly_commit`` 可复用的装配提交边界：在**同一个写事务内**复核期望
                  版本与各自业务条件、写入状态、按原规则重算分组（可按
                  操作类型关闭）、递增一次版本、固化同版不可改快照并提交；
                  成功回执（version / assembly）必须取自该事务内提交时刻
                  的状态；任一步失败（含版本过期）一律回滚，不留部分状态、
                  版本或快照。
- 迁移导入走 ``write_tx`` 但**不**经 ``assembly_commit``：包内原版本与
  快照原样保留，不额外递增版本（见 ``import_package``）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

# 多实例共用同一 SQLite 文件时，BEGIN IMMEDIATE 等待他人写锁的最长时间；
# 超时仍拿不到锁才会抛 sqlite3.OperationalError("database is locked")。
# 可用环境变量 SQLITE_BUSY_TIMEOUT_MS 覆盖（默认 10 秒）。
BUSY_TIMEOUT_MS = int(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "10000"))


class VersionConflict(Exception):
    """装配提交边界在写事务内复核期望版本时发现版本过期。

    由 API 层（main.py）统一翻译为 HTTP 409；携带提交时刻的真实当前版本，
    保证多实例并发下也按提交顺序判定过期版本。
    """

    def __init__(self, expected: int, current: int):
        super().__init__(
            f"版本冲突：当前数据版本为 v{current}，"
            f"您基于 v{expected} 的操作已过期，请刷新后重试")
        self.expected = expected
        self.current = current


class _AssemblyCommit:
    """assembly_commit 边界的提交结果载体。

    new_version / assembly 在事务提交前（版本递增、快照固化的同一事务内）
    写入，成功回执只能取这两个值；result 供端点携带本次提交特有的结果
    （如新建碎片 / 关系 id、试拼 client_id 映射等）。
    """

    __slots__ = ("new_version", "assembly", "result")

    def __init__(self):
        self.new_version: int | None = None
        self.assembly: dict | None = None
        self.result: dict = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fragments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    contour    TEXT NOT NULL,           -- JSON [[x, y], ...] 毫米制闭合轮廓
    x          REAL NOT NULL DEFAULT 0,
    y          REAL NOT NULL DEFAULT 0,
    theta      REAL NOT NULL DEFAULT 0,
    group_id   INTEGER,                 -- NULL = 独立碎片
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fragment_a INTEGER NOT NULL,
    fragment_b INTEGER NOT NULL,
    markers_a  TEXT NOT NULL,           -- JSON [[x, y], ...] A 局部坐标
    markers_b  TEXT NOT NULL,           -- JSON [[x, y], ...] B 局部坐标
    max_error  REAL NOT NULL,           -- 采纳时最大对应点误差（毫米）
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    undone_at  TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    version    INTEGER PRIMARY KEY,       -- 该快照对应的装配版本（不可改、不可删）
    source     TEXT NOT NULL,             -- 变更来源（baseline/create/...，见 main.py）
    state      TEXT NOT NULL,             -- 装配状态 JSON（碎片/关系/连通组）
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # 多实例共用同一数据库文件：BEGIN IMMEDIATE 抢写锁时排队等待而非
        # 立即报 "database is locked"，使版本过期判定严格按提交顺序进行。
        self.conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # 建表前探测是否为从未初始化的数据库 —— 只有全新库才允许在启动时
        # 播种演示数据；既有库（哪怕碎片已删空 / 历史恢复到空装配 / 迁移
        # 导入后的库）重启一律保持原状，不补种、不新增版本与快照。
        self.freshly_initialized = self._is_fresh_database()
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('version', '0')")
        self.conn.commit()
        self.lock = threading.RLock()

    def _is_fresh_database(self) -> bool:
        """建表前判断该数据库是否从未初始化过（不含任何用户表）。

        不存在的文件 / 0 字节空文件首次 connect 时 sqlite_master 为空，
        返回 True；任何已含应用表结构的库均返回 False —— 即便当前无碎片
        无关系（已全部删除或已恢复到空装配），也属于「既有数据库」，
        重启后不得再次写入演示数据。
        """
        row = self.conn.execute(
            "SELECT count(*) FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchone()
        return row[0] == 0

    # ------------------------------------------------------------ 版本

    def version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'version'").fetchone()
        return int(row["value"])

    def bump_version(self):
        v = self.version() + 1
        self.conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'version'", (str(v),))
        return v

    # ------------------------------------------------------------ 事务边界

    @contextmanager
    def read_tx(self):
        """只读边界：进程锁内开一条只读事务。

        一次请求内的多条 SELECT 读到同一版本快照（BEGIN 后在 rollback
        journal 下读事务持有共享锁，其他实例的写提交须等待其结束）；
        绝不写入，因此以 ROLLBACK 结束，不改变任何数据、版本或快照。
        调用方无需再自行 ``with self.lock``（本边界已获取，RLock 可重入）。
        """
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                yield
            finally:
                self.conn.rollback()

    @contextmanager
    def write_tx(self):
        """写边界：进程锁 + ``BEGIN IMMEDIATE`` 立即取得库级写锁。

        多个服务实例共用同一数据库文件时，SQLite 写锁保证同时只有一个
        写事务：后来者在 busy_timeout 内排队，待前者 COMMIT 后取锁，因此
        「复核期望版本 → 写入 → 递增版本 → 写快照」对其他实例严格按提交
        顺序串行。块内抛异常（含 HTTPException / VersionConflict）一律
        ROLLBACK，不留下部分状态、版本或快照。
        """
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def check_version(self, expected: int | None = None):
        """在当前写事务内复核期望版本；过期抛 VersionConflict。

        expected=None 表示该操作不携带期望版本（录入碎片），不做复核。
        必须在 ``write_tx`` 已取得写锁后调用 —— 此时读到的就是本次提交
        所依据的最新版本，不受其他实例影响。
        """
        if expected is not None:
            current = self.version()
            if expected != current:
                raise VersionConflict(expected, current)

    @contextmanager
    def assembly_commit(self, source: str, expected_version: int | None, *,
                        recompute: bool = True):
        """可复用的装配提交边界：所有「改动装配、版本 +1、留同版快照」写操作
        （录入/删除碎片、采纳/撤销关系、试拼确认、轮廓修订确认、组校准确认、
        历史恢复）统一在此收束。

        固定流程（全部处于同一个 write_tx 事务内）：
        1. BEGIN IMMEDIATE 取得库级写锁（多实例按提交顺序串行）；
        2. 复核 expected_version（不携带则跳过，如录入碎片），过期抛
           VersionConflict → API 层翻译为 409；
        3. 执行业务回调（回调内再做各自业务条件校验与状态写入；校验失败
           直接抛异常，外层回滚，不写入任何变化）；
        4. 按原规则重算连通组（recompute=False 供轮廓修订 / 组校准，其
           分组必须保持不变）；
        5. 递增一次装配版本；
        6. 在同一事务内固化该版本的完整装配快照（不可改）；
        7. COMMIT，并把「提交时刻的新版本与装配状态」挂到边界对象上，
           成功回执只能取这两个值，确保回执与本次提交严格对应。

        用法::

            with db.assembly_commit("accept", expected) as c:
                ... 校验与写入 ...
                c.result = {"relation_id": rid}
            return {"ok": True, "version": c.new_version,
                    "assembly": c.assembly, **c.result}

        任何异常都整体回滚：不留下部分碎片/关系/分组状态、不递增版本、
        不写快照。
        """
        commit = _AssemblyCommit()
        with self.write_tx():
            self.check_version(expected_version)
            yield commit
            if recompute:
                self.recompute_groups()
            commit.new_version = self.bump_version()
            commit.assembly = self.assembly_state()
            self.save_snapshot(commit.new_version, source, commit.assembly)

    # ------------------------------------------------------------ 行转换

    @staticmethod
    def fragment_row(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "contour": json.loads(row["contour"]),
            "x": row["x"],
            "y": row["y"],
            "theta": row["theta"],
            "group_id": row["group_id"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def relation_row(row) -> dict:
        return {
            "id": row["id"],
            "fragment_a": row["fragment_a"],
            "fragment_b": row["fragment_b"],
            "markers_a": json.loads(row["markers_a"]),
            "markers_b": json.loads(row["markers_b"]),
            "max_error": row["max_error"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "undone_at": row["undone_at"],
        }

    # ------------------------------------------------------------ 查询

    def get_fragment(self, fid: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM fragments WHERE id = ?", (fid,)).fetchone()
        return self.fragment_row(row) if row else None

    def all_fragments(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM fragments ORDER BY id").fetchall()
        return [self.fragment_row(r) for r in rows]

    def group_members(self, group_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM fragments WHERE group_id = ? ORDER BY id",
            (group_id,)).fetchall()
        return [self.fragment_row(r) for r in rows]

    def all_relations(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM relations ORDER BY id").fetchall()
        return [self.relation_row(r) for r in rows]

    def get_relation(self, rid: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM relations WHERE id = ?", (rid,)).fetchone()
        return self.relation_row(row) if row else None

    # ------------------------------------------------------------ 写入

    def insert_fragment(self, name: str, contour) -> int:
        cur = self.conn.execute(
            "INSERT INTO fragments(name, contour, created_at) VALUES (?, ?, ?)",
            (name, json.dumps(contour), _now()))
        return cur.lastrowid

    def delete_fragment(self, fid: int):
        self.conn.execute(
            "DELETE FROM relations WHERE fragment_a = ? OR fragment_b = ?",
            (fid, fid))
        self.conn.execute("DELETE FROM fragments WHERE id = ?", (fid,))

    def update_pose(self, fid: int, pose):
        self.conn.execute(
            "UPDATE fragments SET x = ?, y = ?, theta = ? WHERE id = ?",
            (pose[0], pose[1], pose[2], fid))

    def update_fragment_contour(self, fid: int, contour):
        """仅更新轮廓；位姿（x/y/theta）、分组保持不变。"""
        self.conn.execute(
            "UPDATE fragments SET contour = ? WHERE id = ?",
            (json.dumps(contour), fid))

    def update_relation_side(self, rid: int, side: str | None, markers,
                             max_error: float):
        """更新关系的边缘标记与最新最大对应点误差。

        side="a"/"b" 时同时替换该侧标记（轮廓修订的本片侧）；side=None 时
        只刷新最大误差（受影响组内不涉及修订片、标记未变的关系）。
        关系 id、两侧碎片归属、active 状态、created_at、undone_at 均不变。
        """
        if side == "a":
            self.conn.execute(
                "UPDATE relations SET markers_a = ?, max_error = ? WHERE id = ?",
                (json.dumps(markers), max_error, rid))
        elif side == "b":
            self.conn.execute(
                "UPDATE relations SET markers_b = ?, max_error = ? WHERE id = ?",
                (json.dumps(markers), max_error, rid))
        else:
            self.conn.execute(
                "UPDATE relations SET max_error = ? WHERE id = ?",
                (max_error, rid))

    def insert_relation(self, fa: int, fb: int, markers_a, markers_b,
                        max_error: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO relations(fragment_a, fragment_b, markers_a, markers_b,"
            " max_error, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (fa, fb, json.dumps(markers_a), json.dumps(markers_b),
             max_error, _now()))
        return cur.lastrowid

    def undo_relation(self, rid: int):
        self.conn.execute(
            "UPDATE relations SET active = 0, undone_at = ? WHERE id = ?",
            (_now(), rid))

    # ------------------------------------------------------------ 连通组

    def recompute_groups(self):
        """按当前有效关系重算连通分量；孤片 group_id 置 NULL（独立状态）。

        只改组归属，不改任何位姿 —— 剩余组内相对位姿保持不变。
        """
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM fragments")]
        parent = {i: i for i in ids}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        for r in self.conn.execute(
                "SELECT fragment_a, fragment_b FROM relations WHERE active = 1"):
            a, b = r["fragment_a"], r["fragment_b"]
            if a in parent and b in parent:
                union(a, b)

        comps: dict[int, list[int]] = {}
        for i in ids:
            comps.setdefault(find(i), []).append(i)
        for members in comps.values():
            gid = min(members) if len(members) > 1 else None
            for m in members:
                self.conn.execute(
                    "UPDATE fragments SET group_id = ? WHERE id = ?", (gid, m))

    # ------------------------------------------------------------ 快照

    def assembly_state(self) -> dict:
        fragments = self.all_fragments()
        groups: dict[int, list[int]] = {}
        for f in fragments:
            if f["group_id"] is not None:
                groups.setdefault(f["group_id"], []).append(f["id"])
        return {
            "version": self.version(),
            "fragments": fragments,
            "groups": [
                {"id": gid, "fragment_ids": sorted(ids)}
                for gid, ids in sorted(groups.items())
            ],
            "relations": self.all_relations(),
        }

    def has_snapshot(self, version: int | None = None) -> bool:
        """是否存在快照；version=None 时检查是否存在任意快照（用于基线）。"""
        if version is None:
            return self.conn.execute(
                "SELECT 1 FROM snapshots LIMIT 1").fetchone() is not None
        return self.conn.execute(
            "SELECT 1 FROM snapshots WHERE version = ?", (version,)).fetchone() is not None

    def save_snapshot(self, version: int, source: str,
                      state: dict, *, ignore: bool = False):
        """把装配状态固化为不可改版本快照（与变更在同一事务内提交）。

        ignore=True 时使用 INSERT OR IGNORE —— 仅服务启动记基线时使用，
        已有快照（同版本）则保留原快照不动。
        """
        sql = ("INSERT OR IGNORE INTO snapshots" if ignore else "INSERT INTO snapshots")
        self.conn.execute(
            sql + " (version, source, state, created_at) VALUES (?, ?, ?, ?)",
            (version, source, json.dumps(state, ensure_ascii=False), _now()))

    def list_snapshot_versions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT version, source, created_at FROM snapshots"
            " ORDER BY version DESC").fetchall()
        return [{"version": r["version"], "source": r["source"],
                 "created_at": r["created_at"]} for r in rows]

    def get_snapshot_state(self, version: int) -> dict | None:
        row = self.conn.execute(
            "SELECT state FROM snapshots WHERE version = ?",
            (version,)).fetchone()
        return json.loads(row["state"]) if row else None

    def restore_state(self, state: dict):
        """把碎片与关系整体替换为快照内容（保留原 ID 与原状态/时间戳）。

        - 碎片以原 id 覆盖写回（含名称、轮廓、位姿、group_id、created_at），
          快照中不存在的碎片删除（含随之删除的关系）；
        - 关系以原 id 覆盖写回（含两侧标记、误差、active、created_at、
          undone_at），快照中不存在的关系删除；
        - 调用方随后必须 recompute_groups() 重算连通组。
        须在事务内调用。
        """
        snap_frags = {int(f["id"]): f for f in state["fragments"]}
        snap_rels = {int(r["id"]): r for r in state["relations"]}
        self.conn.execute("DELETE FROM fragments")
        self.conn.execute("DELETE FROM relations")
        for fid in sorted(snap_frags):
            f = snap_frags[fid]
            self.conn.execute(
                "INSERT INTO fragments(id, name, contour, x, y, theta,"
                " group_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fid, f["name"], json.dumps(f["contour"]),
                 f["x"], f["y"], f["theta"], f["group_id"], f["created_at"]))
        for rid in sorted(snap_rels):
            r = snap_rels[rid]
            self.conn.execute(
                "INSERT INTO relations(id, fragment_a, fragment_b, markers_a,"
                " markers_b, max_error, active, created_at, undone_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, r["fragment_a"], r["fragment_b"],
                 json.dumps(r["markers_a"]), json.dumps(r["markers_b"]),
                 r["max_error"], 1 if r["active"] else 0,
                 r["created_at"], r["undone_at"]))
        # AUTOINCREMENT 的 sqlite_sequence 只增不减，DELETE/显式 id 写回后
        # 仍保留历史最大值（会导致恢复后新碎片/关系跳号）。该内部表无主键
        # 约束，须显式 UPDATE 回退到快照内最大 id；无对应行时插入，空表则
        # 删除其计数行（后续 id 从 1 开始，且绝不与现存 id 冲突）。
        for table, ids in (("fragments", snap_frags), ("relations", snap_rels)):
            if ids:
                cur = self.conn.execute(
                    "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                    (max(ids), table))
                if cur.rowcount == 0:
                    self.conn.execute(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                        (table, max(ids)))
            else:
                self.conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name = ?", (table,))

    def import_package(self, pkg: dict):
        """把迁移包整体写入**空白**目标库（须先通过 portable.validate_package
        与空白基线校验，并在单事务内调用）。

        - 碎片 / 关系保留包内原 ID 与原状态：轮廓、位姿、标记、max_error、
          active、created_at、undone_at 全部按包原样写回；
        - 全部不可改快照以**原版本号**、原 source、原 created_at、原 state
          覆盖写回（目标库原有的空白基线被整包替换）；
        - meta.version 直接置为包内 current_version（导入不产生新版本、
          不递增装配版本，导入后历史查看 / 恢复 / 拼接仍基于原版本号）；
        - 不重置任何时间戳；调用方随后须 recompute_groups() 重算连通组。
        """
        asm = pkg["current_assembly"]
        snap_frags = {int(f["id"]): f for f in asm["fragments"]}
        snap_rels = {int(r["id"]): r for r in asm["relations"]}
        self.conn.execute("DELETE FROM snapshots")
        self.conn.execute("DELETE FROM fragments")
        self.conn.execute("DELETE FROM relations")
        for fid in sorted(snap_frags):
            f = snap_frags[fid]
            self.conn.execute(
                "INSERT INTO fragments(id, name, contour, x, y, theta,"
                " group_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fid, f["name"], json.dumps(f["contour"], ensure_ascii=False),
                 f["x"], f["y"], f["theta"], None, f["created_at"]))
        for rid in sorted(snap_rels):
            r = snap_rels[rid]
            self.conn.execute(
                "INSERT INTO relations(id, fragment_a, fragment_b, markers_a,"
                " markers_b, max_error, active, created_at, undone_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, r["fragment_a"], r["fragment_b"],
                 json.dumps(r["markers_a"], ensure_ascii=False),
                 json.dumps(r["markers_b"], ensure_ascii=False),
                 r["max_error"], 1 if r["active"] else 0,
                 r["created_at"], r["undone_at"]))
        for s in pkg["snapshots"]:
            self.conn.execute(
                "INSERT INTO snapshots(version, source, state, created_at)"
                " VALUES (?, ?, ?, ?)",
                (s["version"], s["source"],
                 json.dumps(s["state"], ensure_ascii=False), s["created_at"]))
        self.conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'version'",
            (str(pkg["current_version"]),))
        for table, ids in (("fragments", snap_frags), ("relations", snap_rels)):
            if ids:
                cur = self.conn.execute(
                    "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                    (max(ids), table))
                if cur.rowcount == 0:
                    self.conn.execute(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                        (table, max(ids)))
            else:
                self.conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name = ?", (table,))
