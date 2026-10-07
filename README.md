# 控制极夜能源负荷与保障级别基础服务

本项目提供极地科考站服务端应用共用的基础能力，用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。具体的物流、样品、能源、医疗和许可业务可在这些边界上扩展自己的状态、规则和接口。

在此基础之上，项目内置了站内能源承诺与负荷处置系统（`EnergyService`）：在可控制的时间基准上把发电、储能、燃料批次和分时需求连成逐区间预测，出现供应缺口时按保障等级、回路依赖、最低运行时长、启动代价、获批实验窗口和人工覆盖生成每个回路的保留或切除决定及依据。

## 目录

- src/polar_station_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `energy_planner.py`：纯函数规划器，逐区间模拟发电、储能、燃料与负荷处置；
  - `energy_service.py`：能源承诺与负荷处置服务，复用基础的幂等、事务与审计边界；
- tests/：基础规则、事务边界、接口路由、能源系统和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m polar_station_foundation.acceptance

验收命令会在临时 SQLite 数据库中登记科考机构、操作者、站点和业务资料，制定并封存一份能源方案，结算一次遥测并核对燃料与能量账目闭合，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 能源承诺与负荷处置

### 资产与约束登记

- `POST /energy/generators`：发电机（额定功率、燃料类型、单位耗油率）；
- `POST /energy/fuel-batches`：燃料批次（数量、可用时间，迟到批次不计入可用量）；
- `POST /energy/batteries`：储能（容量、充放功率、当前电量、效率）；
- `POST /energy/circuits`：配电回路（保障等级 1-9，1=医疗、2=通信、3=防冻；分时需求曲线；`depends_on` 上级回路；`group_id` 成组回路；最低运行区间数；启动能耗）；
- `POST /energy/experiment-windows`：获批实验窗口（审核员或管理员批准，窗口内必须供电）。

### 方案制定与封存

- `POST /energy/plans`：以当前时间为起点逐区间预测，生成草稿方案；
- `POST /energy/plans/seal`：封存方案；同一时段只能封存一个方案，后续确认请求得到同一个封存结果；
- `POST /energy/plans/replan`：只重算未来区段——当前区间的已执行指令原样继承，其余区间按最新供应与约束重新计算，旧方案标记为被替代；
- `POST /energy/fuel-batches/delay`、`POST /energy/equipment/status`：燃料迟到或设备状态变化后自动触发未来区段重算。

缺口处置规则：医疗、通信、防冻回路永远保持供电；其余回路按保障等级从低到高切除；成组回路只能整体保留或整体切除；上级回路被切除时下级回路随动切除；最低运行时长未满足的回路保持运行；启动代价计入区间能耗。

### 人工覆盖

- `POST /energy/overrides` 发起，`POST /energy/overrides/confirm` 由第二名操作者确认后生效；
- 覆盖仅在 `valid_from` 到 `valid_until` 期限内参与规划，过期自动失效；
- 医疗、通信、防冻回路不允许人工切除。

### 遥测结算与账目

- `POST /energy/telemetry`：按区间结算实际发电、用电与储能电量，扣减燃料批次并更新回路状态；同一 `telemetry_id` 重放不会再次结算，内容不一致则冲突；
- `GET /energy/energy-account?site_id=`：核验燃料账（批次消耗 = 结算耗油）与能量账（发电 = 用电 + 储能变化）是否闭合，列出未闭合区间。

### 运行查看

- `GET /energy/dispatch-state?site_id=`：当前区间的各回路处置决定与依据、未结束的限电周期（恢复运行后继续推进）、燃料与储能状态、预测告警；
- `GET /energy/plan-rationale?plan_id=`：方案内每个回路逐区间保留或切除的依据；
- `GET /energy/plans?site_id=`：方案列表（草稿、已封存、被替代）。
