# 陶片拼接系统

面向文物修复人员的陶片拼接 Web 应用：录入毫米制闭合轮廓，基于对应边缘标记
做纯平移 + 旋转的刚体配准，支持预览、采纳、撤销与连通组管理。

## 功能

- **碎片录入**：画布点击或粘贴 JSON 录入毫米制闭合轮廓；自交轮廓整次拒绝。
- **候选拼接**：两片各至少 2 处对应边缘标记（数量一致、同片不重合）。
- **预览**：仅经平移 + 旋转的组合，显示各片位置 (x, y)、角度 (°)、每对
  对应点误差及冲突原因，不落库。
- **采纳校验**（任一不满足即整次拒绝，原拼接组不变）：
  - 同组对应点误差均 ≤ 1 mm；
  - 同组陶片轮廓内部不重叠（允许共边/共点）；
  - 合并两组时整体移动其中一组，组内相对位姿不变；
  - 同组内新增关系（闭环）必须符合已有位姿。
- **撤销**：按剩余有效关系重算连通组，孤片恢复独立状态。
- **有序试拼**：基于当前装配版本快照编排**非空有序**的操作序列（每步为
  「撤销一条现存有效关系」或「以两片陶片 + 成对边缘标记建立关系」），
  在内存快照上逐步推演预演，返回每步结果、最终位姿与分组（或首个失败
  原因），全程不改数据；确认时按同一版本与同一序列整体提交，任一步失败
  或版本过期均不写入任何变化，成功仅递增一次装配版本。
- **轮廓修订**：修复师发现某片陶片轮廓测量有误时，可选中该片提交
  修订后的毫米制闭合轮廓、**该片每一条有效拼接关系**在本片一侧的全新
  边缘标记及期望装配版本，先预览再确认。修订轮廓须通过现有非自交校验；
  新标记须位于修订轮廓边缘、数量与关系另一端一致、至少 2 处且同片不
  重合；随后**保持现有位姿不变**，检查受影响连通组全部有效关系的逐对
  对应点误差 ≤ 1 mm、组成员轮廓内部不重叠。独立碎片只校验轮廓。预览
  返回逐条误差与冲突原因、不落库；确认时重新校验同一输入并原子更新
  轮廓、相关标记与误差——位姿、关系 ID、连通分组与已撤销历史均不变，
  装配版本只 +1。遗漏任何有效关系、任一校验失败均整次拒绝；版本过期
  返回 409。
- **锚点整体校准**：修复师在「查看装配」页选所在组内任意一片作**锚点**
  请求整体校准。锚点位姿固定，只允许平移+旋转组内其余碎片，以**全部有效
  关系的成对边缘标记共同最小化对应点误差平方和**（最小二乘）；轮廓、标记、
  关系状态、关系 id 与连通分组均不变。预览给出调整前后各片位姿、逐关系逐
  对误差、误差总量与轮廓冲突，**不落库**。仅当**误差总量严格下降**、**每对
  对应点误差均 ≤ 1 mm** 且**组内轮廓内部不重叠**时可确认，否则列出原因并
  保持现状；确认携带预览依据的版本在同一事务内重算校验、更新位姿及每条
  有效关系的最新误差，版本只 +1 并留不可改快照。版本过期返回 409，锚点不
  存在返回 404，锚点所在组不足两片（孤片）拒绝且不写入。
- **乐观并发**：采纳 / 撤销 / 删除 / 试拼确认 / 轮廓修订确认 / 整体校准确认携带期望版本号，过期版本返回 409 冲突提示。
- **离线工作站迁移（导出 / 导入 JSON 包）**：修复师更换离线工作站时，可把
  **当前装配及全部不可改版本快照**导出为带格式版本号的 JSON 包
  （`format=pottery-assembly`、`format_version=1.0`），再导入另一台空白
  工作站。导入先**预览**包内碎片、有效及已撤销关系、版本数与导入后的装配，
  **不写库**；确认须携带目标库当前装配版本，在**单一事务**内保留碎片与关系
  的原 ID、轮廓、位姿、标记、状态（active / undone_at / created_at）、时间戳
  及各快照**原版本号、来源与快照时间**。目标库仅允许存在无碎片无关系的
  **空白基线**（不得有任何用户碎片或关系），该基线由导入包整包替换；导入
  不产生新版本号、不新增快照，装配版本直接置为包内当前版本。格式版本不支持、
  轮廓无效、关系引用缺失、快照版本重复或缺号、末版与包内当前装配不一致时
  **整包拒绝并逐条指出原因**；目标版本过期返回 409，库与历史保持原状。导入
  成功后历史查看、恢复、试拼、间隙查询与继续拼接均可正常使用。
- **陶片对间隙查询**：修复师选择一个装配版本与若干陶片对，系统依据该
  版本快照的**固定轮廓与位姿**逐对计算轮廓边界的最短间隙、对应最近点
  坐标及是否发生内部重叠，按间隙从小到大、陶片编号排序返回；全程只读，
  不因计算改变位姿、关系或历史，也不产生新快照。缺少陶片、重复陶片对、
  跨版本或无效编号时整次拒绝并逐字段指出。间隙为零表示接触，负值（-1）
  仅用于标识重叠，不得当作可用间隙。
- **补配区域评估**：修复师在**任一装配版本**（当前版或历史版）中选一片
  作**组锚**，并直接绘制世界坐标毫米制的**目标器形闭合轮廓**；系统依据
  该版快照取锚片所在连通组（孤片按单片），按该版轮廓与位姿放置组内全部
  陶片，**先逐片裁入目标范围**（超出目标的部分不计）、**再取几何并集**
  （片间重叠只计一次），**目标减去并集得到缺口**。返回目标面积、已覆盖
  面积、缺口总面积，以及每个正面积缺口的外边界、内孔边界与面积；缺口按
  面积降序、面积相同时按中心坐标排序，仅共点（无公共边/面积）的缺口不
  合并；目标范围内独立的已覆盖岛表现为缺口多边形的内孔，孔洞拓扑保留。
  页面在历史装配上以绿色标覆盖、红色标缺口（虚线内孔）、棕色标目标轮廓。
  全程只读，不改变装配、关系与快照，不产生新版本；目标轮廓自交/面积为
  零、坐标非有限数、锚片编号非法、版本快照或锚片在该版不存在均整次拒绝
  并指出原因；无正面积缺口时返回空清单与零面积。
- **补配候选组评估**：修复师在**任一装配版本**（当前版或历史版）中选
  锚片、世界坐标**目标器形闭合轮廓**，并一次提交**若干候选**；每个候选
  指定**另一连通组**的一片陶片（候选片）、**锚组内**一片陶片（锚侧
  标记片，可为锚片自己）及两片**按序一一对应**的边缘标记（局部坐标，
  各 ≥2 处）。系统**只取该版快照**，锚组保持该版位姿、保持候选组
  **内部相对位姿**不变，对候选组整体施加一次平移+旋转（世界坐标最小二
  乘拟合标记），逐候选返回逐对对应点误差、与锚组的轮廓内部重叠、目标
  范围内**相对锚组的净新增覆盖面积**及**剩余缺口面积**。仅**每对误差
  均 ≤ 1 mm 且无内部重叠**的候选参与排名，依次按净新增覆盖降序、最大
  误差升序、候选片编号升序排列；**零收益照实显示**。同组候选、缺失版本
  或陶片、无效目标轮廓或边缘标记**整次拒绝并指出问题**；单个候选的拟合
  超差或碰撞**只标记该候选**（`eligible=false` 与原因），不影响其余结果。
  全程只读，不改装配、关系、版本或快照，不产生新版本。
- **不可改版本快照与历史恢复**：录入/删除碎片、采纳/撤销拼接、试拼确认、
  轮廓修订确认、锚点整体校准确认每次成功都与装配数据在**同一事务**内固化
  一条版本快照（碎片轮廓与位姿、有效及已撤销关系、连通组），快照一经写入
  不可改、不可删；校准前后两个版本均可在历史中查看/恢复；启用版本历史时
  （含旧库升级）把当前状态记为**基线**快照；预览与失败操作不产生快照。
  修复师可在「查看装配」页按版本只读回看，
  选择历史版先**预览恢复后的完整装配及与当前版的碎片/关系差异**（不写
  库），确认时提交预览依据的当前版本号：版本过期（409）或目标版不存在
  （404）均拒绝且不写入；成功后原子恢复该版碎片与关系的**原 ID 及状态**
  （含 active / undone_at / created_at、轮廓、标记、位姿），重算连通组，
  装配版本只 +1 并留下新的 `restore` 快照，旧快照全部保留仍可查。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

构建镜像时自动安装依赖，容器启动时自动初始化 SQLite 表结构；**仅数据库
首次初始化**时按开关写入 4 片演示陶片（详见下节）。

- **访问地址**：http://localhost:8000
- **端口**：`8000`（宿主机映射，可在 `docker-compose.yml` 的 `ports` 中修改）
- **API 文档**：http://localhost:8000/docs

## 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000 。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `data/app.db`（容器内 `/app/data/app.db`） | SQLite 数据库文件路径 |
| `SEED_DEMO_DATA` | `1` | **仅数据库首次初始化**时写入 4 片演示陶片；设为 `0` 关闭 |
| `SQLITE_BUSY_TIMEOUT_MS` | `10000` | 多个服务实例共用同一库文件时，写事务等待库级写锁的最长毫秒数；超时本次操作整体取消并返回 503（可刷新后重试） |

Compose 默认将数据库挂载到命名卷 `pottery-data` 持久化。

### 并发与装配提交边界（多实例共用数据库）

所有装配写操作——**录入 / 删除陶片、采纳 / 撤销关系、有序试拼确认、轮廓
修订确认、锚点整体校准确认、历史恢复**——都收束在同一个可复用的**装配
提交边界**（`DB.assembly_commit`）内，按固定顺序在**一个 SQLite 写事务**
中完成：

