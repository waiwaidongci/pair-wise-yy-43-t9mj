# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量和复核退回判定。
- `src/repository.py`：SQLite建表、事务、统一版本链、批次写入、关闭结论和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败场景和版本链测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库（旧库自动补列升级）。使用`X-Actor`和`X-Role`请求头传递身份。

## 统一版本链

事件、监测记录和关闭结论共享事件的`version`：

- 任何写入新记录或状态流转都必须携带`expected_version`；与当前版本不符返回`409`，后到终端刷新后重试。
- 监测记录以现场单号`field_ref`（兼容`external_ref`）唯一识别；同号重传不重复写、不推进版本，直接返回首次结果（响应中`replayed=true`）。
- 批量提交为单事务：服务层先校验整批，仓储层一次提交；任一条非法或版本冲突时整批不落库、版本不被消耗，可用原批次重试。

## 关闭结论与复核

- 关闭时写入关闭结论（`closures`，含结论文本与当时版本），事件详情通过`closure`字段返回最新结论及`valid`状态。
- 已关闭事件收到新的`oil_slick_thickness`（油膜厚度）或`shoreline_reoil`（岸线复油）记录时，最新关闭结论**立即失效**（保留留痕），事件由系统退回`review`复核状态并推进一个版本，审计追加`closure_invalidated`事件。
- 复核期间关闭权限暂停：必须先提交`kind=reverification`的重新核验记录，才能由`response_commander`再次关闭；其他类型记录不会使结论失效。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`，单条记录，新记录须提交`expected_version`
- `POST /api/items/{id}/record-batches`，批量提交`{"expected_version":n,"records":[...]}`
- `POST /api/items/{id}/transition`，必须提交`expected_version`，关闭时可附`conclusion`
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录，关闭结论失效后须重新核验。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
