"""采集目标接口：维护目标站点，并提供目标站点筛选数据快照的生成、翻页与版本切换。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.targetsite import SnapshotError, TargetsiteService

router = APIRouter(prefix="/api/targetsite", tags=["采集目标"])

service = TargetsiteService()

LIST_FIELDS = ["站点编号", "站点名称", "入口URL", "请求方法", "请求头模板", "字符编码", "重试上限", "抓取间隔"]
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
    keyword: str | None = Query(default=None, description="按站点编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    snapshot_id: int | None = Query(default=None, description="按目标站点筛选数据快照翻页"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、站点编号关键字与状态过滤；带 snapshot_id 时只翻快照内的站点集合。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status,
        page=page, size=size, snapshot_id=snapshot_id)
    return PageResult(items=items, total=total, page=page, size=size)


# ---------------------------------------------------------------- 快照接口
# 需要写在 /{entry_id} 之前，避免 "snapshots" 被当作整数主键解析。
@router.get("/snapshots/state", response_model=dict)
def snapshot_state() -> dict:
    """当前生效快照与数据版本。"""
    return service.state()


@router.get("/snapshots", response_model=PageResult[dict])
def list_snapshots(
    fingerprint: str | None = Query(default=None, description="按条件指纹筛选"),
    status: str | None = Query(default=None, description="生效、历史、已回退"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """列出历史快照；旧快照在条件变化后仍可按编号回看。"""
    items, total = service.list_snapshots(
        fingerprint=fingerprint, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/snapshots", response_model=ActionResult)
def create_snapshot(payload: dict[str, Any]) -> ActionResult:
    """筛选并快照采集目标：同时保存条件指纹与站点主键，并在同一事务里写绑定台账。"""
    try:
        result = service.create_snapshot(payload, request_id=payload.get("request_id"))
    except SnapshotError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=result["ok"],
        message=result["message"],
        entry={
            "snapshot": result["snapshot"],
            "bindings": result["bindings"],
            "state": result["state"],
            "deduplicated": result["deduplicated"],
        },
    )


@router.get("/snapshots/{snapshot_id}", response_model=dict)
def get_snapshot(snapshot_id: int, page: int = Query(default=1, ge=1),
                 size: int = Query(default=20, ge=1, le=200)) -> dict:
    """读取快照明细与当前页站点；站点集合严格来自快照，不重新查询底表。"""
    sites, total, snapshot = service.snapshot_sites(snapshot_id, page=page, size=size)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"目标站点筛选数据快照 {snapshot_id} 不存在")
    view = service._snapshot_view(snapshot)
    view["items"] = sites
    view["total"] = total
    view["page"] = page
    view["size"] = size
    view["bindings"] = service._bindings_for(snapshot_id)
    return view


@router.get("/snapshots/{snapshot_id}/sites", response_model=PageResult[dict])
def snapshot_sites(
    snapshot_id: int,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """快照内站点翻页：每页都从同一份快照集合切分，跨页不会漏站点。"""
    sites, total, snapshot = service.snapshot_sites(snapshot_id, page=page, size=size)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"目标站点筛选数据快照 {snapshot_id} 不存在")
    return PageResult(items=sites, total=total, page=page, size=size)


@router.post("/snapshots/{snapshot_id}/switch", response_model=ActionResult)
def switch_snapshot(snapshot_id: int) -> ActionResult:
    """把指定快照切换为当前版本；旧快照继续保留，可按编号回看。"""
    result = service.switch_snapshot(snapshot_id, mode="switch")
    if not result.get("ok"):
        return ActionResult(ok=False, message=str(result.get("message")))
    return ActionResult(ok=True, message=str(result.get("message")), entry=result)


@router.post("/snapshots/{snapshot_id}/rollback", response_model=ActionResult)
def rollback_snapshot(snapshot_id: int) -> ActionResult:
    """回退到更早的快照版本；回退同样推进当前数据版本。"""
    result = service.switch_snapshot(snapshot_id, mode="rollback")
    if not result.get("ok"):
        return ActionResult(ok=False, message=str(result.get("message")))
    return ActionResult(ok=True, message=str(result.get("message")), entry=result)


@router.get("/bindings", response_model=PageResult[dict])
def list_bindings(
    snapshot_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """站点选择器绑定记录台账，状态随快照版本一起记录。"""
    items, total = service.list_bindings(
        snapshot_id=snapshot_id, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


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
    output.write("﻿")
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
        headers={"Content-Disposition": 'attachment; filename="targetsite.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条目标站点明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"目标站点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条目标站点，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="目标站点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条目标站点执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
