"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

除了各业务模块的主表，这里还维护两张跨业务面的辅助表：
- ``targetsite_snapshot``：目标站点筛选数据快照，保存条件指纹与站点主键；
- ``selector_binding``：站点选择器绑定记录台账，状态随快照版本一起变化。

``transaction()`` 给“快照 + 绑定记录”这类多表写入提供最小粒度的事务语义：
事务块内任何一步抛错，表内容、版本号与自增序列都会回到进入事务前的状态。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

SNAPSHOT_TABLE = "targetsite_snapshot"
BINDING_TABLE = "selector_binding"


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 辅助表不参与运营概览的模块统计，避免把快照台账当成独立业务模块。
        self._tables.setdefault(SNAPSHOT_TABLE, [])
        self._tables.setdefault(BINDING_TABLE, [])
        self._aux_tables = {SNAPSHOT_TABLE, BINDING_TABLE}
        # 当前数据版本：新建快照或回退快照都会推进它，用版本号区分快照口径。
        self.data_version: int = 1
        self._sequences: dict[str, int] = {}

    def reset(self) -> None:
        """恢复到种子数据：主要给回归测试和本地复现用。"""
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._tables.setdefault(SNAPSHOT_TABLE, [])
        self._tables.setdefault(BINDING_TABLE, [])
        self.data_version = 1
        self._sequences = {}

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in self._aux_tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_sequence(self, name: str) -> int:
        """按业务前缀生成连续编号，事务回滚时序列也一起回退。"""
        current = self._sequences.get(name, 0) + 1
        self._sequences[name] = current
        return current

    def peek_sequence(self, name: str) -> int:
        return self._sequences.get(name, 0)

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """把多表写入包成一个事务；块内抛错时整块回滚。"""
        tables_backup = copy.deepcopy(self._tables)
        version_backup = self.data_version
        sequence_backup = dict(self._sequences)
        try:
            yield self
        except Exception:
            self._tables = tables_backup
            self.data_version = version_backup
            self._sequences = sequence_backup
            raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