1. `BEGIN IMMEDIATE` 立即取得库级写锁（配合 `busy_timeout`，多个服务实例
   共用同一数据库文件时按**提交顺序**排队串行）；
2. 在事务内复核本次适用的期望版本（录入不携带期望版本，其余均携带），
   过期返回 `409`，并指出对方提交后的真实当前版本；
3. 在事务内复核各自业务条件（轮廓 / 标记 / 误差 / 碰撞 / 对象是否存在 /
   关系是否已撤销 / 目标版是否存在等），任一不满足整次拒绝；
4. 写入碎片 / 关系 / 位姿等状态，并按原规则更新连通组（轮廓修订与整体
   校准不改拓扑，边界内保持分组不变）；
5. 装配版本只**递增一次**；
6. 在同一事务内固化该版本的**同版不可改快照**后提交。

成功回执里的 `version` 与 `assembly` 都取自**该事务提交时刻**的库内结果，
因此即便修复师并发提交，成功方回执中的版本与装配必与本次提交一一对应
（`assembly.version == version`），不会读到他人随后提交的版本。业务校验
失败、对象不存在或版本过期都会整体回滚：**不留下部分碎片 / 关系 / 分组
状态、不递增版本、不写快照**；各类预览（关系 / 试拼 / 修订 / 校准 / 恢复 /
导入）与只读接口（当前装配、历史查看、间隙与补配评估、导出）在同一读
事务内取齐数据，绝不写入。迁移导入是唯一的例外：它走同一写事务与版本
复核，但**不经过装配提交边界**，原子保留包内原版本号与快照、不额外递增。

### 演示数据初始化时机

4 片演示陶片（陶片 A/B/C/D，版本 v1 基线）**只在数据库文件首次初始化
（文件不存在或为空文件、首次建表）且 `SEED_DEMO_DATA=1` 时写入一次**：

- 首次初始化时设 `SEED_DEMO_DATA=0`：数据库保持空白（仅 v0 空白基线），
  之后再改为 `1` 重启**不会补种**演示陶片；
- 已写入演示数据的库，即使后来通过删除接口清空全部陶片，或把历史恢复到
  空装配，重启后仍**保持删除/恢复后的原装配、版本号与不可改快照**，不会
  回填演示陶片，也不会新增版本；
- 通过迁移导入建立的库，重启后同样保持导入的原装配与历史，不补种、不
  新增版本，迁移导入对空白目标与既有历史的判定不因重启改变。

如需重新获得演示数据：备份后删除 `DATABASE_PATH` 指向的数据库文件（或换
用新卷），以 `SEED_DEMO_DATA=1` 启动，即会在全新库上重新播种。

## 碎片录入 / 删除 API 调用示例

录入与删除走标准 REST 接口，删除为乐观并发：须携带当前装配版本号
（见 `GET /api/assembly` 返回的 `version`），成功后返回新版本号。所有
写接口的成功回执都在同一写事务内生成，回执 `version` 与 `assembly` 对应
本次提交（多实例并发时同样成立，见上节「并发与装配提交边界」）。

```bash
# 录入碎片（毫米制、≥3 点的非自交闭合轮廓）
curl -X POST http://localhost:8000/api/fragments \
  -H 'Content-Type: application/json' \
  -d '{"name":"陶片E","contour":[[0,0],[40,0],[35,25],[10,30]]}'
# 201: {"ok":true,"fragment":{"id":5,"name":"陶片E", ...},"version":2,
#       "assembly":{"version":2,"fragments":[...],"groups":[...],"relations":[...]}}

# 查询当前装配版本（删除须携带）
curl http://localhost:8000/api/assembly
# {"version":2,"fragments":[...],"groups":[...],"relations":[...]}

# 删除碎片 #5（及其全部关系）；版本过期返回 409
curl -X DELETE 'http://localhost:8000/api/fragments/5?expected_version=2'
# {"ok":true,"version":3,"assembly":{...}}
```

并发提交（两个修复师 / 两个服务实例基于同一版本 v2 各发一次写操作）时按
提交顺序只可能有一方成功，另一方得到 `409` 且其数据、版本、快照不变：

```bash
# 两端同时提交（均携带 expected_version=2）
# 先提交者：201 {"ok":true,...,"version":3,"assembly":{"version":3,...}}
# 后提交者：409 {"detail":"版本冲突：当前数据版本为 v3，
#                     您基于 v2 的操作已过期，请刷新后重试"}
# 等待写锁超过 SQLITE_BUSY_TIMEOUT_MS（极端情况）：503，本次操作已整体取消，
# 未写入任何变化、版本与快照不变，可刷新到当前版本后重试。
```

> 注意：清空全部陶片或恢复到空装配只是正常的历史变更，重启**不会**因此
> 重新写入演示陶片（规则见上节）。

## 使用流程

1. **录入碎片**：点击「录入碎片」，在主画布单击添加轮廓顶点（毫米制，
   滚轮缩放、拖拽平移），≥3 个顶点后保存；或直接粘贴坐标 JSON。
2. **新建拼接**：点击「新建拼接」，选择碎片甲 / 乙，在两个小图中点击
   轮廓边缘放置对应标记（每片 ≥2 处、数量一致，按序号一一对应）。
3. **预览**：查看组合后的各片位置、角度、每对误差与冲突原因。
4. **采纳**：校验全部通过才落库；任何冲突整次拒绝，原拼接组不变。
5. **撤销**：在「查看装配」页对有效关系点「撤销」，连通组自动重算。
6. **有序试拼**（多步预演 + 一次确认）：
   - 在「新建拼接」页放好两片标记后点「**加入试拼**」，或在「查看装配」页
     点有效关系旁的「**试拼撤销**」，把操作排入序列；
   - 侧栏「有序试拼」自动向服务器预演，逐步显示可执行 / 冲突原因，主画布
     以蓝色展示预演后的最终位姿与连通组（此时数据未改变）；
   - 全部步骤通过后点「**确认整组提交**」：按当前版本与整个序列原子写入，
     版本只 +1；任一步失败或版本过期则没有任何变化，按提示刷新后重试。
7. **修订轮廓**（测量有误时，先预览再确认）：
   - 在「查看装配」页点碎片卡片上的「**修订轮廓**」，进入修订页；
   - 在主画布单击绘制修订后的闭合轮廓（局部坐标、毫米制，可粘贴 JSON /
     一键重置为现轮廓）；
   - 每条有效拼接关系都有一个小图，在本片修订轮廓边缘点击放置新标记
     （自动吸附边缘，数量须与另一端一致、≥2 处、同片不重合；独立碎片
     无需标记）；
   - 点「**预览**」：以现有位姿在主画布显示受影响组的逐对对应点（绿色
     通过 / 红色冲突），侧栏列出逐关系逐条误差、最大误差、碰撞与冲突
     原因，此时**不写库**；
   - 预览通过后点「**确认修订**」：服务器用同一输入重新校验，原子更新
     轮廓、相关标记与误差；位姿、关系 ID、连通分组、已撤销历史均不变，
     装配版本只 +1；任何冲突整次拒绝且没有任何变化，版本过期按提示刷新。
8. **按版本查看与恢复**（历史只读 → 恢复预览 → 确认）：
   - 在「查看装配」页底部「**历史版本**」列表（新版本在前，标注变更来源
     与当前版）点「**查看**」，主画布以紫色只读展示该版的碎片轮廓、位姿、
     连通组与有效/已撤销关系，当前数据不变；
   - 点「**恢复预览**」（或历史版页的「恢复到此版本…」），主画布以蓝色
     展示恢复后的完整装配，侧栏列出与当前版的差异：碎片重现/删除/变更
     （轮廓、位姿等字段）、关系恢复/删除/变更（含「由撤销→有效」），
     以及恢复后连通组；预览**不写库**；
   - 点「**确认恢复**」：提交预览依据的当前版本号，成功则原子恢复该版
     碎片与关系的原 ID、原状态，重算连通组，装配版本只 +1，留下来源为
     「版本恢复」的新快照，旧快照仍可在列表中查看；版本过期或目标版已
     不存在则按提示刷新，不写入任何变化。
9. **整体校准**（选锚点，先预览再确认）：
   - 在「查看装配」页对组内碎片卡片点「**整体校准**」，把该片设为锚点
     （独立碎片无法校准）；
   - 点「**请求预览**」：服务端固定锚点位姿，以组内全部有效关系的成对边缘
     标记共同最小二乘，仅平移+旋转其余碎片；主画布以蓝色展示校准后位姿
     （绿色对应点通过 / 红色超限），侧栏列出调整前后各片 (x, y, 角度)、
     逐关系逐对误差（校准前 → 校准后）、误差总量、最大误差与轮廓内部
     重叠，此时**不写库**；
   - 仅当误差总量**严格下降**、每对对应点误差均 ≤ 1 mm 且组内轮廓内部不
     重叠时「**确认校准**」才可点：确认按预览依据的版本在同一事务内重算
     校验，原子更新其余碎片位姿与每条有效关系的最新误差；锚点位姿、轮廓、
     标记、关系状态、关系 id 与连通分组均不变，装配版本只 +1 并留
     「锚点整体校准」快照；
   - 不满足任一条件（误差未严格下降 / 仍有超限 / 产生轮廓重叠）则说明原因、
     保持现状；预览依据版本过期时按钮禁用，重新预览后再确认；校准前后两个
     版本都会出现在底部「历史版本」列表，可查看或恢复。
