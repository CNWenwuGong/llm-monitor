"""指标覆盖清单（Metric Catalog）。

把「本地大模型评测/监控」常用指标按四大类集中登记，并标注本平台的取数方式与实现状态，
供前端渲染「指标覆盖」面板，也作为回答「这些指标你都有吗」的机器可读事实来源。

状态取值
--------
* ``builtin``  —— 已内置，可直接跑出真实数值
* ``partial``  —— 已实现但口径受限 / 依赖外部服务（如 judge、NVML），会在结果里注明
* ``planned``  —— 暂未内置，清单里明确标出，不做假数据

本模块**只登记事实**，不产生任何模拟数值。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

COUNT_LABELS = {
    "builtin": "已内置",
    "partial": "部分支持",
    "planned": "未内置",
}


def _m(key: str, name: str, en: str, status: str, source: str, how: str,
       suite: str = "", endpoint: str = "") -> Dict[str, Any]:
    return {
        "key": key, "name": name, "en": en, "status": status,
        "available": status in ("builtin", "partial"),
        "source": source, "how": how, "suite": suite, "endpoint": endpoint,
    }


# --------------------------------------------------------------------------
# 四大类登记表
# --------------------------------------------------------------------------
CATEGORIES: List[Dict[str, Any]] = [
    {
        "id": "ability",
        "label": "能力 / 效果指标",
        "desc": "衡量模型「会不会做题、做得好不好」，以准确率、相似度、裁判评分为主。",
        "metrics": [
            _m("mmlu", "MMLU 多学科知识", "MMLU", "builtin", "eval:ability",
               "18 题精简版，按选项判定准确率", suite="mmlu_mini",
               endpoint="/api/eval/start"),
            _m("gsm8k", "GSM8K 数学推理", "GSM8K", "builtin", "eval:ability",
               "10 题，抽取最终数值做数值匹配", suite="gsm8k_mini",
               endpoint="/api/eval/start"),
            _m("bbh", "BBH 复杂推理", "BBH", "builtin", "eval:ability",
               "8 题大模型难题子集，选项判定", suite="bbh_mini",
               endpoint="/api/eval/start"),
            _m("humaneval", "HumanEval / MBPP 代码生成", "HumanEval / MBPP",
               "partial", "eval:ability",
               "8 题带单测，子进程沙箱执行；需勾选「允许执行代码」才启用",
               suite="humaneval_mini", endpoint="/api/eval/start"),
            _m("pass_at_k", "pass@k 无偏估计", "pass@k", "builtin", "eval:ability",
               "同一题采样 k 次，用 1-C(n-c,k)/C(n,k) 作无偏估计", suite="humaneval_mini",
               endpoint="/api/eval/start"),
            _m("math", "MATH / MathVista", "MATH / MathVista", "planned", "—",
               "未内置竞赛级数学与多模态数学题集；如需可后补数据集"),
            _m("bleu", "BLEU 文本相似度", "BLEU", "builtin", "eval:quality",
               "method2 平滑 + 长度罚分，中文按字切分", suite="longbench_lite",
               endpoint="/api/eval/start"),
            _m("rouge", "ROUGE-1/2/L", "ROUGE", "builtin", "eval:quality",
               "P/R/F 三口径 + 加权 F", suite="longbench_lite",
               endpoint="/api/eval/start"),
            _m("chrf", "CHRF++", "CHRF++", "builtin", "eval:quality",
               "字符 1-6gram + 词 1-2gram，β=2", suite="longbench_lite",
               endpoint="/api/eval/start"),
            _m("f1", "F1 Score（抽取类）", "F1", "builtin", "eval:quality",
               "NER 实体集合的 P/R/F1", suite="ner_f1", endpoint="/api/eval/start"),
            _m("mtbench", "MT-Bench 多轮对话", "MT-Bench", "partial", "eval:ability",
               "8 维度 × 2 轮；配了裁判模型走 LLM-as-judge，否则退化为关键词覆盖启发式",
               suite="mtbench_lite", endpoint="/api/eval/start"),
            _m("arena_hard", "Arena Hard", "Arena Hard", "planned", "—",
               "需带参考模型的成对胜负判定，未内置"),
        ],
    },
    {
        "id": "efficiency",
        "label": "效率指标",
        "desc": "衡量「快不快、省不省」，覆盖上下文、吞吐、延迟与显存。",
        "metrics": [
            _m("context_window", "上下文窗口", "Context Window", "builtin", "compute",
               "从 llama-server 启动参数 -c 或 GGUF 元数据读取", endpoint="/api/metrics/efficiency"),
            _m("niah", "NIAH 大海捞针", "NIAH", "builtin", "eval:quality",
               "上下文长度 × 插入深度热力矩阵，程序化生成干草堆", suite="niah",
               endpoint="/api/eval/start"),
            _m("longbench", "LongBench 长文理解", "LongBench", "partial", "eval:quality",
               "6 条长文问答，用 ROUGE/BLEU/CHRF 打分（精简版）", suite="longbench_lite",
               endpoint="/api/eval/start"),
            _m("token_speed", "生成速度 Token/s", "Token/s", "builtin", "tester",
               "单条测试 / 基准 / 压测逐请求实测；可拆 prefill 与 decode 两段",
               endpoint="/api/test/single"),
            _m("ttft", "首 Token 延迟 TTFT", "TTFT", "builtin", "tester",
               "从发请求到收到第一个 delta 的耗时，含 P50/P90/P95/P99",
               endpoint="/api/test/loadtest/start"),
            _m("tps_throughput", "TPS / 整体吞吐", "TPS / Throughput", "builtin", "tester",
               "成功请求产出 token 总量 ÷ 墙钟时长，压测中按并发档位分别统计",
               endpoint="/api/test/loadtest/start"),
            _m("vram", "显存占用（权重 + KV Cache）", "VRAM / KV Cache", "partial", "compute",
               "权重按量化字节数折算；KV Cache 用 n_layer×n_ctx×n_head_kv×head_dim 精确计算；"
               "实时占用走 NVML", endpoint="/api/metrics/efficiency"),
            _m("ppl", "困惑度 PPL", "Perplexity", "partial", "compute",
               "teacher-forcing（llama.cpp 逐 token 递进 / vLLM echo）优先，"
               "不支持时标注原因返回，不做臆测", endpoint="/api/metrics/efficiency"),
        ],
    },
    {
        "id": "alignment",
        "label": "对齐 & 安全指标",
        "desc": "衡量模型「守不守规矩、会不会胡说」，覆盖拒绝、幻觉、遵循与越狱。",
        "metrics": [
            _m("refusal", "有害输出拒绝率", "Harmful Refusal Rate", "builtin", "eval:safety",
               "12 类有害请求，用正则+标记词判定是否明确拒绝", suite="safety_harmful",
               endpoint="/api/eval/start"),
            _m("hallucination", "幻觉率", "Hallucination Rate", "partial", "eval:quality",
               "TruthfulQA 精简版，1-准确率即幻觉率；未接 FactScore 外部工具",
               suite="truthfulqa_lite", endpoint="/api/eval/start"),
            _m("instruction", "指令遵循度", "Instruction Following", "builtin", "eval:quality",
               "10 条可机检约束（字数/格式/禁用词/JSON 等），统计逐条通过率",
               suite="ifeval_lite", endpoint="/api/eval/start"),
            _m("preference", "偏好对齐分数", "Preference Score", "partial", "eval:ability",
               "来自 MT-Bench 的多维度评分；无裁判模型时降级为启发式，仅供参考",
               suite="mtbench_lite", endpoint="/api/eval/start"),
            _m("jailbreak", "越狱成功率", "Jailbreak Success Rate", "builtin", "eval:safety",
               "8 种越狱模板，判定是否被绕过安全策略", suite="safety_jailbreak",
               endpoint="/api/eval/start"),
        ],
    },
    {
        "id": "stability",
        "label": "工程稳定性指标",
        "desc": "衡量「扛不扛得住」，覆盖尾延迟、错误率、并发上限与冷启动。",
        "metrics": [
            _m("p95_p99", "P95 / P99 延迟", "P95 / P99 Latency", "builtin", "tester",
               "对每档并发的 TTFT 与端到端延迟做分位数统计（线性插值）",
               endpoint="/api/test/loadtest/start"),
            _m("error_rate", "错误率", "Error Rate", "builtin", "tester",
               "非 ok 状态请求占比；压测中超过 20% 自动熔断",
               endpoint="/api/test/loadtest/start"),
            _m("concurrency_limit", "并发上限", "Concurrency Limit", "builtin", "tester",
               "阶梯压测后按「QPS 收益递减 / 尾延迟超基线 3 倍 / 错误率>2%」判定拐点",
               endpoint="/api/test/loadtest/start"),
            _m("cold_start", "冷启动时间", "Cold Start", "partial", "tester",
               "Ollama 可真实冷启（卸载后重新加载计时）；llama.cpp 常驻服务无法冷启，"
               "此时返回代理指标并给出 reason", endpoint="/api/metrics/cold-start"),
        ],
    },
]


def catalog(conn: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """返回指标覆盖清单与汇总统计。``conn`` 仅用于附加后端能力提示，可为空。"""
    groups = []
    counts = {"builtin": 0, "partial": 0, "planned": 0}
    for cat in CATEGORIES:
        metrics = []
        for m in cat["metrics"]:
            counts[m["status"]] = counts.get(m["status"], 0) + 1
            metrics.append(dict(m))
        groups.append({**cat, "metrics": metrics,
                       "builtin": sum(1 for m in metrics if m["status"] == "builtin"),
                       "partial": sum(1 for m in metrics if m["status"] == "partial"),
                       "planned": sum(1 for m in metrics if m["status"] == "planned")})
    total = sum(counts.values())
    covered = counts["builtin"] + counts["partial"]
    return {
        "groups": groups,
        "summary": {
            "total": total,
            "builtin": counts["builtin"],
            "partial": counts["partial"],
            "planned": counts["planned"],
            "covered": covered,
            "coverage": round(covered / total, 4) if total else 0.0,
            "labels": COUNT_LABELS,
        },
    }
