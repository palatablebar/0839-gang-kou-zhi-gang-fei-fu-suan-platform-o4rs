# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /appointments`：提箱预约排程工作台。
- `POST /api/appointments/plan`：按船期、排箱数和新增滞港费生成预约方案。
- `POST /api/appointments/replan`：名额变化后保留硬占位并重排未确认预约。
- `POST /api/appointments/{id}/confirm`：原子确认，支持幂等键和版本号。
- `POST /api/appointments/{id}/issue`（或 `/retry`）：签发/失败重试，幂等重试不重复占位。
- `GET /healthz`：健康检查。

预约模块的完整字段和业务顺序见 `docs/APPOINTMENTS.md`。