10. **离线工作站迁移**（导出包 → 空白工作站预览 → 确认导入）：
    - 在「查看装配」页底部「**离线工作站迁移**」点「**导出当前装配包
      （JSON）**」，下载带格式版本号的迁移包（含当前装配与全部不可改
      快照）；此操作只读，不改任何数据；
    - 在另一台**空白**工作站（仅启动生成过空白基线、从未录入碎片/关系）
      同一区块选择该 `.json` 文件，点「**导入预览（不写库）**」：侧栏与主
      画布（蓝色覆盖装配）展示包内碎片、有效及已撤销关系、快照版本清单、
      导入后连通组与完整装配；若目标库不是空白工作站，预览明确给出
      `ok=false` 与现状（碎片/关系/快照数），无法确认；
    - 核对无误后点「**确认导入（整包替换空白基线）**」：浏览器自动携带
      目标库当前装配版本；成功后碎片与关系的**原 ID、轮廓、位姿、标记、
      状态与全部时间戳**以及各快照的**原版本号 / 来源 / 快照时间**全部保留，
      装配版本即包内版本（不新增版本、不新增快照）；随后历史查看、恢复、
      试拼与间隙查询照常使用，新录入的碎片 / 关系 id 接续包内最大 id；
    - 格式版本不支持、轮廓无效、关系引用缺失、快照版本重复 / 缺号、末版与
      包内当前装配不一致 → 整包拒绝并逐条列出原因；目标版本过期（409）或
      目标库非空白（`target_not_blank`）均不写入，库与历史保持原状。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/fragments` | 录入碎片（自交轮廓 400 拒绝） |
| `GET` | `/api/fragments` | 碎片列表 |
| `DELETE` | `/api/fragments/{id}?expected_version=N` | 删除碎片及其关系 |
| `POST` | `/api/relations/preview` | 预览拼接（不落库） |
| `POST` | `/api/relations` | 采纳拼接（携带 `expected_version`） |
| `POST` | `/api/relations/{id}/undo` | 撤销关系（携带 `expected_version`） |
| `GET` | `/api/relations` | 关系列表（含已撤销历史） |
| `GET` | `/api/assembly` | 装配快照：版本、位姿、连通组、关系 |
| `GET` | `/api/assembly/versions` | 历史版本清单（版本/来源/快照时间，新版本在前） |
| `GET` | `/api/assembly/versions/{v}` | 只读查看历史版完整装配（轮廓/位姿/有效及已撤销关系/连通组） |
| `POST` | `/api/assembly/versions/{v}/gaps` | 陶片对间隙查询：按该版快照计算每对边界最短间隙/最近点/是否重叠（只读） |
| `POST` | `/api/assembly/versions/{v}/repair-assessment` | 补配区域评估：按组锚与世界坐标目标轮廓计算目标/已覆盖/缺口面积及逐个缺口边界（只读） |
| `POST` | `/api/assembly/versions/{v}/repair-candidates` | 补配候选组评估：整体平移+旋转各候选所在另一连通组拟合标记，返回误差/碰撞/净新增覆盖/剩余缺口与排名（只读） |
| `POST` | `/api/assembly/versions/{v}/restore/preview` | 恢复预览：恢复后完整装配 + 与当前版碎片/关系差异（不落库） |
| `POST` | `/api/assembly/versions/{v}/restore/confirm` | 确认恢复（原子恢复原 ID 与状态、重算连通组，版本只 +1，留新快照） |
| `POST` | `/api/trials/rehearse` | 有序试拼预演（不落库） |
| `POST` | `/api/trials/commit` | 有序试拼整组确认（原子，版本只 +1） |
| `POST` | `/api/fragments/{id}/revision/preview` | 轮廓修订预览（逐条误差/冲突，不落库） |
| `POST` | `/api/fragments/{id}/revision/confirm` | 轮廓修订确认（重新校验同一输入，原子更新，版本只 +1） |
| `POST` | `/api/calibration/preview` | 锚点整体校准预览（调整前后位姿/逐关系误差/轮廓冲突，不落库） |
| `POST` | `/api/calibration/confirm` | 锚点整体校准确认（重算校验，原子更新位姿与关系误差，版本只 +1） |
| `GET` | `/api/transfer/export` | 导出当前装配与全部不可改快照为带格式版本号的 JSON 包（只读；`?download=true` 附件下载） |
| `POST` | `/api/transfer/import/preview` | 导入预览：包内碎片、有效/已撤销关系、版本数、导入后装配（不落库） |
| `POST` | `/api/transfer/import/confirm` | 确认导入（单事务整包替换空白基线，保留原 ID/轮廓/位姿/标记/状态/时间戳/快照原版本号） |

错误约定：`400` 校验拒绝（轮廓自交 / 标记重合 / 标记不在边缘 / 标记数量
不一致 / 遗漏有效关系 / 误差超限 / 碰撞 / 空序列 / 撤销目标非法 / 目标
版本即当前版 / **锚点所在组不足两片** / **空陶片对列表 / 无效编号 /
同片成对 / 重复陶片对 / 跨版本** / **空候选列表 / 候选与锚同组 /
锚侧片不在锚组 / 候选标记不足、重合、数量不一致、不在该版轮廓边缘**），
`404` 对象不存在（碎片、关系、**目标版本快照**、**锚点碎片**缺失；版本恢复时
`detail.code=version_not_found`，校准时 `detail.code=fragment_not_found`，
**间隙查询时 `detail.code=fragment_not_found` 并逐字段指出缺失陶片，
补配评估时 `detail.code=anchor_not_found`（锚片在该版不存在），补配候选
评估时锚片缺失为 `anchor_not_found`、候选引用陶片缺失为
`fragment_not_found`（逐字段指出）**），
`409` 版本冲突或重复撤销（含恢复预览/确认、**校准预览/确认**、**导入确认**的期望版本
过期；多实例共用库时按提交顺序判定，败方事务整体回滚、状态/版本/快照不变），
`422` 请求体结构不合法，`503` 多实例竞争写锁等待超时（本次操作已整体取消，
不写入任何变化，可刷新到当前版本后重试）。补配评估的 `400` 代码为
`invalid_anchor`（锚片编号非正整数）、`non_finite_coordinate`（目标轮廓
含 NaN/Infinity，逐顶点字段指出）、`invalid_target_contour`（顶点不足 /
面积为零 / 自交）。迁移包本身的结构 / 轮廓 / 引用 / 快照版本
/ 末版一致性问题为 `400`（`detail.code` 见「离线工作站迁移 API」的整包拒绝
表，全部原因列于 `detail.reasons[]`）；目标库非空白为
`400 target_not_blank`，预览端点对非空白目标返回 `200` 且 `ok=false`。

## 有序试拼 API

两个端点请求体相同：`expected_version`（所基于的装配版本）+ 非空 `steps`。
每个 step 通过 `action` 区分：

```jsonc
// 建立关系：两片陶片 + 成对边缘标记（每片 ≥2 处、数量一致、同片不重合）
{ "action": "relate", "fragment_a": 1, "fragment_b": 2,
  "markers_a": [[35,0],[38,15]], "markers_b": [[1,0],[4,15]],
  "client_id": "s1" }          // 可选，序列内唯一，供后续撤销步引用

// 撤销：relation_id 撤销已落库关系；client_id 撤销序列内前序新建关系
{ "action": "undo", "relation_id": 3 }
{ "action": "undo", "client_id": "s1" }
```

### 预演（不落库）

```bash
curl -X POST http://localhost:8000/api/trials/rehearse \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 1,
    "steps": [
      {"action":"relate","fragment_a":1,"fragment_b":2,
       "markers_a":[[35,0],[38,15],[33,30]],
       "markers_b":[[1,0],[4,15],[-1,30]],"client_id":"s1"},
      {"action":"undo","client_id":"s1"},
      {"action":"relate","fragment_a":1,"fragment_b":2,
       "markers_a":[[35,0],[38,15]],
       "markers_b":[[1,0],[4,15]],"client_id":"s2"}
    ]
  }'
```

成功响应：`ok=true`，`steps[]` 含每步类型、对应点误差、涉及碎片位姿、
当步后的连通组；`assembly` 为最终位姿 / 分组 / 关系快照（预演新建关系使用
负的临时 id，`assembly.version` 仍为所基于的版本）。

任一步失败时 HTTP 仍为 `200`，但 `ok=false`：`failed_at` 与 `failure`
给出首个失败步、错误码（如 `relation_inactive` / `fragment_missing` /
`relation_not_found` / `client_relation_not_found` / `rejected`）、人类可读
原因与冲突明细；`steps[]` 只含失败步之前的成功步，`assembly` 为失败前一刻
快照。版本过期返回 `409`。

### 确认（整体提交）

```bash
curl -X POST http://localhost:8000/api/trials/commit \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":1,"steps":[
    {"action":"relate","fragment_a":1,"fragment_b":2,
     "markers_a":[[35,0],[38,15]],"markers_b":[[1,0],[4,15]],
     "client_id":"s1"}
  ]}'
# 201: {"ok":true,"version":2,"committed_steps":1,
#       "relation_ids":{"s1":1},"assembly":{...}}
```

- 全部步骤通过 → `201`，返回新版本号、`client_id → 真实关系 id` 映射和
  最新装配快照，整个序列只递增一次版本；
- 任一步失败 → `400/404/409`，`detail` 含 `failed_at / step / action /
  code / message`，**不写入任何变化**；
- `expected_version` 过期 → `409`，同样不写入。

明确的报错约定：重复撤销（关系已失效）→ `409 relation_inactive`；碎片缺失
→ `404 fragment_missing`；关系 id 缺失 → `404 relation_not_found`；撤销
未知 `client_id` → `404 client_relation_not_found`；同片建立关系、标记数量
不足 / 重合、误差 >1mm、轮廓内部重叠、闭环不符位姿 → `400 rejected`
（冲突明细在 `conflicts[]`）；空序列 / 撤销同时给或不给目标 /
`client_id` 重复 → `400`。

## 轮廓修订 API

选中碎片 `#1`，提交**修订后的毫米制闭合轮廓** `contour`、**该片每一条有效
关系**在本片一侧的新边缘标记 `relations[]`，以及所基于的
`expected_version`。每个条目按 `relation_id` 指定关系、`markers` 给出本片
（关系的 A 侧或 B 侧由系统判定）修订后的标记；关系另一端的标记不可在此
修改。

