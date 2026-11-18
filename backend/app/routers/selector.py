"""抽取规则接口：维护选择器规则，覆盖提交校验、启用配置、停用配置等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.selector import SelectorService

router = APIRouter(prefix="/api/selector", tags=["抽取规则"])

service = SelectorService()

LIST_FIELDS = ["规则编号", "归属站点", "列表选择器", "详情选择器", "字段选择器", "分页参数", "等待条件", "字符编码"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤抽取规则列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/bindings", response_model=PageResult[dict])
def list_bindings(
    snapshot_id: int | None = Query(default=None, description="按快照编号读取绑定台账"),
    status: str | None = Query(default=None, description="生效、已失效"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """读取由目标站点筛选数据快照生成的站点选择器绑定台账。"""
    items, total = service.list_bindings(
        snapshot_id=snapshot_id, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/bindings", response_model=ActionResult)
def create_binding(payload: dict[str, object]) -> ActionResult:
    """按快照站点补写绑定记录；绑定范围超出快照站点时整条拒绝。"""
    snapshot_id = payload.get("snapshot_id")
    if not isinstance(snapshot_id, int):
        return ActionResult(ok=False, message="缺少目标站点筛选数据快照编号")
    site_keys = payload.get("site_keys")
    if site_keys is not None and not isinstance(site_keys, list):
        return ActionResult(ok=False, message="绑定站点必须是站点主键数组")
    rule_id = payload.get("rule_id")
    bindings, message = service.create_binding(
        snapshot_id,
        site_keys=[int(key) for key in site_keys] if site_keys else None,
        rule_id=int(rule_id) if isinstance(rule_id, int) else None,
    )
    if bindings is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"bindings": bindings})


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="selector.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条选择器规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"选择器规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条选择器规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="选择器规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条选择器规则执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
