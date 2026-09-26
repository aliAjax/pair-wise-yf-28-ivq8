# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲、中期锁库批次和审计。

## 代码结构（三层分离）

- `repository.py`：数据记录层。SQLite 建表/迁移与纯数据访问，不含业务规则。
- `service.py`：业务事务层。角色校验、切点冻结、快照生成等全部业务规则在此，事务用 `BEGIN IMMEDIATE` 原子提交。
- `app.py`：展示层。HTTP 路由、JSON 编解码；`web/index.html` 为操作页面。
- `errors.py`：`BusinessError` 与时间工具。

`app.RandomizationStore` 保留为兼容门面（等价于 `RandomizationService`），旧脚本与测试无需改动。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。旧库启动时会自动补 `trials.enrollment_paused` 列并新建 `lock_batches` 表。测试：

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
- `GET /api/trials/{id}/summary`：中心级汇总和审计记录，含 `enrollment_paused` 与 `pending_lock_batch_id`。

### 中期锁库批次

- `POST /api/trials/{id}/lock-batches/initiate`（协调员）：在单个 `BEGIN IMMEDIATE` 事务内冻结数据切点（`cutoff_at`、受试者 ID 高水位、审计 ID 高水位），置 `enrollment_paused=1` 暂停新入组。已受理的紧急揭盲不受影响，照常双人办完。重复发起幂等返回同一待复核批次。
- `POST /api/lock-batches/{id}/review`（监查员）：
  - `{"approve": true}`：按冻结切点重放人数、中心分布和审计记录生成快照（`snapshot_json`），批次置 `completed`，恢复入组。
  - `{"approve": false, "note": "…"}`：不生成快照，批次置 `rejected`（驳回必须填写至少 4 字原因），恢复入组。
  - 已复核的批次不能再次复核；协调员不能复核、监查员不能发起。
- `GET /api/trials/{id}/lock-batches`、`GET /api/lock-batches/{id}`（协调员/监查员）：批次状态与历史。
- 切点规则：审计高水位在批次自身审计写入之前确定，因此锁库/复核动作不进当前批次；锁库期间或完成后才办完的“晚到揭盲”不会写进旧批次，只能进入后续批次的快照。
- 暂停期间入组返回 `423 enrollment_paused`；揭盲申请与审批接口不受暂停影响。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