新标记的约束与建拼一致并额外要求**位于修订轮廓边缘**：数量与另一端一致、
每侧 ≥2 处、同片不重合；遗漏任何一条有效关系整次拒绝。独立碎片
（无有效关系）提交空 `relations`，只做轮廓非自交校验。

### 预览（不落库）

```bash
curl -X POST http://localhost:8000/api/fragments/1/revision/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 3,
    "contour": [[0,0],[35,0],[38,15],[33,30],[37,45],[34,60],[0,60]],
    "relations": [
      {"relation_id": 1, "markers": [[35,0],[38,15],[33,30]]}
    ]
  }'
```

HTTP 恒为 `200`（版本过期为 `409`、碎片/关系不存在为 `404`），响应：

```jsonc
{
  "ok": true,
  "fragment_id": 1,
  "expected_version": 3,
  "independent": false,
  "missing_relation_ids": [],            // 未提交的有效关系 id（有则必拒绝）
  "contour": [[0.0,0.0], "..."],         // 规范化后的修订轮廓
  "conflicts": [],                       // 全局冲突原因（轮廓/标记/误差/碰撞）
  "involved_fragment_ids": [1, 2],       // 受影响连通组
  "relations": [
    { "relation_id": 1, "revised": true, "side": "a",
      "fragment_a": 1, "fragment_b": 2, "other_fragment_id": 2,
      "required_count": 3, "marker_count": 3,
      "errors": [0.0, 0.0, 0.0],         // 逐对对应点误差（mm，位姿保持不变）
      "max_error_mm": 0.0,
      "conflicts": [],                   // 该关系自身的冲突（超限/标记问题）
      "markers_a": [[35,0],"..."],       // 预览生效后的两侧标记（仅替换本片侧）
      "markers_b": [[1,0],"..."] }
  ],
  "overlaps": []                         // 组成员轮廓内部重叠明细（mm²）
}
```

- 受影响连通组内**不直接涉及**修订片的关系也会在 `relations[]` 中逐条核验
  （`revised=false`），其误差必须仍 ≤ 1 mm；
- `ok=false` 时 `conflicts[]` 汇总全部冲突，`relations[].conflicts` 给出逐
  关系明细，`overlaps[]` 给出碰撞双方与重叠面积；预览不写库，可修改后重预。

### 确认（原子更新，版本只 +1）

请求体与预览**完全相同**（服务端重新校验同一输入）：

```bash
curl -X POST http://localhost:8000/api/fragments/1/revision/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 3,
    "contour": [[0,0],[35,0],[38,15],[33,30],[37,45],[34,60],[0,60]],
    "relations": [
      {"relation_id": 1, "markers": [[35,0],[38,15],[33,30]]}
    ]
  }'
# 201: {"ok":true,"fragment_id":1,"version":4,"assembly":{...}}
```

- 全部通过 → `201`：原子写入修订轮廓、每条相关关系本片侧的新标记及最新
  最大误差；**位姿（x/y/theta）、关系 id、连通分组（group_id）、已撤销
  历史（active/undone_at/created_at）保持不变**，装配版本只 +1，返回最新
  装配快照；
- 任一校验失败 → `400`，`detail` 内附与预览相同结构的逐条结果
  （`conflicts / relations / overlaps / missing_relation_ids`），**不写入
  任何变化**；
- `expected_version` 过期 → `409` 冲突提示，不写入；碎片不存在 → `404`；
  提交了库里不存在的关系 id → `404 relation_not_found`；提交已撤销 / 不涉及
  本片的关系、同关系重复提交、遗漏有效关系、标记数量不一致 / 不足 2 处 /
  同片重合 / 不在修订轮廓边缘 → `400`。

## 锚点整体校准 API

修复师从当前装配选一片碎片作**锚点**，对其所在连通组请求整体校准：

- 锚点的位姿（x/y/theta）固定不变，只允许调整组内**其余**碎片的平移与
  旋转；轮廓、边缘标记、关系状态（active/undone_at/created_at）、关系 id
  与连通分组均不变；
- 以组内**全部有效关系**的成对边缘标记构造对应点，最小二乘共同最小化
  全部对应点世界坐标距离的误差平方和（高斯-牛顿/LM）；
- 预览不落库；确认必须满足三个条件，否则整次拒绝、保持现状：
  1. 误差总量（所有对应点误差平方和，mm²）**严格下降**；
  2. **每对**对应点校准后误差均 ≤ 1 mm；
  3. 组内成员轮廓（校准后位姿）**内部不重叠**（允许共边/共点）。

请求体两端点相同：`anchor_id`（锚点碎片）+ `expected_version`（预览所
依据的当前装配版本）。

### 预览（不落库）

```bash
curl -X POST http://localhost:8000/api/calibration/preview \
  -H 'Content-Type: application/json' \
  -d '{"anchor_id": 1, "expected_version": 3}'
```

HTTP 恒为 `200`（版本过期为 `409`、锚点不存在为 `404 fragment_not_found`、
锚点为孤片为 `400 group_too_small`），响应：

```jsonc
{
  "ok": true,                        // 三个条件是否全部满足（是否可确认）
  "anchor_id": 1,
  "expected_version": 3,
  "involved_fragment_ids": [1, 2, 3],
  "sse_before_mm2": 17.19,           // 校准前误差总量（全部对应点误差平方和）
  "sse_after_mm2": 0.0,              // 校准后误差总量
  "sse_strictly_decreased": true,    // 条件 1：误差总量严格下降
  "max_error_before_mm": 1.31,
  "max_error_after_mm": 0.0,
  "all_pair_errors_within_1mm": true,// 条件 2：每对对应点误差均 ≤ 1 mm
  "conflicts": [],                   // 不满足条件的全部原因（可多条）
  "poses": [                         // 调整前后各片位姿（锚点 anchor=true 且不变）
    { "fragment_id": 1, "name": "陶片A-左半", "anchor": true, "moved": false,
      "before": {"x": 0.0, "y": 0.0, "theta_deg": 0.0},
      "after":  {"x": 0.0, "y": 0.0, "theta_deg": 0.0} },
    { "fragment_id": 2, "name": "陶片B-右半", "anchor": false, "moved": true,
      "before": {"x": 40.6, "y": 0.4, "theta_deg": 1.72},
      "after":  {"x": 40.0, "y": 0.0, "theta_deg": 0.0} }
  ],
  "relations": [                     // 逐关系逐对对应点误差（校准前 → 校准后）
    { "relation_id": 1, "fragment_a": 1, "fragment_b": 2,
      "name_a": "陶片A-左半", "name_b": "陶片B-右半", "marker_pairs": 3,
      "errors_before": [0.72, 0.39, 0.71],
      "errors_after":  [0.0, 0.0, 0.0],
      "max_error_before_mm": 0.72, "max_error_after_mm": 0.0 }
  ],
  "overlaps": []                     // 条件 3：校准后轮廓内部重叠明细（mm²）
}
```

- `ok=false` 时 `conflicts[]` 会同时列出「误差总量未严格下降 / 某对误差
  超限（给出关系与序号）/ 哪两片轮廓重叠及面积」全部原因；预览不写库，
  可换锚点后重新预览。

### 确认（原子更新，版本只 +1）

请求体与预览**完全相同**（服务端以同一锚点重新演算并重新校验）：

```bash
curl -X POST http://localhost:8000/api/calibration/confirm \
  -H 'Content-Type: application/json' \
  -d '{"anchor_id": 1, "expected_version": 3}'
# 201: {"ok":true,"anchor_id":1,"version":4,"assembly":{...}}
```

- 全部条件满足 → `201`：在锁与同一事务内更新其余碎片位姿 (x/y/theta) 与
  每条有效关系的最新最大误差；**锚点位姿、轮廓、标记、关系状态、关系 id
  与连通分组保持不变**；装配版本只 +1，返回最新装配快照，并固化一条
  `source=calibration` 的不可改快照（校准前版本快照仍保留、可查看/恢复）；
- 任一条件不满足 → `400`，`detail` 内附与预览相同结构的结果
  （`conflicts / poses / relations / overlaps / sse_*`），**不写入任何变化**；
- `expected_version` 过期 → `409`，不写入；锚点不存在 → `404
  fragment_not_found`；锚点所在组不足两片 → `400 group_too_small`，均不写入。

## 历史版本 API

碎片录入 / 删除、拼接采纳 / 撤销、试拼确认、轮廓修订确认、锚点整体校准
确认每次成功都在同一事务内写入不可改快照（校准前后两个版本均可查看/恢复）；
服务启用时（含旧库升级、首次空库初始化）若尚无快照，
把当前状态记为 `baseline` 基线。快照来源 `source` 取值：

| source | 含义 |
| --- | --- |
| `baseline` | 基线（启用版本历史时记录，旧库升级为当前版补记） |
| `create` / `delete` | 录入 / 删除碎片 |
| `accept` / `undo` | 采纳 / 撤销拼接 |
| `trial` | 有序试拼整组确认 |
| `revision` | 轮廓修订确认 |
| `calibration` | 锚点整体校准确认 |
| `restore` | 历史版本恢复（恢复后留下的新快照） |

预览（拼接 / 试拼预演 / 修订预览 / **校准预览** / 恢复预览）与任何失败、
过期操作都**不产生快照**。

