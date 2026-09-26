# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲和审计。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`site1`、`site2`（研究中心），`coord`（协调员），`monitor1`、`monitor2`（监查员）。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度和随机种子。
- `POST /api/trials/{id}/protocol`：入组前修改方案；一旦入组即锁定。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/enroll`：按当前用户中心入组；响应只返回分配编号，不返回分组。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人不能审批两次。
- `GET /api/trials/{id}/summary`：中心级汇总和审计记录；`enrollment_paused` 表示正处于锁库复核期。

### 中期锁库批次

- `POST /api/trials/{id}/lock-batches`：协调员发起，立即记录数据切点（受试者/审计高水位）并暂停新入组；已受理的紧急揭盲仍可照常申请和双人审批。
- `POST /api/lock-batches/{id}/review`：监查员复核，`{"decision":"confirm"|"reject"}`。通过则生成切点快照（切点人数、中心分布、受试者明细、切点时待办揭盲、审计记录）并恢复入组；不通过则不生成快照并恢复入组。
- `GET /api/trials/{id}/lock-batches`：批次状态与历史；`GET /api/lock-batches/{id}`：单批次详情。中心用户只看状态，不返回完整快照。

切点采用发起事务内确定的 `cutoff_participant_id` / `cutoff_audit_id` 高水位，快照复核时按水位复算：锁库期间晚到的揭盲及其审批不会写入旧批次。页面（`/`）可查看批次状态、历史与快照明细。

## 代码分层

- `store.py`：数据记录层，SQLite 表结构（含 `db_lock_batches`）与随机化、揭盲数据读写；入组事务内含锁库门禁。
- `locking.py`：锁库批次事务编排层（`LockBatchRepository` + `LockBatchService`）。
- `app.py`：HTTP 路由与页面返回；`web/index.html`：页面展示。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
