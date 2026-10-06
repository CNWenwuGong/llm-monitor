"""告警规则引擎：阈值判定 + 去重写库。

规则集合（均可在前端配置开关与阈值）
----------------------------------
* ``tps_low``        当前 TPS 低于阈值
* ``ttft_high``      首 Token 延迟高于阈值
* ``e2e_high``       端到端延迟高于阈值
* ``vram_high``      显存占用百分比高于阈值
* ``gpu_temp_high``  GPU 温度高于阈值
* ``error_rate``     压测错误率高于阈值
* ``gpu_idle``       GPU 长时间空闲（利用率低于阈值）

去重策略：同一规则在 ``DEDUP_WINDOW`` 秒内只记录一次，避免告警刷屏。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from . import collector, config, db, tester

log = logging.getLogger("llm-monitor.alerts")

DEDUP_WINDOW = 60.0
_last_fired: Dict[str, float] = {}

DEFAULT_RULES: Dict[str, Any] = {
    "enabled": True,
    "rules": {
        "tps_low": {"enabled": False, "value": 5.0, "label": "生成速度 TPS 过低"},
        "ttft_high": {"enabled": False, "value": 3000.0, "label": "首 Token 延迟过高"},
        "e2e_high": {"enabled": False, "value": 30000.0, "label": "端到端延迟过高"},
        "vram_high": {"enabled": True, "value": 90.0, "label": "显存占用过高"},
        "gpu_temp_high": {"enabled": True, "value": 85.0, "label": "GPU 温度过高"},
        "error_rate": {"enabled": True, "value": 0.2, "label": "请求错误率过高"},
        "gpu_idle": {"enabled": False, "value": 5.0, "label": "GPU 长时间空闲"},
    },
}

# 指标中文名，用于告警文案
_METRIC_LABEL = {
    "tps_low": "生成速度(TPS)",
    "ttft_high": "首Token延迟",
    "e2e_high": "端到端延迟",
    "vram_high": "显存占用",
    "gpu_temp_high": "GPU温度",
    "error_rate": "错误率",
    "gpu_idle": "GPU利用率",
}

# 值方向：低于 / 高于
_OP = {
    "tps_low": "lt",
    "ttft_high": "gt",
    "e2e_high": "gt",
    "vram_high": "gt",
    "gpu_temp_high": "gt",
    "error_rate": "gt",
    "gpu_idle": "lt",
}


def get_rules() -> Dict[str, Any]:
    saved = db.get_setting("alert_rules")
    if not saved:
        return DEFAULT_RULES
    merged = {**DEFAULT_RULES, **saved}
    merged["rules"] = {**DEFAULT_RULES["rules"], **(saved.get("rules") or {})}
    return merged


def save_rules(rules: Dict[str, Any]) -> Dict[str, Any]:
    merged = get_rules()
    if "enabled" in rules:
        merged["enabled"] = bool(rules["enabled"])
    incoming = rules.get("rules") or {}
    for key, val in incoming.items():
        if key in merged["rules"] and isinstance(val, dict):
            merged["rules"][key] = {**merged["rules"][key], **val}
    db.set_setting("alert_rules", merged)
    return merged


def _fire(rule_key: str, value: float, threshold: float, message: str,
          level: str = "warning") -> bool:
    now = time.time()
    if now - _last_fired.get(rule_key, 0) < DEDUP_WINDOW:
        return False
    _last_fired[rule_key] = now
    db.add_alert(rule_key, _METRIC_LABEL.get(rule_key, rule_key), value, threshold,
                 _OP.get(rule_key, "gt"), level, message)
    return True


def evaluate() -> int:
    """按当前规则评估一次，返回新产生的告警条数。"""
    rules = get_rules()
    if not rules.get("enabled", True):
        return 0
    fired = 0
    sample = collector.last_sample or {}
    conf = rules["rules"]

    def on(key: str) -> bool:
        return bool(conf.get(key, {}).get("enabled"))

    # --- 显存 ---
    if on("vram_high"):
        vram, total = sample.get("vram_used_mb"), sample.get("vram_total_mb")
        if vram and total:
            pct = vram / total * 100
            if pct > float(conf["vram_high"]["value"]):
                fired += _fire("vram_high", round(pct, 1), float(conf["vram_high"]["value"]),
                               f"显存占用 {pct:.1f}%（{vram / 1024:.1f} GiB / "
                               f"{total / 1024:.1f} GiB）超过阈值 "
                               f"{conf['vram_high']['value']}%", "critical")

    # --- GPU 温度 ---
    if on("gpu_temp_high"):
        temp = sample.get("gpu_temp")
        if temp and temp > float(conf["gpu_temp_high"]["value"]):
            fired += _fire("gpu_temp_high", temp, float(conf["gpu_temp_high"]["value"]),
                           f"GPU 温度 {temp:.0f}°C 超过阈值 "
                           f"{conf['gpu_temp_high']['value']}°C", "critical")

    # --- GPU 空闲（仅在无活跃推理时判定）---
    if on("gpu_idle"):
        util = sample.get("gpu_util")
        active = sample.get("active_reqs") or 0
        if util is not None and active == 0 and util < float(conf["gpu_idle"]["value"]):
            fired += _fire("gpu_idle", util, float(conf["gpu_idle"]["value"]),
                           f"GPU 利用率仅 {util:.1f}%，持续低于阈值 "
                           f"{conf['gpu_idle']['value']}%", "info")

    # --- 推理指标：取最近 30 秒内的单条/基准请求 ---
    if on("tps_low") or on("ttft_high") or on("e2e_high"):
        for ctx in tester._active_runs.values():  # noqa: SLF001 - 内部共享运行态
            oks = [r for r in ctx.results if r.get("status") == "ok"]
            if not oks:
                continue
            recent = oks[-20:]
            if on("tps_low"):
                avg_tps = sum(r["tps"] for r in recent if r.get("tps")) / max(
                    1, len([r for r in recent if r.get("tps")]))
                if avg_tps and avg_tps < float(conf["tps_low"]["value"]):
                    fired += _fire("tps_low", round(avg_tps, 2),
                                   float(conf["tps_low"]["value"]),
                                   f"平均生成速度 {avg_tps:.1f} tokens/s 低于阈值 "
                                   f"{conf['tps_low']['value']}", "warning")
            if on("ttft_high"):
                ttfts = [r["ttft_ms"] for r in recent if r.get("ttft_ms")]
                if ttfts:
                    avg = sum(ttfts) / len(ttfts)
                    if avg > float(conf["ttft_high"]["value"]):
                        fired += _fire("ttft_high", round(avg, 1),
                                       float(conf["ttft_high"]["value"]),
                                       f"平均首 Token 延迟 {avg:.0f} ms 超过阈值 "
                                       f"{conf['ttft_high']['value']} ms", "warning")
            if on("e2e_high"):
                e2es = [r["e2e_ms"] for r in recent if r.get("e2e_ms")]
                if e2es:
                    avg = sum(e2es) / len(e2es)
                    if avg > float(conf["e2e_high"]["value"]):
                        fired += _fire("e2e_high", round(avg, 1),
                                       float(conf["e2e_high"]["value"]),
                                       f"平均端到端延迟 {avg:.0f} ms 超过阈值 "
                                       f"{conf['e2e_high']['value']} ms", "warning")
            if on("error_rate"):
                rate = ctx.error_rate()
                if len(ctx.results) >= 8 and rate > float(conf["error_rate"]["value"]):
                    fired += _fire("error_rate", round(rate, 4),
                                   float(conf["error_rate"]["value"]),
                                   f"压测错误率 {rate * 100:.1f}% 超过阈值 "
                                   f"{float(conf['error_rate']['value']) * 100:.0f}%",
                                   "critical")
            if fired:
                log.info("告警触发 %d 条（run #%s）", fired, ctx.run_id)
    return fired