### 版本清单

```bash
curl http://localhost:8000/api/assembly/versions
```

```jsonc
{
  "current_version": 3,
  "versions": [
    { "version": 3, "source": "undo", "source_label": "撤销拼接",
      "created_at": "2026-10-05T08:01:38+00:00", "current": false },
    { "version": 2, "source": "accept", "source_label": "采纳拼接", "...": "…" },
    { "version": 1, "source": "baseline", "source_label": "基线（启用版本历史时记录）",
      "created_at": "…", "current": false }
  ]
}
```

### 查看历史版（只读）

```bash
curl http://localhost:8000/api/assembly/versions/2
```

响应与 `GET /api/assembly` 同结构，并附版本元信息；`assembly.historical=true`，
`assembly.version` 为被查看的版本。可查看该版**碎片轮廓、位姿（x/y/theta）、
连通组、全部关系（含 active=false 的已撤销关系及其 undone_at）**：

```jsonc
{
  "version": 2, "source": "accept", "source_label": "采纳拼接", "created_at": "…",
  "assembly": {
    "version": 2, "historical": true,
    "fragments": [ { "id": 1, "name": "陶片A-左半", "contour": [[0,0], "..."],
                     "x": 0, "y": 0, "theta": 0, "group_id": 1, "created_at": "…" } ],
    "groups": [ { "id": 1, "fragment_ids": [1, 2] } ],
    "relations": [ { "id": 1, "fragment_a": 1, "fragment_b": 2,
                     "markers_a": ["..."], "markers_b": ["..."], "max_error": 0.0,
                     "active": true, "created_at": "…", "undone_at": null } ]
  }
}
```

目标版本快照不存在 → `404 {"detail":{"code":"version_not_found", …}}`。

### 恢复预览（不落库）

请求体只含预览所依据的**当前**装配版本：

```bash
curl -X POST http://localhost:8000/api/assembly/versions/2/restore/preview \
  -H 'Content-Type: application/json' -d '{"expected_version":3}'
```

响应给出恢复后的完整装配（`restored_assembly`，按目标版碎片重算连通组）与
逐对象差异 `diff`；`new_version` 为确认成功后的版本（当前 +1）。预览不改
当前数据、不产生快照：

```jsonc
{
  "ok": true, "expected_version": 3, "target_version": 2,
  "source": "accept", "source_label": "采纳拼接", "snapshot_created_at": "…",
  "new_version": 4,
  "restored_assembly": { "version": 2, "historical": true,
    "fragments": ["..."], "groups": ["..."], "relations": ["..."] },
  "diff": {
    "current_version": 3, "target_version": 2,
    "fragments": {
      "restored": [ { "id": 5, "target": { "…": "目标版碎片（恢复后重现）" } } ],
      "removed":  [ { "id": 6, "current": { "…": "当前版碎片（恢复后删除）" } } ],
      "changed":  [ { "id": 2, "changed_fields": ["x","theta"],
                      "current": {"…": "…"}, "target": {"…": "…"} } ],
      "unchanged": [ { "id": 1 } ]
    },
    "relations": {
      "restored": [ { "id": 1, "target": { "active": true, "…": "曾被删除的关系随恢复重现" } } ],
      "removed":  [ { "id": 7, "current": { "…": "目标版之后建立、恢复后删除" } } ],
      "changed":  [ { "id": 1, "changed_fields": ["active","undone_at"],
                      "current": { "active": false, "undone_at": "…" },
                      "target":  { "active": true,  "undone_at": null } } ],
      "unchanged": [ { "id": 3, "active": true } ]
    },
    "groups": { "current":  ["..."], "target": ["..."] },
    "summary": { "fragments": { "restored": 1, "removed": 1, "changed": 1, "unchanged": 2 },
                 "relations": { "restored": 0, "removed": 1, "changed": 1, "unchanged": 0 } }
  }
}
```

- 期望版本过期 → `409`（同其它端点的版本冲突约定）；
- 目标版不存在 → `404 version_not_found`；目标版即当前版 →
  `400 target_is_current`（无需恢复）。

### 确认恢复（原子，版本只 +1）

请求体与预览**完全相同**（只提交 `expected_version`，即预览依据的当前版）：

```bash
curl -X POST http://localhost:8000/api/assembly/versions/2/restore/confirm \
  -H 'Content-Type: application/json' -d '{"expected_version":3}'
# 201:
# {"ok":true,"restored_from_version":2,"expected_version":3,"version":4,
#  "assembly":{ "version":4, "fragments":[…], "groups":[…], "relations":[…] }}
```

- 成功：在锁与同一事务内**原子恢复目标版碎片与关系的原 ID 及状态**
  （轮廓、位姿、标记、`max_error`、`active`、`created_at`、`undone_at`
  全部按该版原样），目标版之后新增的碎片/关系删除、删除的按原 ID 重现，
  随后按有效关系**重算连通组**；装配版本只 +1，并留下 `source=restore`
  的新快照；**旧快照（含被恢复版本身）全部保留、仍可查看/再次恢复**；
- `expected_version` 与当前版不符（预览后数据已被他人变更）→ `409`，
  不写入；目标版不存在 → `404 version_not_found`，不写入；目标版即当前版
  → `400`，不写入。

## 离线工作站迁移 API

修复师更换离线工作站时，把**当前装配及全部不可改版本快照**导出为带格式
版本号的 JSON 包，再导入另一台空白工作站。三个端点均为顶层 JSON：

| 端点 | 语义 |
| --- | --- |
| `GET /api/transfer/export` | 导出迁移包（只读，不产生新快照；`?download=true` 时 `Content-Disposition: attachment` 直接下载） |
| `POST /api/transfer/import/preview` | 整包校验 + 导入预览（**不写库**）；目标非空白时 HTTP 200、`ok=false` |
| `POST /api/transfer/import/confirm` | 确认导入（携带 `expected_version`，单事务整包替换空白基线） |

### 迁移包格式（`format_version` = `1.0`）

```jsonc
{
  "format": "pottery-assembly",       // 固定格式标识
  "format_version": "1.0",            // 格式版本号（不支持的版本整包拒绝）
  "exported_at": "2026-10-06T03:10:00+00:00",
  "current_version": 2,               // 包内当前装配版本
  "current_assembly": {               // 当前完整装配（末版快照与之逐对象一致）
    "version": 2,
    "fragments": [ { "id": 1, "name": "陶片A-左半", "contour": [[0,0], "..."],
                     "x": 0.0, "y": 0.0, "theta": 0.0, "group_id": 1,
                     "created_at": "…" } ],
    "groups": [ { "id": 1, "fragment_ids": [1, 2] } ],
    "relations": [ { "id": 1, "fragment_a": 1, "fragment_b": 2,
                     "markers_a": ["..."], "markers_b": ["..."], "max_error": 0.0,
                     "active": true, "created_at": "…", "undone_at": null } ]
  },
  "snapshots": [                      // 全部不可改快照，按版本升序，原版本号/时间
    { "version": 1, "source": "baseline", "created_at": "…", "state": { "…": "…" } },
    { "version": 2, "source": "accept",   "created_at": "…", "state": { "…": "…" } }
  ]
}
```

### 导出

```bash
# 直接获取 JSON（可重定向保存）
curl http://localhost:8000/api/transfer/export -o pottery-assembly-v2.json

# 或要求附件下载（响应头带建议文件名）
curl -OJ http://localhost:8000/api/transfer/export?download=true
```

导出只读：不改位姿、关系或历史，不产生新快照。

### 导入预览（不写库）

```bash
curl -X POST http://localhost:8000/api/transfer/import/preview \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{ "package": { "format": "pottery-assembly", "format_version": "1.0", "…": "…" } }
JSON
```

> `package` 字段即导出文件的完整 JSON 内容（文件上传 / 粘贴均可，包本身
> 不嵌在 `{}` 之外）。成功响应：

```jsonc
{
  "ok": true,                        // 目标库是否为可导入的空白工作站
  "package": {
    "format": "pottery-assembly", "format_version": "1.0",
    "exported_at": "…", "current_version": 2, "snapshot_count": 2,
    "versions": [ { "version": 1, "source": "baseline", "created_at": "…" },
                  { "version": 2, "source": "accept",   "created_at": "…" } ]
  },
  "target": { "current_version": 0, "fragment_count": 0, "relation_count": 0,
              "snapshot_count": 1, "nonempty_snapshot_versions": [] },
  "fragment_count": 4,
  "active_relation_count": 1,
  "undone_relation_count": 0,
  "active_relations": [ "…" ],       // 包内有效关系
  "undone_relations": [],            // 包内已撤销关系（含 undone_at）
  "fragments": [ "…" ],
  "imported_assembly": { "version": 2, "fragments": ["…"],
                         "groups": [{ "id": 1, "fragment_ids": [1, 2] }],
                         "relations": ["…"] },
  "imported_version": 2,
  "reasons": []
}
```

- 目标库非空白（存在用户碎片 / 关系，或曾有非空快照）→ HTTP 仍为 `200`，
  但 `ok=false`、`reasons[]` 说明原因、`target` 给出现状；
- 包本身有问题 → `400`，`detail.code` 为主原因、`detail.reasons[]` 一次性
  列出**全部**拒绝原因。

### 确认导入（单事务原子写入）

```bash
curl -X POST http://localhost:8000/api/transfer/import/confirm \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "package": { "format": "pottery-assembly",
        "format_version": "1.0", "…": "…" }}'
# 201:
# {"ok":true,"imported_version":2,"snapshot_count":2,"expected_version":0,
#  "assembly":{ "version":2,"fragments":[…],"groups":[…],"relations":[…] }}
```

