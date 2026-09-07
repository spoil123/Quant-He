# -*- coding: utf-8 -*-
"""主动通知模块测试（P1-5 核心交付：src/common/notify.py）。

契约：无配置/渠道失败 → 静默降级不抛异常；notify 返回真实推送结果；
notify_on_failure 只在有失败项时触发。用 monkeypatch 隔离 _cfg 缓存。
"""

from __future__ import annotations

import pandas as pd
import pytest

import src.common.notify as notify_mod
from src.common.notify import notify, notify_on_failure


@pytest.fixture
def fake_cfg(monkeypatch):
    """可控的 notify 配置（绕过模块级 _config_loaded 缓存）。"""

    def _set(cfg: dict):
        monkeypatch.setattr(notify_mod, "_cfg", lambda: cfg)
        return cfg

    return _set


class TestNotify:
    def test_disabled_returns_false_silently(self, fake_cfg, caplog):
        fake_cfg({"enabled": False})
        assert notify("主题", "内容", level="error") is False

    def test_webhook_success_returns_true(self, fake_cfg, monkeypatch):
        fake_cfg({"enabled": True,
                  "webhook": {"url": "https://hook.example.com/xx"},
                  "smtp": {}})
        calls = {}

        class _Resp:
            def raise_for_status(self):
                return None

        def _post(url, json=None, timeout=None):
            calls["url"] = url
            calls["json"] = json
            return _Resp()

        # _webhook 内是局部 import requests，patch 全局 requests.post 生效
        monkeypatch.setattr("requests.post", _post)
        assert notify("流水线失败", "detail", level="error") is True
        assert calls["url"] == "https://hook.example.com/xx"
        assert calls["json"]["text"]["content"].startswith("[ERROR] 流水线失败")

    def test_webhook_failure_returns_false_no_raise(self, fake_cfg, monkeypatch):
        fake_cfg({"enabled": True,
                  "webhook": {"url": "https://hook.example.com/xx"},
                  "smtp": {}})

        def _post(url, json=None, timeout=None):
            raise RuntimeError("网络不通")

        monkeypatch.setattr("requests.post", _post)
        assert notify("主题", "内容") is False   # 渠道失败 → 不抛异常，返回 False

    def test_channel_exception_caught(self, fake_cfg, monkeypatch):
        """渠道层抛异常也必须被吞掉，notify 返回 False 不炸。"""
        fake_cfg({"enabled": True, "webhook": {}, "smtp": {}})
        monkeypatch.setattr(notify_mod, "_webhook",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸了")))
        monkeypatch.setattr(notify_mod, "_smtp", lambda *a, **k: False)
        assert notify("主题", "内容") is False

    def test_no_channel_configured_returns_false(self, fake_cfg):
        fake_cfg({"enabled": True, "webhook": {}, "smtp": {}})
        assert notify("主题", "内容") is False    # 没配置任何渠道 → 没真推

    def test_bad_cfg_falls_back_log_only(self, fake_cfg):
        # get_config 抛异常场景由 _cfg 内部兜底，这里验证 notify 不炸
        fake_cfg({"enabled": True})
        assert notify("主题") is False


class TestNotifyOnFailure:
    def test_ok_result_skips_notify(self, fake_cfg, monkeypatch):
        fake_cfg({"enabled": True, "webhook": {}, "smtp": {}})
        called = []
        monkeypatch.setattr(notify_mod, "notify",
                            lambda *a, **k: called.append(a))
        notify_on_failure({"ok": True, "steps": []})
        assert called == []

    def test_failure_triggers_notify(self, fake_cfg, monkeypatch):
        fake_cfg({"enabled": True, "webhook": {}, "smtp": {}})
        called = []
        monkeypatch.setattr(notify_mod, "notify",
                            lambda *a, **k: called.append(a))
        notify_on_failure({
            "ok": False,
            "steps": [
                {"step": "update_daily", "ok": True},
                {"step": "walk_forward", "ok": False, "error": "boom"},
            ],
        }, step="run_daily")
        assert len(called) == 1
        subj, body, *_ = called[0]
        assert "run_daily" in subj          # step 参数进标题
        assert "1/2" in subj
        assert "walk_forward" in body       # 失败步骤名进正文
        assert "boom" in body

    def test_disabled_still_archives_on_failure(self, fake_cfg, monkeypatch, tmp_path):
        """disabled 也调用 notify()（其内部落本地归档），失败不静默消失。"""
        fake_cfg({"enabled": False})
        called = []
        monkeypatch.setattr(notify_mod, "notify",
                            lambda *a, **k: called.append(a))
        notify_on_failure({"ok": False,
                           "steps": [{"step": "x", "ok": False,
                                      "error": "boom"}]})
        assert len(called) == 1
        subj, body, *_ = called[0]
        assert "流水线失败" in subj and "boom" in body


class TestChannelStatus:
    """渠道配置状态判定 —— 区分「主动关闭」和「以为开着其实没配」"""

    def test_nothing_configured(self, fake_cfg):
        fake_cfg({"enabled": True, "webhook": {}, "smtp": {}})
        st = notify_mod.channel_status()
        assert not st["webhook"]["configured"]
        assert not st["smtp"]["configured"]

    def test_webhook_ready(self, fake_cfg):
        fake_cfg({"enabled": True,
                  "webhook": {"url": "https://x/y"}, "smtp": {}})
        st = notify_mod.channel_status()
        assert st["webhook"]["configured"]
        assert not st["smtp"]["configured"]

    def test_smtp_needs_all_fields(self, fake_cfg):
        """缺授权码或收件人都算没配好 —— 否则登录时才炸，为时已晚。"""
        fake_cfg({"enabled": True, "webhook": {},
                  "smtp": {"host": "smtp.qq.com", "user": "a@qq.com",
                           "password": "", "to": ["b@x.com"]}})
        assert not notify_mod.channel_status()["smtp"]["configured"]

        fake_cfg({"enabled": True, "webhook": {},
                  "smtp": {"host": "smtp.qq.com", "user": "a@qq.com",
                           "password": "pwd", "to": []}})
        assert not notify_mod.channel_status()["smtp"]["configured"]

    def test_smtp_complete(self, fake_cfg):
        fake_cfg({"enabled": True, "webhook": {},
                  "smtp": {"host": "smtp.qq.com", "user": "a@qq.com",
                           "password": "pwd", "to": ["b@x.com"]}})
        assert notify_mod.channel_status()["smtp"]["configured"]


class TestWebhookPayload:
    """各家机器人 payload 结构不同 —— 选错格式就是"配了也发不出去"。"""

    def test_wecom_format(self):
        p = notify_mod._webhook_payload("主题", "内容", "error", "wecom")
        assert p["msgtype"] == "text"
        assert p["text"]["content"].startswith("[ERROR]")

    def test_feishu_format(self):
        p = notify_mod._webhook_payload("主题", "内容", "info", "feishu")
        assert p["msg_type"] == "text"          # 飞书是下划线 + content.text
        assert p["content"]["text"].startswith("[INFO]")
        assert "msgtype" not in p

    def test_dingtalk_has_at_field(self):
        p = notify_mod._webhook_payload("主题", "内容", "warning", "dingtalk")
        assert p["msgtype"] == "text"
        assert "at" in p

    def test_unknown_format_falls_back_to_wecom(self):
        p = notify_mod._webhook_payload("主题", "内容", "info", "不存在的平台")
        assert p["msgtype"] == "text"


class TestSelfTest:
    """自检必须能把「配了但发不出去」这件事暴露出来"""

    def test_disabled_reports_problem(self, fake_cfg):
        fake_cfg({"enabled": False, "webhook": {}, "smtp": {}})
        r = notify_mod.self_test()
        assert r["enabled"] is False
        assert r["ready"] == []
        assert any("不会有任何推送" in p for p in r["problems"])

    def test_enabled_without_channel_is_flagged(self, fake_cfg):
        """最危险状态：enabled=true 但零渠道。"""
        fake_cfg({"enabled": True, "webhook": {}, "smtp": {}})
        r = notify_mod.self_test()
        assert r["enabled"] is True
        assert r["ready"] == []
        assert any("最危险" in p for p in r["problems"])

    def test_ready_config_no_problems(self, fake_cfg):
        fake_cfg({"enabled": True,
                  "webhook": {"url": "https://x/y"}, "smtp": {}})
        r = notify_mod.self_test()
        assert r["ready"] == ["webhook"]
        assert r["problems"] == []

    def test_send_verifies_actually_delivered(self, fake_cfg, monkeypatch):
        """--send 要真的推一条；推不出去必须算问题，不能自欺欺人。"""
        fake_cfg({"enabled": True,
                  "webhook": {"url": "https://x/y"}, "smtp": {}})
        monkeypatch.setattr(notify_mod, "notify", lambda *a, **k: False)
        r = notify_mod.self_test(send=True)
        assert r["sent"] is False
        assert any("实测推送失败" in p for p in r["problems"])

    def test_send_success(self, fake_cfg, monkeypatch):
        fake_cfg({"enabled": True,
                  "webhook": {"url": "https://x/y"}, "smtp": {}})
        monkeypatch.setattr(notify_mod, "notify", lambda *a, **k: True)
        r = notify_mod.self_test(send=True)
        assert r["sent"] is True
        assert r["problems"] == []

    def test_send_skipped_when_not_ready(self, fake_cfg):
        """配置不全时跳过实测，别拿无效凭据去试。"""
        fake_cfg({"enabled": True, "webhook": {}, "smtp": {}})
        r = notify_mod.self_test(send=True)
        assert r["sent"] is None
        assert any("跳过实测推送" in p for p in r["problems"])


class TestNotifyArchive:
    """归档契约：warning/error 无论渠道是否启用都落盘 logs/alerts/。"""

    def test_disabled_warning_still_archived(self, fake_cfg, monkeypatch, tmp_path):
        fake_cfg({"enabled": False})
        monkeypatch.setattr(notify_mod, "_ALERT_DIR", tmp_path)
        assert notify("数据源失败", "detail", level="warning") is False
        files = list(tmp_path.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "数据源失败" in content and "未送达" in content

    def test_debug_level_not_archived(self, fake_cfg, monkeypatch, tmp_path):
        fake_cfg({"enabled": False})
        monkeypatch.setattr(notify_mod, "_ALERT_DIR", tmp_path)
        notify("低级别", "x", level="debug")
        assert list(tmp_path.glob("*.md")) == []

    def test_enabled_no_channel_archived(self, fake_cfg, monkeypatch, tmp_path):
        fake_cfg({"enabled": True, "webhook": {}, "smtp": {}})
        monkeypatch.setattr(notify_mod, "_ALERT_DIR", tmp_path)
        assert notify("有配置无渠道", "x", level="error") is False
        files = list(tmp_path.glob("*.md"))
        assert len(files) == 1
        assert "有配置无渠道" in files[0].read_text(encoding="utf-8")
