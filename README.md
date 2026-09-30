# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`，必须提交`expected_version`；记录用`external_ref`（现场单号）识别，同号重传沿用首次结果，不重复写入
- `POST /api/items/{id}/records/batch`，批量提交记录，必须提交`expected_version`；整批原子写入，版本冲突时整批保留、可原号重试
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

事件、监测记录和关闭结论共用同一版本链：记录按提交时的事件版本落链，关闭结论与版本绑定。油膜厚度或岸线复油数据更新后，原关闭结论立即失效，事件退回复核，关闭权限暂停至重新核验（补入新版本下的回收与岸线监测记录）。两个终端同时提交记录时只接受当前版本，后到者收到冲突（409）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