- 成功：在锁与**同一事务**内以包替换空白基线——碎片 / 关系保留**原 ID**，
  轮廓、位姿 (x/y/theta)、两侧标记、`max_error`、`active`、`created_at`、
  `undone_at` 全部原样；快照以**原版本号**、原 `source`、原 `created_at`、
  原 `state` 写回；`meta.version` 置为包内 `current_version`（**不新增版本、
  不新增快照**）；随后按有效关系重算连通组，AUTOINCREMENT 计数回退到包内
  最大 id（新碎片 / 关系接续编号，不跳号）；
- 导入后历史查看 (`GET …/versions/{v}`)、恢复预览 / 确认、试拼、间隙查询
  与继续采纳 / 撤销 / 修订 / 校准均基于原版本号正常工作；
- `expected_version` 与目标库当前版本不符 → `409`，库与历史保持原状；
- 目标库非空白 → `400 target_not_blank`（`detail.target` 给出现状），
  不写入。

### 整包拒绝原因（`detail.code`，400，全部原因在 `reasons[]` 列出）

| code | 触发条件 |
| --- | --- |
| `unsupported_format_version` | `format_version` 不受支持（仅接受 `1.0`） |
| `invalid_format` | 顶层 `format` 标识不是 `pottery-assembly` |
| `invalid_contour` | 任一碎片 / 快照轮廓不是有效的毫米制非自交闭合轮廓 |
| `relation_reference_missing` | 当前装配或任意外部快照中的关系引用了包内不存在的碎片 |
| `duplicate_snapshot_version` | 快照版本号重复 |
| `snapshot_version_gap` | 快照版本不连续（必须自 v0/v1 起无缺号） |
| `current_assembly_mismatch` | 末版快照与包内 `current_assembly` 逐对象不一致 |
| `invalid_current_version` / `invalid_snapshot_version` | 版本号不是非负整数、state.version 与快照版本不符等 |
| `invalid_fragment` / `invalid_relation` / `invalid_markers` / `invalid_timestamp` / … | 字段缺失、类型错误、标记不成对、空名称等结构问题 |
| `target_not_blank` | 仅确认端点：目标库存在用户碎片 / 关系或非空历史（空白基线除外） |

任何拒绝（含 400 / 409）都在事务提交前回滚，**库与历史保持原状**。

### 跨工作站完整调用示例

```bash
# 旧工作站：导出（库 data/app.db，当前 v2，含 v1/v2 两个快照）
curl http://old-host:8000/api/transfer/export -o pottery-v2.json

# 新工作站（空白库，启动后只有 v0 空白基线）
curl -s http://new-host:8000/api/assembly | jq '.version'      # => 0

# 1) 先预览（jq 包裹文件内容为 {"package": …}）
jq -Rs '{package: fromjson? // .}' pottery-v2.json \
  | curl -s -X POST http://new-host:8000/api/transfer/import/preview \
      -H 'Content-Type: application/json' --data-binary @- \
  | jq '{ok, fragment_count, active_relation_count, undone_relation_count,
         imported_version, groups: .imported_assembly.groups, reasons}'

# 2) 确认（携带预览时读到的目标库版本 0）
jq -Rs '{expected_version: 0, package: fromjson}' pottery-v2.json \
  | curl -s -X POST http://new-host:8000/api/transfer/import/confirm \
      -H 'Content-Type: application/json' --data-binary @- \
  | jq '{ok, imported_version, snapshot_count}'
# => {"ok": true, "imported_version": 2, "snapshot_count": 2}

# 历史与拼接照常：查看 / 恢复 v1、继续在 v2 上操作
curl http://new-host:8000/api/assembly/versions | jq '.versions[].version'  # 2, 1
```

## 陶片对间隙查询 API

修复师选择**一个装配版本**与**若干陶片对**，系统依据该版本快照中固定的
轮廓与位姿（世界坐标，毫米制）逐对计算：

- 两片轮廓**边界**的最短间隙 `gap_mm`；
- 对应的最近点坐标 `point_a` / `point_b`（分别位于两片轮廓边界上）；
- 是否发生内部重叠 `overlap`（允许共边/共点，面积重叠才算重叠，附
  `overlap_area_mm2`）。

**间隙语义**：`0` 表示两边界接触（`contact=true`）；负值（固定 `-1`）
**仅用于标识内部重叠**，无物理意义，**不得当作可用间隙**。结果按间隙
从小到大、再按陶片编号（`fragment_a`、`fragment_b`）排序返回。

**只读保证**：仅读取指定版本的不可改快照，不修改位姿、关系或历史，也
不产生新版本快照；当前版与任意历史版均可查询。

### 请求

```
POST /api/assembly/versions/{version}/gaps
```

```jsonc
{
  "pairs": [
    { "fragment_a": 1, "fragment_b": 2 },
    { "fragment_a": 1, "fragment_b": 3 },
    { "fragment_a": 2, "fragment_b": 4 }
    // 每对可选 "version": 必须与路径版本一致，否则视为跨版本整次拒绝
  ]
}
```

### 调用示例

```bash
curl -X POST http://localhost:8000/api/assembly/versions/2/gaps \
  -H 'Content-Type: application/json' \
  -d '{"pairs": [{"fragment_a": 1, "fragment_b": 2},
                 {"fragment_a": 1, "fragment_b":  3},
                 {"fragment_a": 2, "fragment_b": 4}]}'
```

响应（按间隙升序；演示数据下 #1/#3 重叠、#1/#2 共边接触、#2/#4 分离）：

```jsonc
{
  "ok": true,
  "version": 2,
  "source": "accept",
  "source_label": "采纳拼接",
  "snapshot_created_at": "2026-10-06T00:41:27+00:00",
  "current_version": 2,
  "historical": false,               // 查询的是否为历史版本
  "pair_count": 3,
  "pairs": [
    { "fragment_a": 1, "fragment_b": 3,
      "name_a": "陶片A-左半", "name_b": "陶片C",
      "gap_mm": -1.0,                // 负值仅标识内部重叠，非可用间隙
      "contact": false, "overlap": true,
      "overlap_area_mm2": 868.321429,
      "point_a": [0.0, 0.0], "point_b": [0.0, 0.0] },
    { "fragment_a": 1, "fragment_b": 2,
      "name_a": "陶片A-左半", "name_b": "陶片B-右半",
      "gap_mm": 0.0,                 // 间隙为零表示接触
      "contact": true, "overlap": false, "overlap_area_mm2": 0.0,
      "point_a": [38.0, 15.0], "point_b": [38.0, 15.0] },
    { "fragment_a": 2, "fragment_b": 4,
      "name_a": "陶片B-右半", "name_b": "陶片D",
      "gap_mm": 5.8835,              // 边界最短间隙（毫米）
      "contact": false, "overlap": false, "overlap_area_mm2": 0.0,
      "point_a": [35.7692, 3.8462], "point_b": [30.0, 5.0] }
  ]
}
```

### 整次拒绝（逐字段指出，不执行任何计算）

- `400 empty_pairs`：`pairs` 为空；
- `400 invalid_pairs`：`detail.errors[]` 逐字段列出全部问题——
  `invalid_fragment_id`（编号非正整数，字段如 `pairs[0].fragment_a`）、
  `same_fragment`（两片相同）、`duplicate_pair`（重复陶片对，不分先后）、
  `cross_version`（某对自带 `version` 与路径版本不一致，仅允许查询同一
  版本快照）；
- `404 fragment_not_found`：某对引用的碎片在该版本快照中不存在，
  `detail.errors[]` 逐字段指出（如 `pairs[0].fragment_b`）；
- `404 version_not_found`：目标版本快照不存在；
- `422`：请求体结构不合法（如编号为字符串）。

```bash
# 重复陶片对 + 无效编号 + 跨版本：一次全部指出
curl -X POST http://localhost:8000/api/assembly/versions/2/gaps \
  -H 'Content-Type: application/json' \
  -d '{"pairs": [{"fragment_a": 1, "fragment_b": 2},
                 {"fragment_a": 2, "fragment_b": 1},
                 {"fragment_a": 0, "fragment_b": 3},
                 {"fragment_a": 3, "fragment_b": 4, "version": 1}]}'
# 400 {"detail":{"code":"invalid_pairs","errors":[
#   {"field":"pairs[1]","code":"duplicate_pair", ...},
#   {"field":"pairs[2].fragment_a","code":"invalid_fragment_id", ...},
#   {"field":"pairs[3].version","code":"cross_version", ...}]}}
```

## 补配区域评估 API

修复师在**任一装配版本**（当前版或任意历史版）中：

1. 选一片陶片作**组锚** —— 系统取该版本快照中锚片所在**连通组**的全部
   陶片（快照 `group_id` 相同者；孤片 `group_id=null` 按单片一组）；
2. 直接在**世界坐标（毫米）**绘制**目标器形闭合轮廓** `target_contour`
   （即待补配的器形范围，≥3 个有限数值顶点，闭合时自动重复末点）。

系统按**该版本快照固化的轮廓与位姿**（仅平移+旋转，无缩放/镜像）放置
组内全部陶片：

- **先裁入目标范围**：每片世界轮廓与目标区域求交 —— 碎片超出目标的
  部分不计入已覆盖；
- **再取几何并集**：裁剪结果做并集 —— 组内片间重叠区域只计一次覆盖；
- **目标减去并集得到缺口**：`缺口 = 目标 − 已覆盖并集`。

**拓扑规则**：目标范围内**独立的已覆盖岛**使缺口多边形带上对应**内孔**
（`holes` 保留孔洞拓扑）；两个缺口**仅共点**（无公共边/面积）时**不
合并**，各自列为一条正面积缺口。缺口按 `area_mm2` **降序**排列，面积
相同时按中心（centroid）坐标 `x`、再 `y` **升序**。目标被完全覆盖时
`gap_count=0`、`gap_area_mm2=0`、`gaps=[]`。

