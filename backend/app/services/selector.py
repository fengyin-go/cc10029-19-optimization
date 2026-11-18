"""抽取规则业务规则：状态流转、字段校验、筛选口径与站点选择器绑定台账。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import BINDING_TABLE, SNAPSHOT_TABLE, store

MODULE = "selector"
LIST_FIELDS = ["规则编号", "归属站点", "列表选择器", "详情选择器", "字段选择器", "分页参数", "等待条件", "字符编码"]
REQUIRED_FIELDS = ["规则编号", "归属站点", "列表选择器"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

BINDING_ACTIVE = "生效"
BINDING_INACTIVE = "已失效"
STATUS_PRIORITY = {"已启用": 3, "待校验": 2, "草稿": 1, "已停用": 0}


class BindingScopeError(Exception):
    """绑定范围越界：绑定记录只能引用当前快照站点主键。"""


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _priority(row: dict[str, Any]) -> int:
    return STATUS_PRIORITY.get(str(row.get("status") or ""), 0)


class SelectorService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"选择器规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于抽取规则可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"选择器规则已{action}"

    # ---------------------------------------------------------- 绑定台账读取
    def _snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        for row in store.rows(SNAPSHOT_TABLE):
            if int(row.get("id", 0)) == int(snapshot_id):
                return row
        return None

    def current_snapshot(self) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows(SNAPSHOT_TABLE) if row.get("状态") == "生效"),
            None,
        )

    def list_bindings(
        self,
        *,
        snapshot_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """抽取规则读取目标站点筛选数据快照生成的绑定台账。"""
        rows = list(store.rows(BINDING_TABLE))
        if snapshot_id is not None:
            rows = [row for row in rows if int(row.get("快照主键") or 0) == int(snapshot_id)]
        if status:
            rows = [row for row in rows if row.get("台账状态") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def pick_rule_for_site(self, site: dict[str, Any]) -> dict[str, Any] | None:
        """给一个站点挑选唯一一条抽取规则：启用优先，同级取最新主键。"""
        code = str(site.get("站点编号") or "").strip()
        name = str(site.get("站点名称") or "").strip()
        candidates = [
            row for row in store.rows(MODULE)
            if str(row.get("归属站点") or "").strip() in {code, name} - {""}
        ]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda row: (_priority(row), int(row.get("id", 0))),
            reverse=True,
        )[0]

    def materialize_bindings(
        self,
        snapshot: dict[str, Any],
        *,
        rule_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """在调用方的事务里按快照站点写绑定记录。

        绑定范围以快照站点主键为上限：只要有一条站点不在快照里就整体抛错，
        由调用方的事务把快照和绑定记录一起回滚。
        """
        allowed = {int(key) for key in snapshot.get("站点主键") or []}
        override = None
        if rule_id is not None:
            override = store.find(MODULE, rule_id)
            if override is None:
                raise BindingScopeError(f"选择器规则 {rule_id} 不存在或已归档")

        written: list[dict[str, Any]] = []
        for site in snapshot.get("站点集合") or []:
            key = int(site.get("id", 0))
            if key not in allowed:
                raise BindingScopeError(f"绑定范围超出快照站点：{key}")
            rule = override or self.pick_rule_for_site(site)
            binding_id = store.next_sequence("binding")
            binding = {
                "id": binding_id,
                "绑定编号": f"BIND-{binding_id:04d}",
                "快照主键": snapshot.get("id"),
                "快照编号": snapshot.get("快照编号"),
                "快照版本": snapshot.get("版本号"),
                "数据版本": snapshot.get("数据版本"),
                "站点主键": key,
                "站点编号": site.get("站点编号"),
                "站点名称": site.get("站点名称"),
                "规则主键": int(rule.get("id", 0)) if rule else None,
                "规则编号": rule.get("规则编号") if rule else None,
                "归属站点": rule.get("归属站点") if rule else None,
                "快照状态": snapshot.get("状态"),
                "台账状态": BINDING_ACTIVE if snapshot.get("状态") == "生效" else BINDING_INACTIVE,
                "绑定范围": "快照站点",
                "创建时间": now_text(),
            }
            store.rows(BINDING_TABLE).append(binding)
            written.append(binding)
        return written

    def create_binding(
        self,
        snapshot_id: int,
        *,
        site_keys: list[int] | None = None,
        rule_id: int | None = None,
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """按快照站点补一条绑定记录；绑定范围不能超出快照里的站点主键。"""
        snapshot = self._snapshot(snapshot_id)
        if snapshot is None:
            return None, f"目标站点筛选数据快照 {snapshot_id} 不存在"
        allowed = {int(key) for key in snapshot.get("站点主键") or []}
        requested = [int(key) for key in (site_keys or [])]
        if not requested:
            return None, "绑定站点不能为空"
        overflow = [key for key in requested if key not in allowed]
        if overflow:
            return None, f"绑定范围超出快照站点：{'、'.join(str(key) for key in overflow)}"

        site_index = {
            int(site.get("id", 0)): site for site in snapshot.get("站点集合") or []
        }
        override = None
        if rule_id is not None:
            override = store.find(MODULE, rule_id)
            if override is None:
                return None, f"选择器规则 {rule_id} 不存在或已归档"

        written: list[dict[str, Any]] = []
        try:
            with store.transaction():
                for key in requested:
                    site = site_index[key]
                    rule = override or self.pick_rule_for_site(site)
                    binding_id = store.next_sequence("binding")
                    binding = {
                        "id": binding_id,
                        "绑定编号": f"BIND-{binding_id:04d}",
                        "快照主键": snapshot.get("id"),
                        "快照编号": snapshot.get("快照编号"),
                        "快照版本": snapshot.get("版本号"),
                        "数据版本": snapshot.get("数据版本"),
                        "站点主键": key,
                        "站点编号": site.get("站点编号"),
                        "站点名称": site.get("站点名称"),
                        "规则主键": int(rule.get("id", 0)) if rule else None,
                        "规则编号": rule.get("规则编号") if rule else None,
                        "归属站点": rule.get("归属站点") if rule else None,
                        "快照状态": snapshot.get("状态"),
                        "台账状态": BINDING_ACTIVE if snapshot.get("状态") == "生效" else BINDING_INACTIVE,
                        "绑定范围": "快照站点",
                        "创建时间": now_text(),
                    }
                    store.rows(BINDING_TABLE).append(binding)
                    written.append(binding)
        except Exception as exc:
            return None, f"站点选择器绑定记录写入失败，已回滚：{exc}"
        return written, f"已生成 {len(written)} 条站点选择器绑定记录"

