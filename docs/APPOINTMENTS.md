# 提箱预约排程

工作台在原有免堆日历和三档费率之外，提供闸口提箱预约排程页：打开 `/appointments`，或从首页右上角“提箱预约”进入。

## 决策顺序

排程器使用最小费用流求全局最优，而不是只按列表顺序抢占名额。目标按以下词法顺序执行：

1. **保接驳船期**：最大化能在截止班次/截止时间前提箱的箱数。
2. **尽量多排**：在船期结果最优后，最大化本班已安排箱数；其余为 `WAITING`，进入下一批。
3. **费用最低**：船期箱数和已排箱数相同时，最小化整批新增滞港费。

费用复用 `app.domain.helpers.tier_fee`：前 3 个计费日 8000 分/日，第 4–8 日 14000 分/日，第 9 日起 22000 分/日。可传入 `cost_basis_date` 计算相对该基准日的新增费；未传时按每个箱最早可排班次计算增量，也支持 `costs_by_shift`、`next_batch_cost_cents` 等显式费用。

## 名额与状态

- `PROPOSED` / `WAITING`：未确认方案，名额变化后可删除并重排。
- `ISSUING` / `ISSUE_FAILED` / `RESERVED`：已进入签发占位，占用班次名额；失败后用原预约或同一 `Idempotency-Key` 重试，不新增占位。
- `SENT` / `ISSUED` / `DISPATCHED` / `CONFIRMED`：已发给车队或已确认，名额变化时保持原班次不变，并作为锁定占用从剩余名额中扣除。

SQLite 确认使用 `BEGIN IMMEDIATE` 串行化同一数据库上的并发事务，并在事务内统计该班次的占用记录，因此并发确认不会超过 `gate_shifts.capacity`。

## 主要接口

- `GET /appointments`：预约工作台页面。
- `GET /api/appointments`：读取班次和预约状态。
- `POST /api/appointments/plan`：计算并保存方案；空请求使用内置样例。
- `POST /api/appointments/replan`：从库中读取硬占位和箱单快照，按剩余名额重排。
- `POST /api/appointments/capacity` 或 `POST /api/shifts/<shift_id>/capacity`：调整名额并重排。
- `POST /api/appointments/<id>/confirm`：原子确认，支持 `Idempotency-Key` 请求头或请求体字段。
- `POST /api/appointments/<id>/issue`：占位并签发；失败后可调用同路径 `/retry` 重试。

纯领域入口为 `app.domain.appointments.plan_appointments(payload)`，也支持 `plan_appointments(containers, shifts)` 位置参数；常见别名包括 `schedule_appointments`、`plan_pickups`、`arrange_appointments` 和 `replan_appointments`。