**只读保证**：仅读取指定版本的不可改快照，不修改当前装配、位姿、关系
或历史，也不产生新版本快照；当前版与任意历史版均可评估。

### 请求

```
POST /api/assembly/versions/{version}/repair-assessment
```

```jsonc
{
  "anchor_id": 1,                       // 组锚碎片 id（须存在于该版本快照）
  "target_contour": [                   // 世界坐标、毫米制、闭合轮廓
    [0, 0], [100, 0], [100, 70], [0, 70]
  ]
}
```

### 调用示例

```bash
curl -X POST http://localhost:8000/api/assembly/versions/2/repair-assessment \
  -H 'Content-Type: application/json' \
  -d '{"anchor_id": 1,
       "target_contour": [[0,0],[100,0],[100,70],[0,70]]}'
```

响应（演示数据 v2：#1/#2 已共边拼接成组，A+B 覆盖 4800 mm²，目标
7000 mm²，右侧为单一缺口 2200 mm²）：

```jsonc
{
  "ok": true,
  "version": 2,
  "source": "accept",
  "source_label": "采纳拼接",
  "snapshot_created_at": "2026-10-06T00:41:27+00:00",
  "current_version": 3,
  "historical": true,                    // 可在历史版上评估
  "anchor": { "id": 1, "name": "陶片A-左半" },
  "group": { "id": 1, "fragment_ids": [1, 2] },
  "independent": false,                  // true 时 group=null，只评估锚片
  "fragment_count": 2,
  "fragment_ids": [1, 2],
  "target_contour": [[0.0,0.0],[100.0,0.0],[100.0,70.0],[0.0,70.0]],
  "target_area_mm2": 7000.0,
  "covered_area_mm2": 4800.0,            // 先裁剪再并集：超出不计、重叠一次
  "gap_area_mm2": 2200.0,                // = 各正面积缺口面积之和
  "gap_count": 1,
  "gaps": [
    {
      "index": 1,                        // 面积降序（并列按中心 x、y 升序）
      "area_mm2": 2200.0,
      "center_mm": [71.8182, 45.9091],   // 缺口中心（世界坐标，毫米）
      "outer_boundary": [[80.0,0.0], [100.0,0.0], /* ... 闭合环，无重复末点 */],
      "holes": [],                       // 内孔边界；覆盖岛使缺口带孔时非空
      "hole_count": 0
    }
  ],
  "coverage_polygons": [                 // 已覆盖并集多边形（外边界+内孔）
    { "outer_boundary": [/* ... */], "holes": [], "area_mm2": 4800.0 }
  ],
  "placed_fragments": [                  // 组内各片按该版位姿放置后的世界轮廓
    { "fragment_id": 1, "name": "陶片A-左半", "clipped_area_mm2": 2205.0,
      "contour_world": [[0.0,0.0], /* ... */] },
    { "fragment_id": 2, "name": "陶片B-右半", "clipped_area_mm2": 2595.0,
      "contour_world": [[35.0,0.0], /* ... */] }
  ]
}
```

完全覆盖（无正面积缺口）：

```jsonc
{ "target_area_mm2": 900.0, "covered_area_mm2": 900.0,
  "gap_area_mm2": 0.0, "gap_count": 0, "gaps": [],
  "coverage_polygons": [/* … */], "placed_fragments": [/* … */] }
```

独立覆盖岛（目标内孤片周围全是缺口，岛成为缺口的内孔）：

```jsonc
"gaps": [{ "index": 1, "area_mm2": 99.0,
           "outer_boundary": [/* 目标外环 */],
           "hole_count": 1,
           "holes": [[[2.0,2.0],[3.0,2.0],[3.0,3.0],[2.0,3.0]]] }]
```

### 页面用法

- **当前版**：在「查看装配」页找到「补配区域评估」卡片，点「开始补配
  评估」；
- **历史版**：在历史版本清单对任一版本点「查看」，侧栏底部同样提供
  「补配区域评估（历史版 vN）」卡片。

选择组锚碎片后在主画布单击绘制世界坐标毫米目标轮廓（也可粘贴 JSON
载入），点「评估（只读）」。画布以棕色实线标目标轮廓、绿色半透明标已
覆盖并集、红色标缺口并编号注明面积；缺口的虚线内边界即目标范围内的独立
已覆盖岛。评估不改任何数据，可反复改锚片 / 轮廓重新评估。

### 整次拒绝（不执行任何计算）

- `400 invalid_anchor`：锚片编号非正整数（`field=anchor_id`）；
- `400 non_finite_coordinate`：目标轮廓含 `NaN` / `±Infinity`，
  `detail.errors[]` 逐顶点指出（字段如 `target_contour[2].x`）；
- `400 invalid_target_contour`：顶点格式非法、有效顶点不足 3 个、
  面积为零（共线）或轮廓自交（`field=target_contour` 或
  `target_contour[i]`）；
- `404 anchor_not_found`：锚片在该版本快照中不存在（含「锚片存在于
  当前版但历史版尚未录入」的情形）；
- `404 version_not_found`：目标版本快照不存在；
- `422`：请求体结构不合法（如 `anchor_id` 为字符串、顶点少于 3 个）。

```bash
# 自交（蝴蝶结）轮廓 + 不存在的锚片演示：
curl -X POST http://localhost:8000/api/assembly/versions/2/repair-assessment \
  -H 'Content-Type: application/json' \
  -d '{"anchor_id": 1,
       "target_contour": [[0,0],[10,10],[10,0],[0,10]]}'
# 400 {"detail":{"code":"invalid_target_contour","errors":[
#   {"field":"target_contour","code":"invalid_target_contour",
#    "message":"轮廓自交：…"}]}}
```

## 补配候选组评估 API

修复师在**任一装配版本**（当前版或任意历史版）中选**锚片**、直接在
**世界坐标（毫米）**绘制**目标器形闭合轮廓**，并一次提交**若干候选**。
每个候选指定三片信息与两侧标记：

- `candidate_fragment_id`：**另一连通组**的一片陶片（候选片；其所在连通
  组整体移动，孤片即单片一组）；
- `anchor_fragment_id`：**锚组内**一片陶片（锚侧标记片，可以就是锚片
  自己）；
- `markers_candidate` / `markers_anchor`：两片**局部坐标**的边缘标记，
  各 ≥2 处、**数量一致、同片不重合、位于该片在该版轮廓的边缘**，
  **按序号一一对应**。

系统**只取该版不可改快照**（当前版或历史版均可）：

1. 锚组保持该版位姿不动；锚组覆盖 = 组内各片世界轮廓**先裁入目标范围、
   再取并集**（沿用补配区域评估语义）；
2. 对每个候选，保持其**候选组内部相对位姿**不变，以两侧标记的**世界
   坐标**做**一次**平移+旋转的最小二乘刚体拟合（`fit_rigid`，与历史
   拼接/校准同一套刚体配准），把同一变换复合到候选组**每一片**的该版
   位姿上；
3. 逐候选返回：
   - 逐对对应点误差 `pair_errors_mm` 与 `max_error_mm`；
   - 移动后候选组各片与**锚组**各片的轮廓内部重叠明细（允许共边/共点，
     面积重叠才算）；候选组内部相对位姿不变，故不检查组内；
   - `candidate_clipped_area_mm2`：候选组先裁入目标再并集的覆盖面积；
   - `net_new_coverage_mm2`（**净新增覆盖**）：候选覆盖减去锚组覆盖
     （与锚组重叠、超出目标的部分都不计）；
   - `remaining_gap_area_mm2`（**剩余缺口**）：目标减去「锚组 ∪ 候选组」
     覆盖并集后的面积。
4. **仅** `每对误差 ≤ 1 mm` **且** `与锚组无内部重叠` 的候选参与排名
   （`eligible=true`），依次按 **净新增覆盖降序 → 最大误差升序 →
   候选片编号升序** 排列，结果同时给出每个候选的 `rank` 与顶层
   `ranking`；**净新增为 0 的合格候选照实显示并照常排名**。单候选的
   拟合超差（`error_exceeds_1mm`）或碰撞（`internal_overlap`）只标记
   该候选（`eligible=false`、`rank=null`），其误差/面积指标仍照实给出，
   **不影响同批其余候选**。

**只读保证**：仅读取指定版本快照，不修改当前装配、位姿、关系、版本或
历史，也不产生新版本快照。

### 请求

```
POST /api/assembly/versions/{version}/repair-candidates
```

```jsonc
{
  "anchor_id": 1,                        // 组锚碎片 id（须存在于该版本快照）
  "target_contour": [[0,0],[100,0],[100,60],[0,60]],   // 世界坐标闭合轮廓
  "candidates": [
    {
      "candidate_fragment_id": 3,        // 另一连通组的一片（孤片亦可）
      "anchor_fragment_id": 1,           // 锚组内一片（可为锚片自己）
      "markers_candidate": [[0,5],[0,15]],   // 候选片局部边缘标记，按序对应
      "markers_anchor":    [[30,5],[30,15]]  // 锚侧片局部边缘标记，按序对应
    },
    {
      "candidate_fragment_id": 4,        // 另一个候选；其所在组成员一并整体移动
      "anchor_fragment_id": 2,           // 用锚组内另一片做锚侧标记片
      "markers_candidate": [[0,5],[0,15]],
      "markers_anchor":    [[3,45],[0,60]]
    }
  ]
}
```

### 调用示例

```bash
curl -X POST http://localhost:8000/api/assembly/versions/2/repair-candidates \
  -H 'Content-Type: application/json' \
  -d '{
    "anchor_id": 1,
    "target_contour": [[0,0],[100,0],[100,60],[0,60]],
    "candidates": [
      {"candidate_fragment_id":3,"anchor_fragment_id":1,
       "markers_candidate":[[0,5],[0,15]],
       "markers_anchor":[[30,5],[30,15]]},
      {"candidate_fragment_id":4,"anchor_fragment_id":1,
       "markers_candidate":[[20,5],[20,15]],
       "markers_anchor":[[30,5],[30,15]]}
    ]
  }'
```

