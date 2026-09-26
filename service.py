"""业务事务层：规则校验与事务编排。

数据存取一律委托给 repository.RandomizationStore；
HTTP 与页面在 app.Handler 中。本层是锁库批次规则的唯一归属。
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3

from errors import BusinessError, now
from repository import DEFAULT_DB, MAX_ARM_LENGTH, RandomizationStore


class RandomizationService:
    def __init__(self, store: RandomizationStore | None = None, db_path=DEFAULT_DB):
        self.store = store or RandomizationStore(db_path)

    # ---- 通用 ----
    def init_schema(self):
        self.store.init_schema()

    def seed(self):
        self.store.seed()

    def connect(self):
        return self.store.connect()

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = self.store.get_active_user(conn, user_id)
        if not user:
            raise BusinessError("用户不存在或已停用", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _trial(self, conn, trial_id):
        row = self.store.get_trial(conn, trial_id)
        if not row:
            raise BusinessError("试验不存在", 404, "not_found")
        return row

    def _audit(self, conn, trial_id, actor, action, detail):
        self.store.insert_audit(conn, trial_id, actor, action, detail, now())

    # ---- 试验管理 ----
    def create_trial(self, user_id, name, protocol_version, arms, strata_factors, block_size, seed):
        name = name.strip()
        if len(name) < 3 or not protocol_version.strip() or len(seed.strip()) < 8:
            raise BusinessError("试验名称、方案版本和至少 8 位随机种子不能为空", 422, "invalid_trial")
        if not isinstance(arms, list) or len(arms) < 2:
            raise BusinessError("至少需要两个试验组", 422, "invalid_arms")
        arms = [str(a).strip() for a in arms]
        if any(not a or len(a) > MAX_ARM_LENGTH for a in arms) or len(set(arms)) != len(arms):
            raise BusinessError("试验组名称必须非空、唯一且不过长", 422, "invalid_arms")
        if not isinstance(strata_factors, list) or any(not str(x).strip() for x in strata_factors) or len(set(map(str, strata_factors))) != len(strata_factors):
            raise BusinessError("分层因素必须是非空且不重复的数组", 422, "invalid_strata")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < len(arms) or block_size % len(arms) != 0:
            raise BusinessError("区组长度必须为试验组数的正整数倍", 422, "invalid_block_size")
        with self.store.connect() as conn:
            self._user(conn, user_id, {"coordinator"})
            try:
                trial_id = self.store.insert_trial(
                    conn, name, protocol_version.strip(),
                    json.dumps(arms), json.dumps([str(x).strip() for x in strata_factors]),
                    block_size, seed.strip(), user_id, now(),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("试验名称已存在", 409, "trial_exists")
            self._audit(conn, trial_id, user_id, "trial.create", {"protocol_version": protocol_version, "arms": len(arms), "block_size": block_size})
            return {"id": trial_id, "name": name, "status": "draft", "arms": arms, "strata_factors": strata_factors, "block_size": block_size}

    def update_protocol(self, user_id, trial_id, protocol_version, arms=None, strata_factors=None, block_size=None, seed=None):
        with self.store.connect() as conn:
            self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            enrolled = self.store.participant_count(conn, trial_id)
            if enrolled or trial["status"] != "draft":
                raise BusinessError("入组开始后不能修改随机方案", 409, "protocol_locked")
            new_arms = arms if arms is not None else json.loads(trial["arms_json"])
            new_strata = strata_factors if strata_factors is not None else json.loads(trial["strata_factors_json"])
            new_block = block_size if block_size is not None else trial["block_size"]
            new_seed = str(seed) if seed is not None else trial["seed"]
            self.create_trial_validation_only(new_arms, new_strata, new_block, new_seed)
            self.store.update_protocol_row(
                conn, trial_id, protocol_version.strip(),
                json.dumps(new_arms), json.dumps(new_strata), new_block, new_seed,
            )
            self._audit(conn, trial_id, user_id, "protocol.update", {"protocol_version": protocol_version})
            return {"id": trial_id, "protocol_version": protocol_version, "arms": new_arms, "block_size": new_block}

    @staticmethod
    def create_trial_validation_only(arms, strata_factors, block_size, seed):
        if not isinstance(arms, list) or len(arms) < 2 or len(set(arms)) != len(arms):
            raise BusinessError("试验组配置无效", 422, "invalid_arms")
        if not isinstance(strata_factors, list) or not strata_factors or len(set(strata_factors)) != len(strata_factors):
            raise BusinessError("分层因素配置无效", 422, "invalid_strata")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < len(arms) or block_size % len(arms):
            raise BusinessError("区组长度无效", 422, "invalid_block_size")
        if len(str(seed)) < 8:
            raise BusinessError("随机种子至少 8 位", 422, "invalid_seed")

    def start_trial(self, user_id, trial_id):
        with self.store.connect() as conn:
            self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            if trial["status"] != "draft":
                raise BusinessError("只有草稿试验可以开始", 409, "invalid_status")
            self.store.start_trial_row(conn, trial_id, now())
            self._audit(conn, trial_id, user_id, "trial.start", {})
            return {"id": trial_id, "status": "running"}

    # ---- 随机与入组 ----
    def _stratum(self, conn, trial, factors, site_id):
        expected = json.loads(trial["strata_factors_json"])
        if set(factors) != set(expected):
            raise BusinessError(f"必须提供分层因素: {', '.join(expected)}", 422, "invalid_factors")
        normalized = {k: str(factors[k]).strip() for k in sorted(expected)}
        if any(not v for v in normalized.values()):
            raise BusinessError("分层因素值不能为空", 422, "invalid_factors")
        key = f"{site_id}|" + json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row = self.store.get_stratum(conn, trial["id"], key)
        if row:
            return row
        factors_json = json.dumps({"site_id": site_id, **normalized}, ensure_ascii=False, sort_keys=True)
        return conn.execute(
            "SELECT * FROM strata WHERE id=?",
            (self.store.insert_stratum(conn, trial["id"], key, factors_json, now()),),
        ).fetchone()

    def _next_allocation(self, conn, trial, stratum):
        for block_no in range(1, 101):
            if self.store.block_count(conn, stratum["id"], block_no) == 0:
                rng = random.Random(f"{trial['seed']}:{stratum['stratum_key']}:{block_no}")
                arms = json.loads(trial["arms_json"])
                plan = []
                blocks = len(arms) if trial["block_size"] > len(arms) else 1
                for _ in range(blocks * (trial["block_size"] // len(arms))):
                    plan.extend(arms)
                rng.shuffle(plan)
                start = self.store.max_sequence(conn, stratum["id"])
                self.store.insert_allocations(
                    conn,
                    [(trial["id"], stratum["id"], start + offset, block_no, arm) for offset, arm in enumerate(plan, 1)],
                )
            free = self.store.free_allocation(conn, stratum["id"])
            if free:
                return free
        raise BusinessError("随机分配表已耗尽，请由统计人员扩展方案", 409, "allocation_exhausted")

    def enroll(self, user_id, trial_id, external_id, factors):
        external_id = str(external_id).strip()
        if not external_id:
            raise BusinessError("外部受试者编号不能为空", 422, "invalid_external_id")
        with self.store.connect() as conn:
            actor = self._user(conn, user_id, {"site"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                trial = self._trial(conn, trial_id)
                if trial["status"] != "running":
                    raise BusinessError("试验尚未开始或已经停止", 409, "trial_not_running")
                if trial["enrollment_paused"]:
                    raise BusinessError("试验已暂停新入组（锁库批次待复核），紧急揭盲不受影响", 423, "enrollment_paused")
                existing = self.store.find_participant(conn, trial_id, external_id)
                if existing:
                    if existing["site_id"] != actor["site_id"]:
                        raise BusinessError("不能在当前中心查看其他中心的受试者", 403, "site_isolation")
                    conn.commit()
                    return self._blinded_participant(conn, existing, actor, allow_arm=False, idempotent=True)
                stratum = self._stratum(conn, trial, factors, actor["site_id"])
                allocation = self._next_allocation(conn, trial, stratum)
                allocation_code = hashlib.sha256(f"{trial_id}:{external_id}".encode()).hexdigest()[:12].upper()
                participant_id = self.store.insert_participant(
                    conn, trial_id, actor["site_id"], external_id, stratum["id"],
                    allocation["id"], allocation_code, user_id, now(),
                )
                self.store.mark_allocation_used(conn, allocation["id"], participant_id, now())
                self._audit(conn, trial_id, user_id, "participant.enroll", {"participant_id": participant_id, "external_id": external_id, "allocation_id": allocation["id"], "site_id": actor["site_id"]})
                participant = self.store.get_participant(conn, participant_id)
                return self._blinded_participant(conn, participant, actor, allow_arm=False, idempotent=False)
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "participants.trial_id, participants.external_id" in str(exc):
                    with self.store.connect() as retry:
                        row = self.store.find_participant(retry, trial_id, external_id)
                        if row and row["site_id"] == actor["site_id"]:
                            return self._blinded_participant(retry, row, actor, False, True)
                raise BusinessError("并发入组冲突，请重新提交", 409, "enrollment_conflict")
            except Exception:
                conn.rollback()
                raise

    def _blinded_participant(self, conn, participant, viewer, allow_arm=False, idempotent=False):
        result = {
            "id": participant["id"], "trial_id": participant["trial_id"],
            "external_id": participant["external_id"], "site_id": participant["site_id"],
            "allocation_code": participant["allocation_code"], "status": participant["status"],
            "created_at": participant["created_at"], "idempotent": idempotent,
        }
        if allow_arm:
            result["arm"] = self.store.allocation_arm(conn, participant["allocation_id"])
        return result

    def list_participants(self, user_id, trial_id):
        with self.store.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            self._trial(conn, trial_id)
            rows = self.store.list_participants(conn, trial_id, actor["site_id"] if actor["role"] == "site" else None)
            return [self._blinded_participant(conn, row, actor) for row in rows]

    def get_participant(self, user_id, participant_id):
        with self.store.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            row = self.store.get_participant(conn, participant_id)
            if not row:
                raise BusinessError("受试者不存在", 404, "not_found")
            if actor["role"] == "site" and row["site_id"] != actor["site_id"]:
                raise BusinessError("只能查看本中心受试者", 403, "site_isolation")
            approved = self.store.approved_unblinding_exists(conn, participant_id)
            return self._blinded_participant(conn, row, actor, allow_arm=approved)

    # ---- 紧急揭盲（锁库期间照常受理与审批）----
    def request_unblinding(self, user_id, participant_id, reason):
        if len(reason.strip()) < 8:
            raise BusinessError("揭盲原因至少 8 字", 422, "reason_required")
        with self.store.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator"})
            participant = self.store.get_participant(conn, participant_id)
            if not participant:
                raise BusinessError("受试者不存在", 404, "not_found")
            if actor["role"] == "site" and participant["site_id"] != actor["site_id"]:
                raise BusinessError("不能申请其他中心的揭盲", 403, "site_isolation")
            if self.store.open_unblinding_request(conn, participant_id):
                raise BusinessError("该受试者已有待审批的揭盲申请", 409, "request_exists")
            request_id = self.store.insert_unblinding_request(conn, participant_id, user_id, reason.strip(), now())
            self._audit(conn, participant["trial_id"], user_id, "unblinding.request", {"request_id": request_id, "participant_id": participant_id})
            return {"id": request_id, "status": "pending"}

    def approve_unblinding(self, user_id, request_id):
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"monitor", "coordinator"})
                request = self.store.get_unblinding_request(conn, request_id)
                if not request:
                    raise BusinessError("揭盲申请不存在", 404, "not_found")
                if request["status"] != "pending":
                    raise BusinessError("揭盲申请已经完成", 409, "already_decided")
                if request["first_approver"] is None:
                    self.store.set_first_approver(conn, request_id, user_id)
                    participant = self.store.get_participant(conn, request["participant_id"])
                    self._audit(conn, participant["trial_id"], user_id, "unblinding.approve.first", {"request_id": request_id})
                    return {"id": request_id, "status": "pending", "first_approver": user_id, "second_approval_required": True}
                if request["first_approver"] == user_id:
                    raise BusinessError("两次揭盲审批必须由不同人员完成", 409, "distinct_approver_required")
                self.store.approve_unblinding_request(conn, request_id, user_id, now())
                participant = self.store.get_participant(conn, request["participant_id"])
                arm = self.store.allocation_arm(conn, participant["allocation_id"])
                self._audit(conn, participant["trial_id"], user_id, "unblinding.approve.second", {"request_id": request_id, "participant_id": participant["id"]})
                return {"id": request_id, "status": "approved", "first_approver": request["first_approver"], "second_approver": user_id, "arm": arm}
            except Exception:
                conn.rollback()
                raise

    # ---- 中期锁库批次 ----
    def initiate_lock_batch(self, user_id, trial_id, reason=""):
        """协调员发起：冻结数据切点并暂停新入组。在途紧急揭盲不受影响。"""
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"coordinator"})
                trial = self._trial(conn, trial_id)
                if trial["status"] != "running":
                    raise BusinessError("只有进行中的试验可以发起中期锁库", 409, "invalid_status")
                existing = self.store.pending_lock_batch(conn, trial_id)
                if existing:
                    return self._lock_batch_dict(conn, existing)
                # 切点必须在本批次自身审计写入之前确定，保证审计快照不含锁库动作
                cutoff_participant_id = self.store.cutoff_participant_id(conn, trial_id)
                cutoff_audit_id = self.store.cutoff_audit_id(conn, trial_id)
                batch_no = self.store.next_lock_batch_no(conn, trial_id)
                batch_id = self.store.insert_lock_batch(
                    conn, trial_id, batch_no, user_id, now(), cutoff_participant_id, cutoff_audit_id
                )
                self.store.set_enrollment_paused(conn, trial_id, True)
                self._audit(
                    conn, trial_id, user_id, "lock_batch.initiate",
                    {"batch_id": batch_id, "batch_no": batch_no,
                     "cutoff_participant_id": cutoff_participant_id, "cutoff_audit_id": cutoff_audit_id,
                     "reason": reason.strip()},
                )
                row = self.store.get_lock_batch(conn, batch_id)
                return self._lock_batch_dict(conn, row)
            except Exception:
                conn.rollback()
                raise

    def review_lock_batch(self, user_id, batch_id, approve, note=""):
        """监查员复核：通过则按切点生成快照批次并恢复入组；不通过则不生成批次并恢复入组。"""
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"monitor"})
                batch = self.store.get_lock_batch(conn, batch_id)
                if not batch:
                    raise BusinessError("锁库批次不存在", 404, "not_found")
                if batch["status"] != "pending":
                    raise BusinessError("该锁库批次已完成复核", 409, "lock_batch_decided")
                note = (note or "").strip()
                if approve:
                    snapshot = self._build_snapshot(conn, batch)
                    self.store.complete_lock_batch(conn, batch_id, user_id, now(), json.dumps(snapshot, ensure_ascii=False))
                    self.store.set_enrollment_paused(conn, batch["trial_id"], False)
                    self._audit(
                        conn, batch["trial_id"], user_id, "lock_batch.complete",
                        {"batch_id": batch_id, "batch_no": batch["batch_no"],
                         "participant_count": snapshot["participant_count"],
                         "site_count": len(snapshot["sites"]), "note": note},
                    )
                else:
                    if len(note) < 4:
                        raise BusinessError("复核不通过必须填写至少 4 字原因", 422, "review_note_required")
                    self.store.reject_lock_batch(conn, batch_id, user_id, now(), note)
                    self.store.set_enrollment_paused(conn, batch["trial_id"], False)
                    self._audit(
                        conn, batch["trial_id"], user_id, "lock_batch.reject",
                        {"batch_id": batch_id, "batch_no": batch["batch_no"], "note": note},
                    )
                return self._lock_batch_dict(conn, self.store.get_lock_batch(conn, batch_id))
            except Exception:
                conn.rollback()
                raise

    def _build_snapshot(self, conn, batch):
        """按发起时冻结的切点重放人数、中心分布和审计记录。"""
        trial_id = batch["trial_id"]
        rows = self.store.participants_at_cutoff(conn, trial_id, batch["participant_cutoff_id"])
        by_site = {}
        for row in rows:
            by_site[row["site_id"]] = by_site.get(row["site_id"], 0) + 1
        audit_rows = self.store.audit_log_at_cutoff(conn, trial_id, batch["audit_cutoff_id"])
        return {
            "cutoff_at": batch["cutoff_at"],
            "participant_cutoff_id": batch["participant_cutoff_id"],
            "audit_cutoff_id": batch["audit_cutoff_id"],
            "participant_count": len(rows),
            "sites": [{"site_id": site_id, "count": count} for site_id, count in sorted(by_site.items())],
            "audit": [
                {"id": r["id"], "actor_id": r["actor_id"], "action": r["action"],
                 "detail": json.loads(r["detail"]), "created_at": r["created_at"]}
                for r in audit_rows
            ],
        }

    def _lock_batch_dict(self, conn, batch):
        snapshot = json.loads(batch["snapshot_json"]) if batch["snapshot_json"] else None
        return {
            "id": batch["id"], "trial_id": batch["trial_id"], "batch_no": batch["batch_no"],
            "status": batch["status"], "initiated_by": batch["initiated_by"], "reviewed_by": batch["reviewed_by"],
            "initiated_at": batch["initiated_at"], "reviewed_at": batch["reviewed_at"],
            "cutoff_at": batch["cutoff_at"],
            "participant_cutoff_id": batch["participant_cutoff_id"],
            "audit_cutoff_id": batch["audit_cutoff_id"],
            "review_note": batch["review_note"], "snapshot": snapshot,
        }

    def list_lock_batches(self, user_id, trial_id):
        with self.store.connect() as conn:
            self._user(conn, user_id, {"coordinator", "monitor"})
            self._trial(conn, trial_id)
            return [self._lock_batch_dict(conn, row) for row in self.store.list_lock_batches(conn, trial_id)]

    def get_lock_batch(self, user_id, batch_id):
        with self.store.connect() as conn:
            self._user(conn, user_id, {"coordinator", "monitor"})
            batch = self.store.get_lock_batch(conn, batch_id)
            if not batch:
                raise BusinessError("锁库批次不存在", 404, "not_found")
            return self._lock_batch_dict(conn, batch)

    def trial_summary(self, user_id, trial_id):
        with self.store.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            trial = self._trial(conn, trial_id)
            site_id = actor["site_id"] if actor["role"] == "site" else None
            rows = self.store.list_participants(conn, trial_id, site_id)
            total = len(rows)
            by_site = self.store.site_counts(conn, trial_id, site_id)
            audit = self.store.audit_log(conn, trial_id)
            pending = self.store.pending_lock_batch(conn, trial_id)
            return {
                "trial": {"id": trial["id"], "name": trial["name"], "protocol_version": trial["protocol_version"],
                          "status": trial["status"], "enrollment_paused": bool(trial["enrollment_paused"])},
                "participants_visible": total, "by_site": [dict(x) for x in by_site],
                "pending_lock_batch_id": pending["id"] if pending else None,
                "audit": [dict(x) | {"detail": json.loads(x["detail"])} for x in audit],
            }
