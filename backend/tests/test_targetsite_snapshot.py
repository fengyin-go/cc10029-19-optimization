"""目标站点筛选数据快照回归测试（标准库 unittest，无需额外依赖）。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from app.services.selector import SelectorService
from app.services.targetsite import SnapshotError, TargetsiteService
from app.store import BINDING_TABLE, SNAPSHOT_TABLE, store


class SnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = TargetsiteService()
        self.selector = SelectorService()

    def test_snapshot_saves_fingerprint_and_site_keys(self) -> None:
        result = self.service.create_snapshot(
            {"values": {"入口URL": "采集目标样例"}, "request_id": "case-1"}
        )
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["站点主键"], [1, 2, 3])
        self.assertEqual(snapshot["站点总数"], 3)
        self.assertEqual(len(snapshot["条件指纹"]), 64)

        # 条件顺序、大小写不同应命中同一个条件指纹。
        other = self.service.create_snapshot(
            {"values": {"入口URL": "  采集目标样例 "}, "request_id": "case-2"}
        )
        self.assertEqual(other["snapshot"]["条件指纹"], snapshot["条件指纹"])

    def test_pagination_shares_one_site_set(self) -> None:
        result = self.service.create_snapshot(
            {"values": {}, "request_id": "paging-1"}
        )
        snapshot_id = result["snapshot"]["id"]

        seen: list[int] = []
        for page in (1, 2, 3):
            sites, total, _ = self.service.snapshot_sites(snapshot_id, page=page, size=2)
            self.assertEqual(total, 3)
            seen.extend(site["id"] for site in sites)
        # 翻页结果覆盖全量站点，且没有跨页重复。
        self.assertEqual(sorted(seen), [1, 2, 3])
        self.assertEqual(len(seen), len(set(seen)))

        # 底表新增站点不会改变既有快照的翻页结果。
        self.service.create_entry({
            "站点编号": "TARG-0009", "站点名称": "后加入站点", "入口URL": "后加入",
        })
        sites, total, _ = self.service.snapshot_sites(snapshot_id, page=1, size=20)
        self.assertEqual(total, 3)
        self.assertEqual(sorted(site["id"] for site in sites), [1, 2, 3])

    def test_duplicate_site_number_resolves_to_one_snapshot_row(self) -> None:
        # TARG-0003 原始记录是已启用；再登记一条同编号记录并把它停用，制造版本冲突。
        row, _ = self.service.create_entry({
            "站点编号": "TARG-0003", "站点名称": "停用重复站点", "入口URL": "采集目标样例",
        })
        self.service.run_action(int(row["id"]), "停用配置")
        result = self.service.create_snapshot(
            {"values": {"入口URL": "采集目标样例"}, "request_id": "conflict-1"}
        )
        snapshot = result["snapshot"]
        # 同一站点编号只保留一条：已启用记录优先于停用记录。
        self.assertEqual(snapshot["站点主键"], [1, 2, 3])
        self.assertEqual(len(snapshot["站点主键"]), len(set(snapshot["站点主键"])))
        conflict = snapshot["冲突站点"][0]
        self.assertEqual(conflict["站点编号"], "TARG-0003")
        self.assertEqual(conflict["选用主键"], 3)
        self.assertEqual(conflict["舍弃主键"], [4])

    def test_bindings_are_limited_to_snapshot_sites(self) -> None:
        result = self.service.create_snapshot(
            {"values": {}, "request_id": "bind-1"}
        )
        snapshot_id = result["snapshot"]["id"]
        bindings = result["bindings"]
        self.assertEqual([item["站点主键"] for item in bindings], [1, 2, 3])
        self.assertTrue(all(item["台账状态"] == "生效" for item in bindings))

        written, message = self.selector.create_binding(snapshot_id, site_keys=[99])
        self.assertIsNone(written)
        self.assertIn("超出快照站点", message)

        written, _ = self.selector.create_binding(snapshot_id, site_keys=[2])
        self.assertIsNotNone(written)
        self.assertEqual(written[0]["站点主键"], 2)

    def test_snapshot_and_bindings_roll_back_together(self) -> None:
        before = (
            len(store.rows(SNAPSHOT_TABLE)),
            len(store.rows(BINDING_TABLE)),
            store.data_version,
        )
        original = SelectorService.materialize_bindings

        def boom(self, snapshot, *, rule_id=None):  # noqa: ANN001 - 测试用故障注入
            raise RuntimeError("模拟抽取规则读取失败")

        SelectorService.materialize_bindings = boom
        try:
            with self.assertRaises(SnapshotError):
                self.service.create_snapshot({"values": {}, "request_id": "txn-1"})
        finally:
            SelectorService.materialize_bindings = original

        after = (
            len(store.rows(SNAPSHOT_TABLE)),
            len(store.rows(BINDING_TABLE)),
            store.data_version,
        )
        self.assertEqual(before, after, "快照失败时绑定记录与版本号必须一起回滚")

    def test_same_request_is_recorded_once(self) -> None:
        first = self.service.create_snapshot({"values": {}, "request_id": "same-1"})
        second = self.service.create_snapshot({"values": {}, "request_id": "same-1"})
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["snapshot"]["id"], second["snapshot"]["id"])
        self.assertEqual(len(store.rows(SNAPSHOT_TABLE)), 1)
        self.assertTrue(second["ok"])

    def test_switch_and_rollback_keep_old_snapshots_readable(self) -> None:
        first = self.service.create_snapshot({"values": {}, "request_id": "ver-1"})
        self.service.create_entry({
            "站点编号": "TARG-0008", "站点名称": "新站点", "入口URL": "新站点",
        })
        second = self.service.create_snapshot({"values": {}, "request_id": "ver-2"})
        first_id = first["snapshot"]["id"]
        second_id = second["snapshot"]["id"]
        # 新快照生效后，旧快照自动转为历史版本，但仍然可回看。
        self.assertEqual(self.service.get_snapshot(first_id)["状态"], "历史")
        self.assertEqual(second["snapshot"]["站点总数"], 4)

        before_version = store.data_version
        switched = self.service.switch_snapshot(first_id, mode="rollback")
        self.assertTrue(switched["ok"])
        self.assertGreater(store.data_version, before_version)
        self.assertEqual(switched["snapshot"]["站点总数"], 3)

        old = self.service.get_snapshot(first_id)
        self.assertIsNotNone(old)
        self.assertEqual(len(old["站点集合"]), 3)
        for field in ("站点编号", "站点名称", "入口URL", "请求方法", "字符编码", "status"):
            self.assertIn(field, old["站点集合"][0])

        # 回退目标必须是更早版本；当前生效的最新版本不能被当作回退目标。
        self.service.switch_snapshot(second_id, mode="switch")
        refused = self.service.switch_snapshot(second_id, mode="rollback")
        self.assertFalse(refused["ok"])
        self.assertIn("更早版本", refused["message"])

    def test_snapshot_state_flows_to_selector_binding_ledger(self) -> None:
        first = self.service.create_snapshot({"values": {}, "request_id": "ledger-1"})
        second = self.service.create_snapshot({"values": {}, "request_id": "ledger-2"})
        self.service.switch_snapshot(first["snapshot"]["id"], mode="rollback")

        first_id = first["snapshot"]["id"]
        second_id = second["snapshot"]["id"]
        rows, total = self.selector.list_bindings(snapshot_id=first_id)
        self.assertEqual(total, 3)
        self.assertTrue(all(row["台账状态"] == "生效" for row in rows))
        rows, total = self.selector.list_bindings(snapshot_id=second_id)
        self.assertEqual(total, 3)
        self.assertTrue(all(row["台账状态"] == "已失效" for row in rows))

    def test_cross_page_no_duplicate_after_filters_leave_repeats(self) -> None:
        # 多条件叠加后底表只剩部分记录；快照仍按站点编号去重，跨页不会重复。
        self.service.create_entry({
            "站点编号": "TARG-0001", "站点名称": "同编号第二条", "入口URL": "采集目标样例",
        })
        result = self.service.create_snapshot(
            {"values": {"入口URL": "采集目标样例", "请求方法": "采集目标样例"},
             "request_id": "dup-page-1"}
        )
        snapshot_id = result["snapshot"]["id"]
        keys = []
        for page in (1, 2, 3):
            sites, _, _ = self.service.snapshot_sites(snapshot_id, page=page, size=1)
            keys.extend(site["id"] for site in sites)
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(set(keys)), 3)
        self.assertEqual(result["snapshot"]["站点总数"], 3)

    def test_unknown_filter_field_is_rejected(self) -> None:
        with self.assertRaises(SnapshotError):
            self.service.create_snapshot(
                {"values": {"不存在的字段": "x"}, "request_id": "bad-1"}
            )


if __name__ == "__main__":
    unittest.main()