响应（合格候选按净新增降序排名；不合格候选保留在提交序位置并标原因）：

```jsonc
{
  "ok": true,
  "version": 2,
  "source": "accept",
  "source_label": "采纳拼接",
  "snapshot_created_at": "2026-10-07T01:20:00+00:00",
  "current_version": 3,
  "historical": true,
  "anchor": {"id": 1, "name": "陶片A-左半"},
  "group": {"id": 1, "fragment_ids": [1, 2]},   // 锚组（孤片时 group=null）
  "independent": false,
  "anchor_fragment_ids": [1, 2],
  "target_contour": [[0.0,0.0],"..."],
  "target_area_mm2": 6000.0,
  "anchor_covered_area_mm2": 4800.0,            // 锚组在目标内的覆盖并集
  "anchor_coverage_polygons": [ /* 外边界/内孔/面积，结构同补配评估 */ ],
  "candidate_count": 2,
  "eligible_count": 1,
  "ranking": [                                  // 仅合格候选，按排名规则排序
    {"rank": 1, "candidate_index": 1,
     "candidate_fragment_id": 3, "candidate_name": "陶片C",
     "anchor_fragment_id": 1,
     "net_new_coverage_mm2": 820.0, "max_error_mm": 0.12}
  ],
  "candidates": [
    {
      "index": 1,                               // 提交序（从 1 起）
      "candidate_fragment_id": 3, "candidate_name": "陶片C",
      "anchor_fragment_id": 1, "anchor_side_fragment_name": "陶片A-左半",
      "candidate_group": null,                  // 候选为孤片；成组时给整组 id 与成员
      "moved_fragment_ids": [3],
      "pair_count": 2,
      "pair_errors_mm": [0.12, 0.05],
      "max_error_mm": 0.12,
      "all_pair_errors_within_1mm": true,
      "overlaps": [], "has_internal_overlap": false,
      "eligible": true,
      "rejection_reasons": [],
      "rank": 1,
      "transform": {"tx_mm": 40.0, "ty_mm": 0.0, "theta_deg": 0.0},
      "moved_poses": [                          // 候选组各片调整前/后位姿
        {"fragment_id": 3, "name": "陶片C",
         "before": {"x": 80.0, "y": 5.0, "theta_deg": 1.7},
         "after":  {"x": 40.0, "y": 0.0, "theta_deg": 0.0}}
      ],
      "markers_world": {                        // 拟合后两侧标记世界坐标
        "anchor": [[30.0,5.0],[30.0,15.0]],
        "candidate": [[29.88,5.05],"..."]},
      "target_area_mm2": 6000.0,
      "anchor_covered_area_mm2": 4800.0,
      "candidate_clipped_area_mm2": 820.0,      // 候选组先裁入目标再并集
      "net_new_coverage_mm2": 820.0,            // 相对锚组的净新增
      "remaining_gap_area_mm2": 1380.0          // 采纳本候选后的剩余缺口
    },
    {
      "index": 2,
      "candidate_fragment_id": 4, "candidate_name": "陶片D",
      "eligible": false,
      "rank": null,
      "all_pair_errors_within_1mm": true,
      "max_error_mm": 0.0,
      "rejection_reasons": ["internal_overlap"],   // 单候选碰撞：只标记自身
      "overlaps": [
        {"candidate_fragment_id": 4, "anchor_fragment_id": 1,
         "candidate_name": "陶片D", "anchor_name": "陶片A-左半",
         "area_mm2": 210.0}],
      "has_internal_overlap": true,
      "net_new_coverage_mm2": 610.0,            // 指标照实给出，但不参与排名
      "remaining_gap_area_mm2": 1390.0
      // "transform"/"moved_poses"/"markers_world"/... 字段同样完整返回
    }
  ]
}
```

- 误差超差时 `rejection_reasons` 含 `error_exceeds_1mm`
  （`all_pair_errors_within_1mm=false`、`max_error_mm>1`）；碰撞与超差
  可同时出现；零收益合格候选 `net_new_coverage_mm2=0.0`、`rank` 照常
  给出（与其他零收益候选按最大误差、候选片编号决胜）。
- 候选片所在连通组有多片时，`candidate_group.fragment_ids` 与
  `moved_fragment_ids` 列出整组、`moved_poses` 给出每片调整前后位姿，
  `candidate_clipped_area_mm2` 为整组裁入目标后的并集，`overlaps` 逐对
  列出组内各片与锚组各片的重叠。

### 整次拒绝（逐字段指出，不执行任何拟合）

请求级问题**整次拒绝**（不会只处理部分候选）：

- `400 empty_candidates`：`candidates` 为空列表；
- `400 invalid_anchor`：锚片编号非正整数（`field=anchor_id`）；
- `400 non_finite_coordinate`：目标轮廓或任一标记含 NaN / ±Infinity，
  `detail.errors[]` 逐顶点/逐标记指出（字段如
  `candidates[0].markers_candidate[2].y`）；
- `400 invalid_target_contour`：目标轮廓顶点格式非法、不足 3 个、面积
  为零或自交（与补配评估同一校验）；
- `400 invalid_candidates`：`detail.errors[]` 逐字段列出全部问题——
  `invalid_fragment_id`（编号非正整数）、`invalid_marker`（标记不是二元
  数值坐标）、`too_few_markers`（某侧不足 2 处）、`duplicate_marker`
  （同片标记重合）、`marker_count_mismatch`（两侧数量不一致）、
  `marker_not_on_edge`（标记不在该片**该版**轮廓边缘，局部坐标）、
  `same_group`（候选片与锚片属同一连通组）、
  `anchor_fragment_not_in_anchor_group`（锚侧标记片不在锚组内）；
- `404 anchor_not_found`：锚片在该版本快照中不存在（含「当前版存在、
  历史版尚未录入」）；
- `404 fragment_not_found`：候选引用的陶片在该版本快照中不存在，
  `detail.errors[]` 逐字段指出（如 `candidates[1].candidate_fragment_id`）；
- `404 version_not_found`：目标版本快照不存在；
- `422`：请求体结构不合法（如 `anchor_id` 为字符串、缺少
  `candidates` 字段）。

注意与**单候选失败**的区别：拟合误差超 1 mm 或与锚组碰撞**不会**导致
整次拒绝，该候选仍出现在 `candidates[]` 中（`eligible=false`、
`rejection_reasons` 标明原因、指标照实给出），其余候选正常评估排名。

```bash
# 同组候选 + 标记不在边缘：一次全部指出（400，不执行任何拟合）
curl -X POST http://localhost:8000/api/assembly/versions/2/repair-candidates \
  -H 'Content-Type: application/json' \
  -d '{"anchor_id":1,"target_contour":[[0,0],[100,0],[100,60],[0,60]],
       "candidates":[
         {"candidate_fragment_id":2,"anchor_fragment_id":1,
          "markers_candidate":[[5,5],[6,6]],
          "markers_anchor":[[30,5],[30,15]]}]}'
# 400 {"detail":{"code":"invalid_candidates","errors":[
#   {"field":"candidates[0].candidate_fragment_id","code":"same_group", ...},
#   {"field":"candidates[0].markers_candidate[0]","code":"marker_not_on_edge", ...},
#   {"field":"candidates[0].markers_candidate[1]","code":"marker_not_on_edge", ...}]}}
```

## 项目结构

```
app/
  main.py          # FastAPI 路由、单条拼接、版本控制、试拼/修订/校准/历史恢复/间隙查询/补配评估/补配候选组评估/离线迁移端点
  history.py       # 历史版本：来源标签、连通组重算、碎片/关系差异演算（只读）
  portable.py      # 离线工作站迁移：JSON 包导出、整包校验、导入预览演算（只读）
  revision.py      # 轮廓修订：边缘标记校验、受影响组误差/碰撞演算（只读）
  calibration.py   # 锚点整体校准：固定锚点的组内刚体最小二乘、误差/碰撞演算（只读）
  gaps.py          # 陶片对间隙查询：版本快照上的边界最短间隙/最近点/重叠演算（只读）
  repair.py        # 补配区域评估：组锚连通组按版位姿裁剪并集、缺口孔洞拓扑演算（只读）
  candidates.py    # 补配候选组评估：候选组整体刚体拟合标记、误差/与锚组碰撞/净新增覆盖/剩余缺口/排名演算（只读）
  trial.py         # 有序试拼：内存快照逐步推演 + 整组提交 + 位姿演算
  geometry.py      # 刚体配准（最小二乘）、轮廓/边缘标记校验、重叠检测
  database.py      # SQLite 持久化、读/写事务边界、可复用装配提交边界（版本复核/分组/递增/同版快照）、不可改版本快照、按版恢复、迁移包整包导入
  static/          # 浏览器二维画布前端（原生 JS）
tests/
  test_gaps.py     # 陶片对间隙查询 API 测试
  test_repair.py   # 补配区域评估 API/纯演算（孔洞、仅共点、排序、裁剪、只读、拒绝）测试
  test_repair_candidates.py # 补配候选组评估 API/纯演算（整组刚体、误差/碰撞标记、排名、零收益、只读、整次拒绝）测试
  test_transfer.py # 离线工作站迁移（导出/预览/确认/整包拒绝/空白约束/409）测试
  test_writes_concurrency.py # 装配提交边界：回执与本次提交一致、失败/过期原子回滚、预览只读、多实例共用库按提交顺序判过期、导入不额外递增
Dockerfile
docker-compose.yml
requirements.txt
```
