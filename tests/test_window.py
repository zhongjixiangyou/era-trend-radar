#!/usr/bin/env python3
"""窗口契约的最小单测（docs/window-rolling.md §6）。

只跑标准库：python3 -m unittest discover -s tests
不联网、不写 content/，临时文件一律落 tmp。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import _core as C  # noqa: E402

CN = timezone(timedelta(hours=8))
ROLLING = {"window": {"mode": "rolling", "days": 180, "timezone": "Asia/Shanghai"}}
FIXED = {
    "window": {
        "mode": "fixed",
        "frozen": {"start": "2026-05-13", "end": "2026-11-13", "frozenAt": "2026-11-13"},
        "timezone": "Asia/Shanghai",
    }
}
AS_OF = "2026-11-14T12:00:00+08:00"


class WindowBoundaryTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ERA_AS_OF", None)
        os.environ.pop("ERA_MAX_TS", None)
        self.win = C.resolve_window(ROLLING, as_of=AS_OF)

    def tearDown(self):
        C.set_window(None)

    def test_shape(self):
        self.assertEqual(self.win.mode, "rolling")
        self.assertEqual(self.win.days, 180)
        self.assertEqual(self.win.from_str, "2026-05-19")
        self.assertEqual(self.win.to_str, "2026-11-14")
        self.assertEqual(C.fmt_ts(self.win.upper), "2026-11-14T12:00:00+08:00")
        self.assertEqual(C.fmt_ts(self.win.from_ts), "2026-05-19T00:00:00+08:00")
        self.assertEqual(self.win.as_of_source, "cli")
        self.assertEqual(self.win.as_of_from, "as-of")
        self.assertEqual(
            self.win.as_meta(),
            {"from": "2026-05-19", "to": "2026-11-14", "mode": "rolling",
             "asOf": "2026-11-14T12:00:00+08:00", "asOfFrom": "as-of", "days": 180},
        )
        self.assertEqual(self.win.as_report()["asOfFrom"], "as-of")

    def test_lower_bound(self):
        """① from 当天 00:00:00 通过；from-1 秒算旧记录（warning，不是 error）。"""
        self.assertEqual(self.win.judge("2026-05-19T00:00:00+08:00"), C.JUDGE_OK)
        self.assertEqual(self.win.judge("2026-05-19T00:00:00Z"), C.JUDGE_OK)  # Z 换算成 08:00+08
        self.assertEqual(self.win.judge("2026-05-18T23:59:59+08:00"), C.JUDGE_OLD)
        self.assertTrue(self.win.covers("2026-05-19"))
        self.assertEqual(self.win.judge("2026-05-18"), C.JUDGE_OLD)

    def test_upper_bound(self):
        """② asOf 本身通过；asOf+1 秒越上界。"""
        self.assertEqual(self.win.judge(AS_OF), C.JUDGE_OK)
        self.assertEqual(self.win.judge("2026-11-14T12:00:01+08:00"), C.JUDGE_FUTURE)
        self.assertEqual(self.win.judge("2026-11-15"), C.JUDGE_FUTURE)
        self.assertEqual(self.win.judge("2026-11-14"), C.JUDGE_OK)  # date==to 合法

    def test_midnight_shift(self):
        """③ 同一天内的 asOf 只改上界、不改日历日；跨一天下界整体平移。"""
        early = C.resolve_window(ROLLING, as_of="2026-11-14T00:00:01+08:00")
        late = C.resolve_window(ROLLING, as_of="2026-11-14T23:59:59+08:00")
        self.assertEqual(early.to_str, late.to_str)
        self.assertEqual(early.from_str, late.from_str)
        nxt = C.resolve_window(ROLLING, as_of="2026-11-15T00:00:01+08:00")
        self.assertEqual(nxt.to_str, "2026-11-15")
        self.assertEqual(nxt.from_str, "2026-05-20")

    def test_same_window_for_all_stages(self):
        """④ 一轮里 collect/validate 一个 asOf：解析两次得到同样的边界。"""
        a = C.resolve_window(ROLLING, as_of=AS_OF)
        b = C.resolve_window(ROLLING, as_of=AS_OF)
        self.assertEqual((a.from_str, a.to_str, a.upper), (b.from_str, b.to_str, b.upper))
        self.assertEqual(a.judge("2026-05-18T23:59:59+08:00"), b.judge("2026-05-18T23:59:59+08:00"))

    def test_stack(self):
        """时区不是 +08 时按配置时区换日（口径与 JS 的 toLocaleDateString 一致）。"""
        cfg = {"window": {"mode": "rolling", "days": 180, "timezone": "UTC"}}
        win = C.resolve_window(cfg, as_of="2026-11-14T23:30:00+08:00")  # UTC 仍是 11-14 15:30
        self.assertEqual(win.to_str, "2026-11-14")
        win2 = C.resolve_window(cfg, as_of="2026-11-14T08:30:00+08:00")  # UTC = 11-14 00:30
        self.assertEqual(win2.to_str, "2026-11-14")
        win3 = C.resolve_window(cfg, as_of="2026-11-14T04:30:00+08:00")  # UTC = 11-13 20:30
        self.assertEqual(win3.to_str, "2026-11-13")


class FixedAndOverrideTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ERA_AS_OF", None)
        os.environ.pop("ERA_MAX_TS", None)

    def tearDown(self):
        C.set_window(None)

    def test_fixed_reproduces_frozen(self):
        """⑤ fixed 模式与 2026-11-13 冻结完全一致，meta 带 frozenAt。"""
        win = C.resolve_window(FIXED, as_of="2026-12-31T10:00:00+08:00")
        self.assertEqual((win.from_str, win.to_str), ("2026-05-13", "2026-11-13"))
        self.assertEqual(win.days, None)
        self.assertEqual(C.fmt_ts(win.upper), "2026-11-13T23:59:59+08:00")
        self.assertEqual(win.judge("2026-11-13T23:59:59+08:00"), C.JUDGE_OK)
        self.assertEqual(win.judge("2026-11-14T00:00:00+08:00"), C.JUDGE_FUTURE)
        self.assertEqual(win.as_meta()["frozenAt"], "2026-11-13")
        self.assertNotIn("frozenAt", C.resolve_window(ROLLING, as_of=AS_OF).as_meta())

    def test_fixed_legacy_flat_config(self):
        """老配置只有 window.start/end（没有 frozen 块）也要能读。"""
        legacy = {"window": {"mode": "fixed", "start": "2026-05-13", "end": "2026-11-13"}}
        win = C.resolve_window(legacy)
        self.assertEqual((win.from_str, win.to_str), ("2026-05-13", "2026-11-13"))
        self.assertEqual(win.frozen_at, "2026-11-13")

    def test_max_ts_overrides_config(self):
        """⑥ --max-ts 压过配置；fixed 下不移动日期、只压上界。"""
        win = C.resolve_window(FIXED, max_ts="2026-11-10T00:00:00+08:00")
        self.assertEqual((win.from_str, win.to_str), ("2026-05-13", "2026-11-13"))
        self.assertEqual(C.fmt_ts(win.upper), "2026-11-10T00:00:00+08:00")
        self.assertEqual(win.as_of_source, "cli")

    def test_as_of_overrides_env_max_ts(self):
        """⑥ --as-of 压过 ERA_MAX_TS。"""
        os.environ["ERA_MAX_TS"] = "2026-10-01T00:00:00+08:00"
        win = C.resolve_window(ROLLING, as_of=AS_OF)
        self.assertEqual(win.to_str, "2026-11-14")
        self.assertEqual(win.as_of_source, "cli")
        env_only = C.resolve_window(ROLLING)
        self.assertEqual(env_only.to_str, "2026-10-01")
        self.assertEqual(env_only.as_of_source, "env")

    def test_as_of_from_channels(self):
        """asOfFrom 区分通道，asOfSource 枚举保持 cli/env/arg/clock 不变。"""
        cli_max = C.resolve_window(ROLLING, max_ts="2026-11-10T00:00:00+08:00")
        self.assertEqual((cli_max.as_of_source, cli_max.as_of_from), ("cli", "max-ts"))

        os.environ["ERA_MAX_TS"] = "2026-11-09T00:00:00+08:00"
        env_max = C.resolve_window(ROLLING)
        self.assertEqual((env_max.as_of_source, env_max.as_of_from), ("env", "max-ts"))
        os.environ.pop("ERA_MAX_TS", None)

        os.environ["ERA_AS_OF"] = "2026-11-14T12:00:00+08:00"
        env_as_of = C.resolve_window(ROLLING)
        self.assertEqual((env_as_of.as_of_source, env_as_of.as_of_from), ("env", "as-of"))
        # CI 多导的 ERA_WINDOW_AS_OF_FROM 优先级更高（同一轮口径以 CI 为准）
        os.environ["ERA_WINDOW_AS_OF_FROM"] = "max-ts"
        pinned = C.resolve_window(ROLLING)
        self.assertEqual((pinned.as_of_source, pinned.as_of_from), ("env", "max-ts"))
        os.environ.pop("ERA_WINDOW_AS_OF_FROM", None)
        os.environ.pop("ERA_AS_OF", None)

        arg = C.resolve_window(ROLLING, now=C.parse_ts("2026-11-14T12:00:00+08:00"))
        self.assertEqual((arg.as_of_source, arg.as_of_from), ("arg", "arg"))

        clock = C.resolve_window(ROLLING)
        self.assertEqual((clock.as_of_source, clock.as_of_from), ("clock", "clock"))

    def test_apply_window_moves_max_dt(self):
        win = C.apply_window(ROLLING, as_of=AS_OF)
        self.assertEqual(C.max_dt(), win.upper)
        self.assertEqual(C.active_window(), win)
        C.set_max_ts("2026-01-01T00:00:00+08:00")  # 老入口：只设上限，窗口态清空
        self.assertIsNone(C.active_window())
        self.assertEqual(C.fmt_ts(C.max_dt()), "2026-01-01T00:00:00+08:00")


class ValidateWindowTest(unittest.TestCase):
    """真跑校验器：下界是 WINDOW_OUT warning，上界是 TS_FUTURE error。"""

    @classmethod
    def setUpClass(cls):
        sample = sorted((ROOT / "content" / "events").glob("*.json"))[0]
        cls.base = json.loads(sample.read_text(encoding="utf-8"))

    def setUp(self):
        os.environ.pop("ERA_AS_OF", None)
        os.environ.pop("ERA_MAX_TS", None)
        C.apply_window(ROLLING, as_of=AS_OF)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        C.set_window(None)

    def _validate(self, **patch) -> C.Ctx:
        rec = dict(self.base)
        rec.update(patch)
        path = self.dir / f"{rec['id']}.json"
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        ctx, _files = C.validate_paths([str(path)], strict_refs="skip", history="off")
        return ctx

    def _codes(self, ctx: C.Ctx) -> list[str]:
        return [f.code for f in ctx.findings]

    def test_old_record_is_warning_only(self):
        ctx = self._validate(occurredAt="2026-05-18T23:59:59+08:00")
        self.assertIn("WINDOW_OUT", self._codes(ctx))
        self.assertEqual([f for f in ctx.findings if f.code == "TS_FUTURE"], [])
        self.assertTrue(ctx.ok, f"旧记录不能把整库判负: {self._codes(ctx)}")

    def test_future_record_is_error(self):
        ctx = self._validate(occurredAt="2026-11-14T12:00:01+08:00")
        self.assertIn("TS_FUTURE", self._codes(ctx))
        self.assertFalse(ctx.ok)

    def test_in_window_record_clean(self):
        ctx = self._validate(occurredAt=AS_OF)
        self.assertNotIn("WINDOW_OUT", self._codes(ctx))
        self.assertTrue(ctx.ok)


class ExportMetaTest(unittest.TestCase):
    def test_rolling_meta_has_no_frozen(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import export_astro as X  # noqa: PLC0415

        win = C.resolve_window(ROLLING, as_of=AS_OF)
        events, _trends, _fc, _counts = X.build_payload(
            [], {}, "2026-11-14T12:00:00+08:00", [], [], window_meta=win.as_meta()
        )
        self.assertEqual(events["meta"]["window"]["from"], "2026-05-19")
        self.assertEqual(events["meta"]["window"]["to"], "2026-11-14")
        self.assertEqual(events["meta"]["window"]["mode"], "rolling")
        self.assertEqual(events["meta"]["window"]["days"], 180)
        self.assertNotIn("frozenAt", events["meta"]["window"])

    def test_fixed_meta_keeps_frozen(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import export_astro as X  # noqa: PLC0415

        win = C.resolve_window(FIXED)
        events, _t, _f, _c = X.build_payload(
            [], {}, "2026-11-14T12:00:00+08:00", [], [], window_meta=win.as_meta()
        )
        self.assertEqual(events["meta"]["window"]["from"], "2026-05-13")
        self.assertEqual(events["meta"]["window"]["frozenAt"], "2026-11-13")
        self.assertIsNone(events["meta"]["window"]["days"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
