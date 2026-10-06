"""评测指标与资源估算的纯算法实现（无第三方依赖，全部可单测）。

包含四类指标的计算底座
----------------------
* **质量/相似度**：BLEU-4（含平滑）、ROUGE-1/2/L、CHRF++、F1（抽取类）、精确匹配
* **能力判别**：多选判定、数值判定、代码 pass@k（无偏估计）
* **效率**：困惑度 PPL、prefill/decode 拆分、FLOPs 理论算力
* **资源**：GGUF 头部解析 → KV Cache 显存精确计算；llama.cpp 启动参数解析

设计原则：这里的函数**不依赖任何网络与模型**，输入输出都是纯数据，
因此可以用 `python -m backend.metrics_ext --selftest` 单独回归。
"""

from __future__ import annotations

import math
import os
import re
import struct
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 通用统计
# --------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    if len(vs) == 1:
        return round(vs[0], 2)
    k = (len(vs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return round(vs[int(k)], 2)
    return round(vs[lo] + (vs[hi] - vs[lo]) * (k - lo), 2)


def mean(values: Sequence[Optional[float]]) -> Optional[float]:
    vs = [v for v in values if v is not None]
    return round(sum(vs) / len(vs), 4) if vs else None


# --------------------------------------------------------------------------
# 分词（中英混排友好：CJK 按字切、拉丁按词切、标点单独成 token）
# --------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_WORD_CH = re.compile(r"[a-zA-Z0-9_']")


def tokenize(text: str) -> List[str]:
    """把文本切成 token 列表：CJK 单字成 token，拉丁词按连续字母数字聚合。"""
    text = (text or "").lower()
    out: List[str] = []
    buf: List[str] = []
    for ch in text:
        if _CJK_RE.match(ch):
            if buf:
                out.append("".join(buf))
                buf = []
            out.append(ch)
        elif _WORD_CH.match(ch):
            buf.append(ch)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
            if not ch.isspace():
                out.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _ngrams(tokens: Sequence[str], n: int) -> List[Tuple[str, ...]]:
    if len(tokens) < n:
        return []
    return [tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


# --------------------------------------------------------------------------
# BLEU-4（Chen & Cherry / NLTK method2 风格平滑）
# --------------------------------------------------------------------------
def bleu(hypothesis: str, references: Sequence[str], max_n: int = 4,
         smooth: bool = True) -> Dict[str, Any]:
    """标准 BLEU（0~1）。多参考句时取每个 n-gram 的最大参考计数（clipping）。"""
    hyp = tokenize(hypothesis)
    refs = [tokenize(r) for r in references if r]
    if not hyp or not refs:
        return {"bleu": 0.0, "precisions": [0.0] * max_n, "bp": 0.0,
                "length_ratio": 0.0, "hyp_len": len(hyp)}

    precisions: List[float] = []
    raw: List[float] = []
    for n in range(1, max_n + 1):
        hg = Counter(_ngrams(hyp, n))
        total = sum(hg.values())
        max_ref: Counter = Counter()
        for rt in refs:
            rg = Counter(_ngrams(rt, n))
            for k, v in rg.items():
                if v > max_ref[k]:
                    max_ref[k] = v
        clipped = sum(min(c, max_ref.get(k, 0)) for k, c in hg.items())
        raw.append(clipped / total if total else 0.0)

    # 出现零命中时启用 method2 平滑（n>1 分子分母各 +1），避免整句 BLEU 直接归零
    need_smooth = smooth and any(p == 0.0 for p in raw[1:])
    for n, p in enumerate(raw, start=1):
        if need_smooth and n > 1:
            hg = Counter(_ngrams(hyp, n))
            total = sum(hg.values())
            max_ref = Counter()
            for rt in refs:
                rg = Counter(_ngrams(rt, n))
                for k, v in rg.items():
                    if v > max_ref[k]:
                        max_ref[k] = v
            clipped = sum(min(c, max_ref.get(k, 0)) for k, c in hg.items())
            p = (clipped + 1) / (total + 1) if total else 0.0
        precisions.append(p)

    ref_len = min((len(r) for r in refs), key=lambda L: (abs(L - len(hyp)), L))
    ratio = len(hyp) / ref_len if ref_len else 0.0
    # 长度罚分 BP = min(1, exp(1 - r/c))：c 为候选长度、r 为参考长度
    bp = 1.0 if (ref_len == 0 or len(hyp) >= ref_len) else math.exp(1.0 - ref_len / len(hyp))
    if any(p == 0.0 for p in precisions):
        score = 0.0
    else:
        score = bp * math.exp(sum(math.log(p) for p in precisions) / len(precisions))
    return {
        "bleu": round(score, 4),
        "precisions": [round(p, 4) for p in precisions],
        "bp": round(bp, 4),
        "length_ratio": round(ratio, 4),
        "hyp_len": len(hyp),
        "ref_len": ref_len,
    }


# --------------------------------------------------------------------------
# ROUGE（N / L）
# --------------------------------------------------------------------------
def _prf(match: float, pred_total: float, gold_total: float) -> Dict[str, float]:
    p = match / pred_total if pred_total else 0.0
    r = match / gold_total if gold_total else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


def rouge_n(hypothesis: str, reference: str, n: int = 1) -> Dict[str, float]:
    """ROUGE-N（按 clipped count 计算，与 BLEU 的 n-gram 重叠口径一致）。"""
    hyp = tokenize(hypothesis)
    ref = tokenize(reference)
    hg = Counter(_ngrams(hyp, n))
    rg = Counter(_ngrams(ref, n))
    match = sum(min(c, rg.get(k, 0)) for k, c in hg.items())
    return _prf(match, sum(hg.values()), sum(rg.values()))


def _lcs(a: Sequence[str], b: Sequence[str]) -> List[str]:
    """最长公共子序列（返回序列本身，便于调试）。空间优化到 O(min(n,m))。"""
    n, m = len(a), len(b)
    if not n or not m:
        return []
    prev = [0] * (m + 1)
    # 保留方向矩阵以便回溯
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        ai = a[i - 1]
        row = table[i]
        prow = table[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                row[j] = prow[j - 1] + 1
            else:
                row[j] = prow[j] if prow[j] >= row[j - 1] else row[j - 1]
    del prev
    # 回溯
    i, j = n, m
    seq: List[str] = []
    while i > 0 and j > 0:
        if a[i - 1] == b[j - 1]:
            seq.append(a[i - 1])
            i -= 1
            j -= 1
        elif table[i - 1][j] >= table[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return list(reversed(seq))


def rouge_l(hypothesis: str, reference: str) -> Dict[str, float]:
    """ROUGE-L（基于 LCS 的 F1，β=1）。"""
    hyp = tokenize(hypothesis)
    ref = tokenize(reference)
    lcs = len(_lcs(hyp, ref))
    return _prf(lcs, len(hyp), len(ref))


def rouge(hypothesis: str, reference: str,
          weights: Tuple[float, float, float] = (0.2, 0.4, 0.4)) -> Dict[str, Any]:
    """ROUGE 1/2/L 汇总，并给出加权总分（默认 0.2/0.4/0.4，与 ROUGE-2 更看重多词一致性）。"""
    r1 = rouge_n(hypothesis, reference, 1)
    r2 = rouge_n(hypothesis, reference, 2)
    rl = rouge_l(hypothesis, reference)
    total = weights[0] * r1["f1"] + weights[1] * r2["f1"] + weights[2] * rl["f1"]
    return {"rouge1": r1, "rouge2": r2, "rougeL": rl,
            "rouge_weighted": round(total, 4)}


# --------------------------------------------------------------------------
# CHRF++（Popović 2017：字符 1~6-gram + 词 1~2-gram，β=2，取平均）
# --------------------------------------------------------------------------
def _f_beta(p: float, r: float, beta: float) -> float:
    b2 = beta * beta
    return (1 + b2) * p * r / (b2 * p + r) if (b2 * p + r) else 0.0


def chrf_pp(hypothesis: str, reference: str, char_n_max: int = 6,
            word_n_max: int = 2, beta: float = 2.0) -> Dict[str, Any]:
    """CHRF++ 分数（0~1）。字符 n-gram 忽略空白，词 n-gram 按空白切分。"""
    hyp_c = "".join((hypothesis or "").split()).lower()
    ref_c = "".join((reference or "").split()).lower()
    hyp_w = (hypothesis or "").lower().split()
    ref_w = (reference or "").lower().split()

    char_scores: List[float] = []
    for n in range(1, char_n_max + 1):
        hg = Counter(_ngrams(list(hyp_c), n))
        rg = Counter(_ngrams(list(ref_c), n))
        match = sum(min(c, rg.get(k, 0)) for k, c in hg.items())
        if not sum(hg.values()) and not sum(rg.values()):
            continue  # 双方都没有该阶 n-gram（文本过短）→ 不参与平均
        p = match / sum(hg.values()) if sum(hg.values()) else 0.0
        r = match / sum(rg.values()) if sum(rg.values()) else 0.0
        char_scores.append(_f_beta(p, r, beta))

    word_scores: List[float] = []
    for n in range(1, word_n_max + 1):
        hg = Counter(_ngrams(hyp_w, n))
        rg = Counter(_ngrams(ref_w, n))
        match = sum(min(c, rg.get(k, 0)) for k, c in hg.items())
        if not sum(hg.values()) and not sum(rg.values()):
            continue
        p = match / sum(hg.values()) if sum(hg.values()) else 0.0
        r = match / sum(rg.values()) if sum(rg.values()) else 0.0
        word_scores.append(_f_beta(p, r, beta))

    char_f = sum(char_scores) / len(char_scores) if char_scores else 0.0
    word_f = sum(word_scores) / len(word_scores) if word_scores else 0.0
    all_scores = char_scores + word_scores
    score = sum(all_scores) / len(all_scores) if all_scores else 0.0
    return {"chrf": round(score, 4), "chrf_char": round(char_f, 4),
            "chrf_word": round(word_f, 4), "beta": beta}


# --------------------------------------------------------------------------
# 抽取类 F1 / 精确匹配 / 数值判定 / 多选判定
# --------------------------------------------------------------------------
def _set_from(value: Any) -> set:
    if value is None:
        return set()
    if isinstance(value, (set, frozenset)):
        return {str(v).strip().lower() for v in value}
    if isinstance(value, (list, tuple)):
        return {str(v).strip().lower() for v in value}
    return {str(value).strip().lower()}


def f1_sets(predicted: Any, gold: Any) -> Dict[str, float]:
    """抽取类任务（NER / 信息抽取）的集合 F1。"""
    p, g = _set_from(predicted), _set_from(gold)
    tp = len(p & g)
    precision = tp / len(p) if p else 0.0
    recall = tp / len(g) if g else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4), "tp": tp, "pred": len(p), "gold": len(g)}


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def normalize_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[，。；：、！？（）【】《》“”‘’·,.;:!?()\[\]{}\"'`]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def exact_match(predicted: str, gold: str) -> bool:
    return normalize_text(predicted) == normalize_text(gold)


def extract_number(text: str) -> Optional[float]:
    """抽取答案里的最后一个数值（GSM8K 风格：结果通常写在句末）。"""
    nums = _NUM_RE.findall((text or "").replace(",", ""))
    if not nums:
        return None
    try:
        return float(nums[-1])
    except ValueError:
        return None


def numeric_match(predicted: str, gold: str, abs_tol: float = 1e-4) -> Dict[str, Any]:
    pv, gv = extract_number(predicted), extract_number(gold)
    if pv is None or gv is None:
        return {"ok": False, "pred": pv, "gold": gv}
    return {"ok": abs(pv - gv) <= abs_tol, "pred": pv, "gold": gv}


_CHOICE_RE = re.compile(r"(?:^|[^A-Za-z])([A-D])(?![A-Za-z])")


def extract_choice(text: str, allow_text_match: bool = True,
                   options: Optional[Sequence[str]] = None) -> Optional[str]:
    """从模型回答里抽取 A/B/C/D。

    优先级：`答案：X` / `Answer: X` / `(X)` / 首个独立大写字母；
    都不中时，若给了选项文本，退化为「回答里包含哪个选项的原文」。
    """
    if not text:
        return None
    t = text.strip()
    for pat in (r"(?:答案|正确选项|选择|answer|answer is|choice)\s*[:：]?\s*\(?([A-D])\)?",
                r"^\s*\(?([A-D])\)?\s*[.、,:：)]",
                r"\(([A-D])\)"):
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    if allow_text_match and options:
        # 选项文本匹配：整句等于某选项最优；否则取唯一最长命中，避免误判
        low = normalize_text(t)
        exacts = [i for i, opt in enumerate(options)
                  if normalize_text(str(opt)) and normalize_text(str(opt)) == low]
        if len(exacts) == 1:
            return "ABCD"[exacts[0]] if exacts[0] < 4 else None
        hits = []
        for idx, opt in enumerate(options):
            key = normalize_text(str(opt))
            if len(key) >= 2 and key in low:
                hits.append((len(key), "ABCD"[idx] if idx < 4 else None))
        if hits:
            hits.sort(reverse=True)
            if hits[0][1] and (len(hits) == 1 or hits[0][0] > hits[1][0]):
                return hits[0][1]
    m = _CHOICE_RE.search(t)
    return m.group(1).upper() if m else None


# --------------------------------------------------------------------------
# pass@k（Codex 论文无偏估计）
# --------------------------------------------------------------------------
def pass_at_k(n: int, c: int, k: int) -> float:
    """n 个样本中 c 个通过，pass@k = 1 - C(n-c, k) / C(n, k)。"""
    if n <= 0 or k <= 0:
        return 0.0
    c = max(0, min(c, n))
    if k > n:
        k = n
    if n - c < k:
        return 1.0
    return round(1.0 - math.comb(n - c, k) / math.comb(n, k), 4)


# --------------------------------------------------------------------------
# 困惑度 PPL
# --------------------------------------------------------------------------
def perplexity(logprobs: Sequence[Optional[float]]) -> Optional[float]:
    """PPL = exp(-1/N · Σ ln p)。输入为自然对数概率（llama.cpp 的 logprob 即为 ln p）。"""
    vals = [float(x) for x in logprobs if x is not None]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    try:
        return round(math.exp(-avg), 4)
    except OverflowError:
        return None


def mean_logprob(logprobs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [float(x) for x in logprobs if x is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


# --------------------------------------------------------------------------
# 效率：prefill / decode 拆分与 FLOPs
# --------------------------------------------------------------------------
def split_throughput(prompt_tokens: Optional[int], completion_tokens: Optional[int],
                     ttft_ms: Optional[float], e2e_ms: Optional[float]) -> Dict[str, Any]:
    """把端到端延迟拆成 prefill（算力密集）与 decode（访存瓶颈）两段吞吐。"""
    out: Dict[str, Any] = {"prefill_tps": None, "decode_tps": None,
                           "prefill_ms": None, "decode_ms": None}
    if ttft_ms and prompt_tokens:
        out["prefill_ms"] = round(ttft_ms, 1)
        out["prefill_tps"] = round(prompt_tokens / (ttft_ms / 1000), 2)
    if e2e_ms and ttft_ms is not None and completion_tokens:
        gen_ms = max(1.0, e2e_ms - ttft_ms)
        out["decode_ms"] = round(gen_ms, 1)
        out["decode_tps"] = round(completion_tokens / (gen_ms / 1000), 2)
    return out


def flops_theory(params: Optional[float], tokens: Optional[float],
                 factor: float = 2.0) -> Optional[float]:
    """理论浮点运算量：前向传播约 2·N·T（乘加各算一次）。"""
    if not params or not tokens:
        return None
    return round(factor * float(params) * float(tokens), 2)


def achieved_tflops(flops: Optional[float], seconds: Optional[float]) -> Optional[float]:
    if not flops or not seconds or seconds <= 0:
        return None
    return round(flops / seconds / 1e12, 3)


# --------------------------------------------------------------------------
# GGUF 头部解析（本地文件，只读文件头，不加载权重）
# --------------------------------------------------------------------------
_GGUF_SCALAR = {
    0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?",
    10: "Q", 11: "q", 12: "d",
}
_GGUF_SIZE = {"B": 1, "b": 1, "H": 2, "h": 2, "I": 4, "i": 4, "f": 4,
              "?": 1, "Q": 8, "q": 8, "d": 8}

# llama.cpp KV 量化类型 → 每元素字节数（含 block 元数据开销）
KV_TYPE_BYTES: Dict[str, float] = {
    "f32": 4.0, "f16": 2.0, "bf16": 2.0,
    "q8_0": 34 / 32, "q4_0": 18 / 32, "q4_1": 20 / 32,
    "q5_0": 22 / 32, "q5_1": 24 / 32, "iq4_nl": 18 / 32,
    "q4_0_4_4": 18 / 32, "q4_0_4_8": 18 / 32, "q4_0_8_8": 18 / 32,
}

_GGUF_CACHE: Dict[Tuple[str, float], Dict[str, Any]] = {}


def _gguf_read_str(f) -> str:
    n = struct.unpack("<Q", f.read(8))[0]
    return f.read(n).decode("utf-8", "replace")


def _gguf_read_value(f, t: int, depth: int = 0) -> Any:
    if t == 8:
        return _gguf_read_str(f)
    if t == 9:
        if depth > 3:
            raise ValueError("嵌套数组过深")
        et = struct.unpack("<I", f.read(4))[0]
        cnt = struct.unpack("<Q", f.read(8))[0]
        # 只精确读取前 8 个元素，其余按类型跳过（避免大词表数组拖慢）
        head = [_gguf_read_value(f, et, depth + 1) for _ in range(min(cnt, 8))]
        for _ in range(max(0, cnt - 8)):
            _gguf_read_value(f, et, depth + 1)
        return head
    fmt = _GGUF_SCALAR.get(t)
    if not fmt:
        raise ValueError(f"未知 GGUF 值类型 {t}")
    return struct.unpack("<" + fmt, f.read(_GGUF_SIZE[fmt]))[0]


def gguf_hyperparams(path: str, with_params: bool = True) -> Dict[str, Any]:
    """读取 GGUF 头部元数据，得到层数 / KV 头数 / 头维度等 KV Cache 计算所需超参。

    只读文件头（不 mmap 权重），并带 (路径, mtime) 缓存。
    ``with_params=True`` 时顺带累加张量维度，得到精确参数量（用于 FLOPs 估算）。
    """
    if not path:
        return {"ok": False, "error": "未提供模型文件路径"}
    try:
        st = os.stat(path)
    except OSError as exc:
        return {"ok": False, "error": f"无法访问模型文件: {exc}"}
    key = (os.path.abspath(path), st.st_mtime, with_params)
    if key in _GGUF_CACHE:
        return _GGUF_CACHE[key]

    kv: Dict[str, Any] = {}
    params = 0
    n_tensors_read = 0
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic != b"GGUF":
                return {"ok": False, "error": "不是 GGUF 文件"}
            version = struct.unpack("<I", f.read(4))[0]
            n_tensors = struct.unpack("<Q", f.read(8))[0]
            n_kv = struct.unpack("<Q", f.read(8))[0]
            for _ in range(n_kv):
                k = _gguf_read_str(f)
                t = struct.unpack("<I", f.read(4))[0]
                v = _gguf_read_value(f, t)
                if len(kv) < 600:
                    kv[k] = v
            if with_params:
                # 张量信息段：name / n_dims / dims[] / ggml_type / offset
                for _ in range(n_tensors):
                    _gguf_read_str(f)
                    n_dims = struct.unpack("<I", f.read(4))[0]
                    dims = struct.unpack("<" + "Q" * n_dims, f.read(8 * n_dims))
                    struct.unpack("<I", f.read(4))[0]      # ggml_type
                    struct.unpack("<Q", f.read(8))[0]      # offset
                    prod = 1
                    for d in dims:
                        prod *= int(d)
                    params += prod
                    n_tensors_read += 1
    except (OSError, ValueError, struct.error) as exc:
        return {"ok": False, "error": f"解析 GGUF 头失败: {exc}"}

    arch = str(kv.get("general.architecture", "") or "")

    def pick(suffix: str) -> Any:
        for cand in (f"{arch}.{suffix}" if arch else "", suffix):
            if cand and cand in kv:
                return kv[cand]
        # 兜底：任意架构前缀
        for k, v in kv.items():
            if k.endswith("." + suffix):
                return v
        return None

    out = {
        "ok": True,
        "path": os.path.abspath(path),
        "size_bytes": st.st_size,
        "size_gib": round(st.st_size / 2 ** 30, 2),
        "gguf_version": version,
        "tensor_count": n_tensors,
        "architecture": arch,
        "name": kv.get("general.name"),
        "basename": kv.get("general.basename"),
        "file_type": kv.get("general.file_type"),
        "block_count": pick("block_count"),
        "head_count": pick("attention.head_count"),
        "head_count_kv": pick("attention.head_count_kv"),
        "key_length": pick("attention.key_length"),
        "value_length": pick("attention.value_length"),
        "embedding_length": pick("embedding_length"),
        "context_length": pick("context_length"),
        "parameter_count": kv.get("general.parameter_count") or (params or None),
        "parameter_count_source": ("gguf.general.parameter_count"
                                   if kv.get("general.parameter_count")
                                   else (f"张量维度累加（{n_tensors_read} 个张量）"
                                         if params else None)),
        "rope_freq_base": pick("rope.freq_base"),
        "quantization": kv.get("general.quantization_version"),
    }
    if out["head_count_kv"] is None:
        out["head_count_kv"] = out["head_count"]
    _GGUF_CACHE[key] = out
    return out


def kv_cache_bytes(n_layer: int, n_ctx: int, n_head_kv: int,
                   key_length: int, value_length: Optional[int] = None,
                   kv_type: str = "f16", slots: int = 1) -> Dict[str, Any]:
    """KV Cache 显存精确计算。

    ``bytes = slots × n_layer × n_ctx × n_head_kv × (key_length + value_length) × bytes_per_elem``

    * ``kv_type`` 取 llama.cpp ``--cache-type-k/-v``（f16 / q8_0 / q4_0 …）
    * ``key_length`` 优先用 GGUF 的 ``attention.key_length``（部分模型与 n_embd/n_head 不同）
    """
    value_length = value_length or key_length
    per_elem = KV_TYPE_BYTES.get((kv_type or "f16").lower())
    if per_elem is None:
        return {"ok": False, "error": f"未识别的 KV 量化类型 {kv_type}"}
    if not all(isinstance(x, int) and x > 0 for x in
               (n_layer, n_ctx, n_head_kv, key_length, value_length, slots)):
        return {"ok": False, "error": "缺少完整的 KV Cache 计算参数"}
    elems_per_token = n_head_kv * (key_length + value_length)
    total_bytes = slots * n_layer * n_ctx * elems_per_token * per_elem
    return {
        "ok": True,
        "kind": kv_type,
        "bytes_per_elem": round(per_elem, 4),
        "per_token_bytes": round(elems_per_token * per_elem, 1),
        "per_slot_mib": round(total_bytes / slots / 2 ** 20, 1),
        "total_mib": round(total_bytes / 2 ** 20, 1),
        "total_gib": round(total_bytes / 2 ** 30, 2),
        "slots": slots,
        "n_ctx": n_ctx,
        "formula": f"{slots}×{n_layer}层×{n_ctx}ctx×{n_head_kv}KV头×"
                   f"({key_length}+{value_length})维×{per_elem:.4f}B/元素",
    }


# --------------------------------------------------------------------------
# llama.cpp / vLLM 启动参数解析（从进程命令行取权威配置）
# --------------------------------------------------------------------------
_ARG_ALIASES: Dict[str, str] = {
    "-m": "model_path", "--model": "model_path",
    "-c": "ctx_size", "--ctx-size": "ctx_size",
    "-ngl": "n_gpu_layers", "--n-gpu-layers": "n_gpu_layers",
    "-b": "batch_size", "--batch-size": "batch_size",
    "-ub": "ubatch_size", "--ubatch-size": "ubatch_size",
    "-np": "parallel", "--parallel": "parallel",
    "-ctk": "cache_type_k", "--cache-type-k": "cache_type_k",
    "-ctv": "cache_type_v", "--cache-type-v": "cache_type_v",
    "-fa": "flash_attn", "--flash-attn": "flash_attn",
    "--port": "port", "--host": "host",
    "-t": "threads", "--threads": "threads",
    "--tensor-split": "tensor_split", "-ts": "tensor_split",
    "-a": "alias", "--alias": "alias",
}
_FLAG_NO_VALUE = {"-fa", "--flash-attn"}


def parse_server_args(cmdline: Sequence[str]) -> Dict[str, Any]:
    """解析 llama-server / vLLM 命令行参数（值为 'auto'/'on' 时转成 True）。

    这些参数是运行时的**权威配置**：KV 量化类型、上下文长度、offload 层数
    在 HTTP 接口里拿不到，只能从进程命令行取。
    """
    out: Dict[str, Any] = {"ok": False, "raw": list(cmdline)}
    args = list(cmdline or [])
    i = 0
    while i < len(args):
        a = args[i]
        key = None
        val: Optional[str] = None
        if "=" in a and a.startswith("-"):
            head, _, tail = a.partition("=")
            key = _ARG_ALIASES.get(head)
            val = tail
        else:
            key = _ARG_ALIASES.get(a)
            if key and key not in _FLAG_NO_VALUE:
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    val = args[i + 1]
                    i += 1
                elif key in ("flash_attn",):
                    val = "on"
            elif key in _FLAG_NO_VALUE:
                val = "on"
        if key:
            if val is None:
                val = "on"
            out[key] = val
        i += 1
    if any(k in out for k in ("ctx_size", "cache_type_k", "n_gpu_layers", "model_path")):
        out["ok"] = True
    for numeric in ("ctx_size", "n_gpu_layers", "batch_size", "ubatch_size",
                    "parallel", "port", "threads"):
        if numeric in out:
            try:
                out[numeric] = int(str(out[numeric]))
            except ValueError:
                pass
    return out


def infer_model_path(raw: str, cwd: Optional[str] = None,
                     extra_dirs: Optional[Sequence[str]] = None) -> Optional[str]:
    """把 server 的 ``-m`` 相对路径还原成绝对路径（按 cwd 与常见目录探测）。"""
    if not raw:
        return None
    cands: List[str] = []
    if os.path.isabs(raw):
        cands.append(raw)
    if cwd:
        cands.append(os.path.join(cwd, raw))
    for d in extra_dirs or []:
        cands.append(os.path.join(d, raw))
    for c in cands:
        if os.path.exists(c):
            return os.path.abspath(c)
    return None


# --------------------------------------------------------------------------
# 自测（python -m backend.metrics_ext --selftest）
# --------------------------------------------------------------------------
def _selftest() -> int:
    fails: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if not cond:
            fails.append(f"{name} {detail}")

    # 分词
    check("tokenize 中英混排", tokenize("KV Cache 很慢") ==
          ["kv", "cache", "很", "慢"], str(tokenize("KV Cache 很慢")))

    # BLEU
    check("BLEU 完全相同为 1", abs(bleu("the cat sat on the mat",
                                       ["the cat sat on the mat"])["bleu"] - 1.0) < 1e-9)
    b = bleu("the cat sat on the mat", ["the cat sat on a mat"])
    check("BLEU 微差落在 (0,1)", 0 < b["bleu"] < 1, str(b["bleu"]))
    check("BLEU 完全不同为 0", bleu("aaaa bbbb", ["cccc dddd"])["bleu"] == 0.0)
    check("BLEU 短句罚分生效",
          abs(bleu("the cat", ["the cat sat on the mat"])["bp"] - math.exp(-2)) < 1e-3,
          str(bleu("the cat", ["the cat sat on the mat"])["bp"]))
    check("BLEU 长句不罚分", bleu("the cat sat on the mat today",
                                 ["the cat sat on the mat"])["bp"] == 1.0)
    check("BLEU 输出 4 项精确率",
          len(bleu("the cat sat on the mat", ["the cat sat on the mat"])["precisions"]) == 4)

    # ROUGE
    check("ROUGE-1 全同为 1", rouge_n("hello world", "hello world", 1)["f1"] == 1.0)
    check("ROUGE-2 半匹配", abs(rouge_n("a b c d", "a b x y", 2)["precision"] - 1 / 3) < 1e-4,
          str(rouge_n("a b c d", "a b x y", 2)))
    check("ROUGE-L 全同为 1", rouge_l("a b c", "a b c")["f1"] == 1.0)
    check("ROUGE-L 子序列", abs(rouge_l("a x b y c", "a b c")["recall"] - 1.0) < 1e-9)
    r = rouge("本地部署需要注意显存", "本地部署时需要注意显存占用")
    check("ROUGE 汇总字段完整", all(k in r for k in ("rouge1", "rouge2", "rougeL", "rouge_weighted")))

    # CHRF++
    check("CHRF++ 全同为 1", abs(chrf_pp("hello world", "hello world")["chrf"] - 1.0) < 1e-9)
    check("CHRF++ 中文全同为 1", abs(chrf_pp("显存占用很高", "显存占用很高")["chrf"] - 1.0) < 1e-9)
    check("CHRF++ 差值为 0", chrf_pp("abc", "xyz")["chrf"] == 0.0)
    partial = chrf_pp("显存占用比较高", "显存占用很高")["chrf"]
    check("CHRF++ 部分匹配介于 0~1", 0 < partial < 1, str(partial))

    # 抽取 F1
    check("F1 完全命中", f1_sets(["a", "b"], ["a", "b"])["f1"] == 1.0)
    check("F1 半数命中", abs(f1_sets(["a", "b"], ["a", "c"])["f1"] - 0.5) < 1e-9)
    check("F1 空预测为 0", f1_sets([], ["a"])["f1"] == 0.0)

    # 数值 / 精确匹配
    check("数值匹配 391=391", numeric_match("所以结果是 391。", "391")["ok"])
    check("数值匹配 千分位", numeric_match("答案是 1,234 元", "1234")["ok"])
    check("数值匹配 不同值判否", not numeric_match("结果是 390", "391")["ok"])
    check("EM 忽略大小写与标点", exact_match("Paris.", "paris"))

    # 多选抽取
    check("多选 答案：B", extract_choice("答案：B") == "B")
    check("多选 Answer is C", extract_choice("Answer is C.") == "C")
    check("多选 (D)", extract_choice("我选 (D)") == "D")
    check("多选 文本兜底", extract_choice("应该是巴黎", options=["伦敦", "巴黎", "东京", "柏林"]) == "B")
    check("多选 无答案返回 None", extract_choice("我不知道") is None)

    # pass@k
    check("pass@1 = c/n", abs(pass_at_k(10, 2, 1) - 0.2) < 1e-9)
    check("pass@k c>=k 为 1", pass_at_k(5, 5, 3) == 1.0)
    check("pass@k 无通过为 0", pass_at_k(10, 0, 5) == 0.0)
    check("pass@10 单调不降", pass_at_k(10, 3, 10) >= pass_at_k(10, 3, 1))

    # PPL
    ppl = perplexity([math.log(0.5)] * 10)
    check("PPL 均匀 0.5 概率 = 2", abs((ppl or 0) - 2.0) < 1e-6, str(ppl))
    check("PPL 空输入为 None", perplexity([]) is None)

    # 吞吐拆分
    sp = split_throughput(1000, 200, 2000, 8000)
    check("prefill 吞吐 = 500 tok/s", sp["prefill_tps"] == 500.0, str(sp))
    check("decode 吞吐 = 33.33 tok/s", abs(sp["decode_tps"] - 33.33) < 0.01, str(sp))

    # FLOPs
    check("FLOPs = 2NT", flops_theory(7e9, 100) == 1.4e12)
    check("实现 TFLOPS", abs((achieved_tflops(1e12, 0.5) or 0) - 2.0) < 1e-9)

    # KV Cache：以本机 27B 模型参数手算校验
    # 64 层 × 16384 ctx × 4 KV 头 × (256+256) 维 × q4_0(0.5625B) = 1.125 GiB/槽
    kv = kv_cache_bytes(64, 16384, 4, 256, 256, "q4_0", 1)
    check("KV Cache 手算校验 (1.125 GiB)",
          abs(kv["total_gib"] - 1.125) < 0.01, str(kv))
    kv4 = kv_cache_bytes(64, 16384, 4, 256, 256, "q4_0", 4)
    check("KV Cache 4 槽 = 4.5 GiB", abs(kv4["total_gib"] - 4.5) < 0.02, str(kv4))
    kvf16 = kv_cache_bytes(64, 16384, 4, 256, 256, "f16", 1)
    check("f16 是 q4_0 的 3.55 倍",
          abs(kvf16["total_mib"] / kv["total_mib"] - 2 / (18 / 32)) < 0.01,
          f"{kvf16['total_mib']} / {kv['total_mib']}")
    check("KV 参数缺失返回 ok=False", kv_cache_bytes(0, 0, 0, 0)["ok"] is False)

    # 启动参数解析
    args = parse_server_args([
        "llama-server", "-m", "models\\m.gguf", "-ngl", "90", "-fa", "on",
        "-c", "16384", "--cache-type-k", "q4_0", "--cache-type-v", "q4_0",
        "--port", "8080", "-ub=512",
    ])
    check("解析 ctx_size", args.get("ctx_size") == 16384, str(args))
    check("解析 cache_type_k", args.get("cache_type_k") == "q4_0")
    check("解析 n_gpu_layers", args.get("n_gpu_layers") == 90)
    check("解析 flash-attn 无值开关", args.get("flash_attn") == "on")
    check("解析等号写法 -ub=512", args.get("ubatch_size") == 512)
    check("解析 model_path", args.get("model_path") == "models\\m.gguf")

    # GGUF 解析（文件不存在时优雅降级）
    miss = gguf_hyperparams("Z:/not/exist.gguf")
    check("GGUF 缺失文件不抛异常", miss["ok"] is False and "error" in miss)

    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"SELFTEST OK（{len(fails)} 失败）")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(__doc__)
