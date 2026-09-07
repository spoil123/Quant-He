# -*- coding: utf-8 -*-
"""
主动通知（P1-5）：把「退出码 + 日志」升级成「主动推送」。

支持的渠道（config/notify.yaml 配置，全部可选）：
    webhook   通用 HTTP 回调（企业微信机器人 / Slack / 自建服务，POST JSON）
    smtp      邮件（smtplib，无需第三方依赖）

设计原则：
    - 无配置 / 渠道不可用 → 静默降级为 logger 记录，绝不让告警流程本身抛异常
    - 只发「要人处理」的消息：monitor 有告警、流水线有失败
    - 幂等可重试：外部失败不影响主流程

用法：
    from src.common.notify import notify
    notify("数据流水线失败", "update_daily 退出码 1", level="error")
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from src.common.config import get_config, load_dotenv_once
from src.common.logger import logger

load_dotenv_once()

# 缓存一份配置，避免每次推送都重读 yaml
_notify_cfg: Optional[dict] = None
_config_loaded = False


def _cfg() -> dict:
    global _notify_cfg, _config_loaded
    if not _config_loaded:
        try:
            _notify_cfg = get_config("notify")
        except Exception:                                   # noqa: BLE001
            _notify_cfg = {}
        _config_loaded = True
    return _notify_cfg or {}


def _enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def _webhook_payload(subject: str, body: str, level: str, fmt: str) -> dict:
    """按平台生成 payload —— 各家机器人格式不同，不适配就是「配了也发不出去」。

    wecom/dingtalk/generic 用 {"msgtype":"text","text":{"content":...}}
    feishu              用 {"msg_type":"text","content":{"text":...}}
    """
    text = f"[{level.upper()}] {subject}\n{body}"
    # 钉钉「自定义关键词」安全设置通常填"告警"：若消息不含该词会被静默拦截
    # （HTTP 200 + errcode 310000）。在 content 头部固定带上"告警"规避。
    if fmt == "dingtalk":
        text = f"[告警] {text}"
    if fmt == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    # wecom（企业微信机器人）/ dingtalk / generic 同构
    payload = {"msgtype": "text", "text": {"content": text}}
    if fmt == "dingtalk":
        payload["at"] = {"isAtAll": False}
    return payload


def _webhook(subject: str, body: str, level: str) -> bool:
    """推送 webhook。返回是否成功（无 URL / 推送异常 / 业务错误 → False）。

    H: 不只按 HTTP 状态判成功 —— 钉钉/企业微信在关键词不符、签名错误时
    仍回 HTTP 200 + JSON errcode≠0（钉钉 310000 等），只看 status_code
    会误报"推送成功"。解析响应体 errcode==0 才算成功；无法解析的
    2xx（自建服务无 JSON）按成功处理。
    """
    wh = _cfg().get("webhook") or {}
    url = wh.get("url", "")
    if not url:
        return False
    fmt = str(wh.get("format", "wecom")).lower()
    import requests

    payload = _webhook_payload(subject, body, level, fmt)
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        # 业务级校验：钉钉/企微/飞书都在 body 里回 errcode/StatusCode
        try:
            data = r.json()
        except Exception:                                    # noqa: BLE001
            data = None
        if isinstance(data, dict):
            # 钉钉: {"errcode":0}  企微: {"errcode":0}  飞书: {"code":0}
            err = data.get("errcode", data.get("code"))
            if err is not None and err != 0:
                logger.error(f"webhook 业务拒绝: errcode={err} "
                             f"msg={data.get('errmsg') or data.get('msg')} —— "
                             f"常见原因：安全关键词不含'告警'/签名错/机器人被移除")
                return False
        logger.info(f"webhook 推送成功: {subject}")
        return True
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"webhook 推送失败: {type(e).__name__}: {e}")
        return False


def _smtp(subject: str, body: str) -> bool:
    """发送邮件。返回是否成功（配置缺失 / 发送异常 → False，不抛）。"""
    smtp = _cfg().get("smtp") or {}
    host, port = smtp.get("host", ""), int(smtp.get("port", 465))
    user, pwd = smtp.get("user", ""), smtp.get("password", "")
    to = smtp.get("to", [])
    if not (host and user and pwd and to):
        return False
    if isinstance(to, str):
        to = [x.strip() for x in to.split(",") if x.strip()]

    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = ", ".join(to)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(user, pwd)
                s.sendmail(user, to, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls()
                s.login(user, pwd)
                s.sendmail(user, to, msg.as_string())
        logger.info(f"邮件推送成功: {subject}")
        return True
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"邮件推送失败: {type(e).__name__}: {e}")
        return False


_ALERT_DIR = Path("logs") / "alerts"


def _archive_alert(subject: str, body: str, level: str, delivered: bool) -> None:
    """本地告警归档：无论渠道是否启用/送达，warning/error 都落盘一份。

    解决「只有日志、没人被惊动」的最后一环 —— 即使 enabled=false 或渠道
    全空，告警至少会写进 logs/alerts/YYYY-MM-DD.md，下次启动/巡检时
    DataMonitor 会读它并提示「昨天有 N 条未处理告警」。
    """
    if level not in ("warning", "error"):
        return
    try:
        _ALERT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = "已送达" if delivered else "未送达(仅归档)"
        line = (f"- [{stamp}] **{level.upper()}** {subject} "
                f"({state})\n  {body}\n")
        with open(_ALERT_DIR / f"{datetime.now():%Y-%m-%d}.md",
                  "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:                                  # noqa: BLE001
        logger.debug(f"告警归档失败: {type(e).__name__}: {e}")


def channel_status() -> Dict[str, Dict[str, object]]:
    """逐个渠道报告配置完整性（只检查配置，不实际发送）。

    用来区分三种状态，它们的危险程度完全不同：
      · enabled=false            —— 主动关闭，消息进日志，是明确的选择
      · enabled=true + 无渠道     —— 最危险：看起来开了告警，实际一条都发不出
      · enabled=true + 渠道完整   —— 正常
    """
    cfg = _cfg()
    wh = cfg.get("webhook") or {}
    smtp = cfg.get("smtp") or {}
    to = smtp.get("to", [])
    return {
        "webhook": {
            "configured": bool(wh.get("url")),
            "detail": f"url={((wh.get('url') or '')[:36] + '...') if wh.get('url') else '未配置'}",
        },
        "smtp": {
            "configured": bool(smtp.get("host") and smtp.get("user")
                               and smtp.get("password") and to),
            "detail": (f"host={smtp.get('host') or '未配置'} "
                       f"user={smtp.get('user') or '未配置'} "
                       f"to={to or '未配置'}"),
        },
    }


def notify(subject: str, body: str = "", level: str = "warning") -> bool:
    """推送一条消息。返回是否成功推送（无配置/降级返回 False，不抛异常）。

    level: debug/info/warning/error —— 日志级别 + webhook 消息前缀。
    warning/error 级一律先落本地归档（logs/alerts/），再尝试外发 ——
    即使渠道没配好，告警也不会只进 debug 日志而丢失。
    """
    if not _enabled():
        # 主动关闭或未配置：归档 + 显式日志（区别于 info 级），不留死角
        _archive_alert(subject, body, level, delivered=False)
        logger.log({"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 30),
                   f"[notify 未启用,已归档] {subject} {body}")
        return False

    # enabled=true 却没有任何可用渠道 = 告警全部丢失且无人知晓，必须显式报错
    chans = channel_status()
    if not any(bool(v["configured"]) for v in chans.values()):
        _archive_alert(subject, body, level, delivered=False)
        logger.error(
            "notify 已启用(enabled=true)但没有任何渠道配置完整 —— "
            "该告警不会送达任何地方。请补全 config/notify.yaml 的 webhook.url "
            "或 smtp.*，然后运行 `python -m src.common.notify --send` 自检"
        )
        return False

    logger.log({"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 30),
               f"[notify:{level}] {subject} {body}")

    sent = False
    try:
        sent = _webhook(subject, body, level) or sent
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"webhook 异常: {type(e).__name__}: {e}")
    try:
        sent = _smtp(subject, body) or sent
    except Exception as e:                                  # noqa: BLE001
        logger.error(f"smtp 异常: {type(e).__name__}: {e}")

    if not sent and level in ("error", "warning"):
        # 到了这里说明"想发但没发出去" —— 告警的告警，至少落一条 ERROR 日志
        logger.error(f"告警未送达任何渠道: {subject}")
    _archive_alert(subject, body, level, delivered=sent)
    return sent


def notify_on_failure(result: dict, step: str = "") -> None:
    """流水线结果里只要有失败项就推送一条汇总告警。

    不在入口判 enabled —— 直接调 notify()，由它统一处理降级：
    未启用/无渠道时告警会落本地归档（logs/alerts/），下次巡检可见，
    而不是静默消失。旧实现 enabled=false 直接 return，失败既不推送
    也不归档，等于没做告警。
    """
    if result.get("ok"):
        return
    steps = result.get("steps") or []
    failed = [r for r in steps if not r.get("ok")]
    if not failed:
        return
    lines = [f"  ❌ {r.get('step', '?')}: {(r.get('error') or '')[:200]}" for r in failed]
    notify(f"流水线失败 {step or ''}（{len(failed)}/{len(steps)} 环节）",
           "\n".join(lines), level="error")


def self_test(send: bool = False) -> Dict[str, object]:
    """告警链路自检 —— 防止「告警系统自己坏了，而没人收到告警」。

    检查三件事：
      1. enabled 开关是否打开
      2. 是否至少有一个渠道配置完整
      3. （--send）真的推一条测试消息，验证凭据有效、网络可达

    用法：
        python -m src.common.notify          # 只检查配置
        python -m src.common.notify --send   # 检查 + 实测推送
    """
    enabled = _enabled()
    chans = channel_status()
    ready = [k for k, v in chans.items() if v["configured"]]
    problems: list[str] = []

    if not enabled:
        problems.append(
            "notify.enabled=false：所有告警静默降级为日志，线上出事不会有任何推送")
    elif not ready:
        problems.append(
            "enabled=true 但没有任何渠道配置完整：这是最危险的状态 —— "
            "看起来开着告警，实际一条都发不出去")

    sent: Optional[bool] = None
    if send:
        if not (enabled and ready):
            problems.append("配置不完整，跳过实测推送")
        else:
            sent = notify("[自检] 告警通道测试",
                          "这是一条测试消息，用于验证告警通道可用。"
                          "收到说明配置正确；若后续故障却没收到同类消息，"
                          "说明通道已失效，请重新自检。",
                          level="info")
            if not sent:
                problems.append("实测推送失败：凭据或网络有问题，见上方 ERROR 日志")

    print("\n" + "=" * 64)
    print("  告警通道自检")
    print("=" * 64)
    print(f"  enabled        {enabled}")
    for name, info in chans.items():
        mark = "✔" if info["configured"] else "✘"
        print(f"  {mark} {name:12s} {info['detail']}")
    if sent is not None:
        print(f"  实测推送        {'成功' if sent else '失败'}")
    print("-" * 64)
    if problems:
        print("  存在问题：")
        for p in problems:
            print(f"    ⚠ {p}")
        print("\n  配置方法见 config/notify.yaml 模板；配完再跑一次 "
              "`python -m src.common.notify --send` 确认能收到")
    else:
        print("  ✔ 告警链路可用")
    print("=" * 64 + "\n")

    return {"enabled": enabled, "channels": chans, "ready": ready,
            "sent": sent, "problems": problems}


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="告警通道自检")
    ap.add_argument("--send", action="store_true",
                    help="实际推送一条测试消息，验证凭据与网络")
    args = ap.parse_args()
    result = self_test(send=args.send)
    sys.exit(0 if not result["problems"] else 1)
