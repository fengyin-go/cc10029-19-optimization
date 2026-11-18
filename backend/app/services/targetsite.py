"""采集目标业务规则：状态流转、字段校验、筛选快照与站点选择器绑定台账。

这里的核心对象是「目标站点筛选数据快照」：

* 筛选条件（入口URL、请求方法、字符编码等多个字段叠加）先归一化再算条件指纹；
* 快照落库时同时保存条件指纹和站点主键，翻页只读快照里那份站点集合，
  同一首页、第二页永远共享同一份数据，不会因为底表变化而左右漂移；
* 同一站点编号同时命中启用/停用等多条记录时，按「已启用 > 待校验 > 草稿 > 已停用」
  的优先级选定唯一主键，冲突信息留在快照里，读端只看到一份口径明确的快照；
* 快照与抽取规则的站点选择器绑定记录在同一事务里写入，快照失败时绑定一起回滚；
* 回退旧快照或切换到新版本都会推进当前数据版本，旧快照仍然可以按编号回看。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.services.selector import BindingScopeError, SelectorService
from app.store import BINDING_TABLE, SNAPSHOT_TABLE, store

MODULE = "targetsite"
LIST_FIELDS = ["站点编号", "站点名称", "入口URL", "请求方法", "请求头模板", "字符编码", "重试上限", "抓取间隔"]
REQUIRED_FIELDS = ["站点编号", "站点名称", "入口URL"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

SNAPSHOT_ACTIVE = "生效"
SNAPSHOT_HISTORY = "历史"
SNAPSHOT_ROLLED_BACK = "已回退"
SNAPSHOT_STATUSES = [SNAPSHOT_ACTIVE, SNAPSHOT_HISTORY, SNAPSHOT_ROLLED_BACK]

BINDING_ACTIVE = "生效"
BINDING_INACTIVE = "已失效"

# 站点编号冲突时的口径优先级：启用记录优先于停用记录，同级取最新主键。
STATUS_PRIORITY = {"已启用": 3, "待校验": 2, "草稿": 1, "已停用": 0}


class SnapshotError(Exception):
    """快照写入前置校验失败；由路由层转成 ok=False 的可读回执。"""


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_filters(
    values: dict[str, Any] | None = None,
    *,
    keyword: str | None = None,
    status: str | None = None,
) -> dict[str, str]:
    """把条件归一化成可比较、可生成指纹的形态：去空白、大小写折叠、忽略空值。"""
    normalized: dict[str, str] = {}
    for field in LIST_FIELDS:
        raw = (values or {}).get(field)
        text = "" if raw is None else str(raw).strip()
        if text:
            normalized[field] = text.casefold()
    if keyword and keyword.strip():
        normalized["keyword"] = keyword.strip().casefold()
    if status and status.strip():
        normalized["status"] = status.strip()
    return normalized


def condition_fingerprint(normalized: dict[str, str]) -> str:
    """条件指纹：同一组条件无论字段顺序、大小写都得到同一个指纹。"""
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def site_key(row: dict[str, Any]) -> str:
    """站点业务主键：优先站点编号；缺失时退化为站点名称或行主键。"""
    code = str(row.get("站点编号") or "").strip()
    if code:
        return code
    name = str(row.get("站点名称") or "").strip()
    if name:
        return name
    return f"id:{row.get('id')}"


def _status_priority(row: dict[str, Any]) -> int:
    return STATUS_PRIORITY.get(str(row.get("status") or ""), 0)


class TargetsiteService:
    # ------------------------------------------------------------------ 列表
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        snapshot_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """常规列表沿用既有口径；带快照时只翻快照里的站点集合。"""
        if snapshot_id is not None:
            snapshot = self.get_snapshot(snapshot_id)
            if snapshot is not None:
                sites = list(snapshot.get("站点集合") or [])
                total = len(sites)
                start = (page - 1) * size
                return sites[start:start + size], total
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("站点编号", "")).casefold()]
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
        # 底表站点发生变化，当前数据版本推进，已有快照仍按自身版本回看。
        store.data_version += 1
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"目标站点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集目标可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.data_version += 1
        return entry, f"目标站点已{action}"

    # -------------------------------------------------------------- 快照匹配
    def _match_rows(
        self,
        filters: dict[str, str],
        keyword: str | None,
        status: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按条件取出底表记录，并按站点编号收敛成每个站点唯一一条。"""
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("站点编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(site_key(row), []).append(row)

        chosen: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for key in sorted(grouped):
            candidates = grouped[key]
            # 先按状态优先级，再按主键倒序，保证冲突时读端口径唯一且可复现。
            ordered = sorted(
                candidates,
                key=lambda row: (_status_priority(row), int(row.get("id", 0))),
                reverse=True,
            )
            winner = ordered[0]
            chosen.append(winner)
            if len(candidates) > 1:
                conflicts.append({
                    "站点编号": key,
                    "选用主键": int(winner.get("id", 0)),
                    "选用状态": winner.get("status"),
                    "舍弃主键": [int(row.get("id", 0)) for row in ordered[1:]],
                    "口径": "同一站点编号命中多条记录，按已启用优先、同级取最新主键收敛为一份快照",
                })
        return chosen, conflicts

    def _snapshot_site(self, row: dict[str, Any]) -> dict[str, Any]:
        # 站点明细沿用列表接口既有字段，快照只作为额外口径随记录一起保存。
        site = {"id": int(row.get("id", 0))}
        for field in LIST_FIELDS:
            site[field] = row.get(field)
        site["status"] = row.get("status")
        return site

    # -------------------------------------------------------------- 快照写入
    def state(self) -> dict[str, Any]:
        current = next(
            (row for row in store.rows(SNAPSHOT_TABLE) if row.get("状态") == SNAPSHOT_ACTIVE),
            None,
        )
        return {
            "data_version": store.data_version,
            "current_snapshot_id": current.get("id") if current else None,
            "current_snapshot_no": current.get("快照编号") if current else None,
            "snapshot_count": len(store.rows(SNAPSHOT_TABLE)),
            "binding_count": len(store.rows(BINDING_TABLE)),
        }

    def _validate_payload(
        self,
        values: dict[str, Any],
        keyword: str | None,
        status: str | None,
    ) -> None:
        unknown = [
            str(key) for key, value in (values or {}).items()
            if key not in LIST_FIELDS and value not in (None, "")
        ]
        if unknown:
            raise SnapshotError(f"筛选条件字段不在采集目标范围内：{'、'.join(sorted(unknown))}")
        if status and status.strip() not in STATUS_ORDER:
            raise SnapshotError(f"状态「{status}」不在允许的筛选状态里")

    def create_snapshot(
        self,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """按筛选条件生成目标站点筛选数据快照，并在同一事务里写绑定台账。

        同一请求（相同幂等键 + 相同条件指纹）重复提交时只落一条快照，
        直接返回成功回执；条件指纹相同但换了请求标识则生成新版本。
        """
        values = dict(payload.get("values") or {})
        keyword = payload.get("keyword")
        status = payload.get("status")
        request_id = request_id or payload.get("request_id")
        values = {key: value for key, value in values.items()}
        keyword = str(keyword).strip() if keyword else None
        status = str(status).strip() if status else None

        self._validate_payload(values, keyword, status)
        normalized = normalize_filters(values, keyword=keyword, status=status)
        fingerprint = condition_fingerprint(normalized)
        # 幂等键：优先用客户端请求标识；没有请求标识时退化为条件指纹。
        # 同一请求（同一标识、同一条件指纹）重复提交只落一条快照，按成功回执返回；
        # 想在同一组条件上生成新版本，换一个请求标识即可。
        idempotency_key = (request_id or "").strip() or f"fingerprint:{fingerprint}"

        snapshots = store.rows(SNAPSHOT_TABLE)
        for existing in snapshots:
            if existing.get("幂等键") != idempotency_key:
                continue
            if existing.get("条件指纹") != fingerprint:
                raise SnapshotError("请求标识已用于其他筛选条件，请更换请求标识后重试")
            return {
                "ok": True,
                "message": "目标站点筛选数据快照已存在，按成功回执返回",
                "deduplicated": True,
                "snapshot": self._snapshot_view(existing),
                "bindings": self._bindings_for(existing.get("id")),
                "state": self.state(),
            }

        try:
            with store.transaction():
                chosen, conflicts = self._match_rows(values, keyword, status)
                sites = [self._snapshot_site(row) for row in chosen]
                site_keys = [site["id"] for site in sites]

                # 同一指纹下的版本号递增；新一轮快照成为当前版本，旧版本留作回看。
                version = max(
                    (int(row.get("版本号", 0)) for row in snapshots
                     if row.get("条件指纹") == fingerprint),
                    default=0,
                ) + 1
                for row in snapshots:
                    if row.get("状态") == SNAPSHOT_ACTIVE:
                        row["状态"] = SNAPSHOT_HISTORY
                # 旧版本快照对应的绑定台账同步失效，台账始终反映当前生效版本。
                for binding in store.rows(BINDING_TABLE):
                    binding["快照状态"] = next(
                        (row.get("状态") for row in snapshots
                         if int(row.get("id", 0)) == int(binding.get("快照主键") or 0)),
                        binding.get("快照状态"),
                    )
                    if binding["快照状态"] != SNAPSHOT_ACTIVE:
                        binding["台账状态"] = BINDING_INACTIVE

                snapshot_id = store.next_sequence("snapshot")
                store.data_version += 1
                snapshot = {
                    "id": snapshot_id,
                    "快照编号": f"SNAP-{snapshot_id:04d}",
                    "条件指纹": fingerprint,
                    "筛选条件": normalized,
                    "站点主键": site_keys,
                    "站点集合": sites,
                    "站点总数": len(sites),
                    "版本号": version,
                    "数据版本": store.data_version,
                    "状态": SNAPSHOT_ACTIVE,
                    "幂等键": idempotency_key,
                    "请求标识": (request_id or "").strip() or None,
                    "冲突站点": conflicts,
                    "创建时间": now_text(),
                }
                snapshots.append(snapshot)
                bindings = self._write_bindings(snapshot)

            return {
                "ok": True,
                "message": f"目标站点筛选数据快照已生成，绑定 {len(bindings)} 条站点选择器记录",
                "deduplicated": False,
                "snapshot": self._snapshot_view(snapshot),
                "bindings": bindings,
                "state": self.state(),
            }
        except SnapshotError:
            raise
        except Exception as exc:  # 事务块内任何异常都会把快照与绑定一起回滚
            raise SnapshotError(f"目标站点筛选数据快照写入失败，已回滚：{exc}") from exc

    # ------------------------------------------------------------ 选择器绑定
    def _write_bindings(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """读取快照让抽取规则生成绑定记录；绑定范围由抽取规则侧校验。"""
        try:
            return SelectorService().materialize_bindings(snapshot)
        except BindingScopeError as exc:
            # 绑定范围越界即视为写入失败，由当前事务把快照一起回滚。
            raise SnapshotError(str(exc)) from exc

    def _bindings_for(self, snapshot_id: int | None) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(BINDING_TABLE)
            if int(row.get("快照主键") or 0) == int(snapshot_id or 0)
        ]

    # ------------------------------------------------------------ 快照读取
    def _snapshot_view(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": snapshot.get("id"),
            "快照编号": snapshot.get("快照编号"),
            "条件指纹": snapshot.get("条件指纹"),
            "筛选条件": dict(snapshot.get("筛选条件") or {}),
            "站点主键": list(snapshot.get("站点主键") or []),
            "站点总数": snapshot.get("站点总数", 0),
            "版本号": snapshot.get("版本号"),
            "数据版本": snapshot.get("数据版本"),
            "状态": snapshot.get("状态"),
            "冲突站点": list(snapshot.get("冲突站点") or []),
            "创建时间": snapshot.get("创建时间"),
        }

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        for row in store.rows(SNAPSHOT_TABLE):
            if int(row.get("id", 0)) == int(snapshot_id):
                return row
        return None

    def list_snapshots(
        self,
        *,
        fingerprint: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(SNAPSHOT_TABLE))
        if fingerprint:
            rows = [row for row in rows if row.get("条件指纹") == fingerprint]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows.sort(key=lambda row: (int(row.get("版本号", 0)), int(row.get("id", 0))), reverse=True)
        total = len(rows)
        start = (page - 1) * size
        return [self._snapshot_view(row) for row in rows[start:start + size]], total

    def snapshot_sites(
        self, snapshot_id: int, *, page: int = 1, size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
        """翻页只切快照内部的站点集合：每页合计恒等于快照站点总数。"""
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            return [], 0, None
        sites = list(snapshot.get("站点集合") or [])
        total = len(sites)
        start = (page - 1) * size
        return sites[start:start + size], total, snapshot

    def list_bindings(
        self,
        *,
        snapshot_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(BINDING_TABLE))
        if snapshot_id is not None:
            rows = [row for row in rows if int(row.get("快照主键") or 0) == int(snapshot_id)]
        if status:
            rows = [row for row in rows if row.get("台账状态") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    # ------------------------------------------------------------ 版本切换
    def switch_snapshot(self, snapshot_id: int, *, mode: str = "switch") -> dict[str, Any]:
        """切换当前快照版本；回退与切换都改变当前数据版本，旧快照仍可回看。"""
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            return {"ok": False, "message": f"目标站点筛选数据快照 {snapshot_id} 不存在"}
        if mode == "rollback":
            current = next(
                (row for row in store.rows(SNAPSHOT_TABLE) if row.get("状态") == SNAPSHOT_ACTIVE),
                None,
            )
            # 回退只能指向当前版本之前的历史快照，不能拿最新版本冒充回退。
            if current is not None and int(snapshot.get("id", 0)) >= int(current.get("id", 0)):
                return {"ok": False, "message": "回退目标必须是更早版本的目标站点筛选数据快照"}

        with store.transaction():
            for row in store.rows(SNAPSHOT_TABLE):
                if row.get("状态") == SNAPSHOT_ACTIVE:
                    row["状态"] = SNAPSHOT_ROLLED_BACK if mode == "rollback" else SNAPSHOT_HISTORY
            snapshot["状态"] = SNAPSHOT_ACTIVE
            snapshot["数据版本"] = store.data_version + 1
            for binding in store.rows(BINDING_TABLE):
                binding["快照状态"] = next(
                    (row.get("状态") for row in store.rows(SNAPSHOT_TABLE)
                     if int(row.get("id", 0)) == int(binding.get("快照主键") or 0)),
                    binding.get("快照状态"),
                )
                binding["台账状态"] = (
                    BINDING_ACTIVE
                    if int(binding.get("快照主键") or 0) == snapshot_id
                    else BINDING_INACTIVE
                )
                if int(binding.get("快照主键") or 0) == snapshot_id:
                    # 当前版本下的绑定台账跟着快照的数据版本一起走。
                    binding["数据版本"] = snapshot["数据版本"]
            store.data_version += 1

        action = "回退" if mode == "rollback" else "切换"
        return {
            "ok": True,
            "message": f"目标站点筛选数据快照已{action}到 {snapshot.get('快照编号')}",
            "snapshot": self._snapshot_view(snapshot),
            "bindings": self._bindings_for(snapshot_id),
            "state": self.state(),
        }
