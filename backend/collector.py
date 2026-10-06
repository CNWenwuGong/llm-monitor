"""硬件与推理指标采集。

三层数据来源
------------
1. **GPU**：优先走 NVML（pynvml），支持多卡聚合；无 NVIDIA 设备时自动降级为
   "模拟 GPU"（可通过配置关闭），保证在无卡机器上界面依然可完整演示。
2. **系统**：psutil 采集 CPU / RAM / 推理进程 RSS。
3. **推理运行时**：后端自维护的请求计数器（活跃请求、RPS、TPS、TTFT）。

采集协程 ``collector_loop`` 每 ``SAMPLE_INTERVAL`` 秒采样一次，
写库后通过 WebSocket 广播给前端。
"""

from __future__ import annotations

import asyncio
import logging
import math
import platform
import random
import time
from collections import deque
from typing import Any, Dict, List, Optional

import psutil

from . import config, db
from .ws import broadcaster

log = logging.getLogger("llm-monitor.collector")

# --------------------------------------------------------------------------
# 推理进程匹配
# --------------------------------------------------------------------------
_INFERENCE_PROC_HINTS = ("ollama", "vllm", "llama-server", "llama_cpp", "text-generation")
_proc: Optional[psutil.Process] = None


def _find_inference_process() -> Optional[psutil.Process]:
    """按进程名匹配本机推理服务进程（Ollama / vLLM / llama.cpp）。"""
    global _proc
    if _proc is not None:
        try:
            if _proc.is_running():
                return _proc
        except Exception:  # noqa: BLE001
            pass
        _proc = None
    try:
        for p in psutil.process_iter(["name", "cmdline"]):
            name = (p.info.get("name") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            if any(h in name or h in cmd for h in _INFERENCE_PROC_HINTS):
                _proc = p
                return p
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------
# GPU 读取
# --------------------------------------------------------------------------
class GPUReader:
    """NVML 封装；无设备时降级为模拟数据源。"""

    def __init__(self, simulate_when_missing: bool = True) -> None:
        self.handles: List[Any] = []
        self.names: List[str] = []
        self.available = False
        self.simulated = False
        self.nvml = None
        self._init_nvml(simulate_when_missing)

    def _init_nvml(self, simulate_when_missing: bool) -> None:
        try:
            import pynvml  # type: ignore

            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                raw = pynvml.nvmlDeviceGetName(h)
                self.names.append(raw.decode() if isinstance(raw, bytes) else str(raw))
                self.handles.append(h)
            self.nvml = pynvml
            self.available = count > 0
            if self.available:
                log.info("NVML 初始化成功，检测到 %d 张 GPU: %s", count, ", ".join(self.names))
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("NVML 不可用（%s），GPU 指标降级。", exc)

        if simulate_when_missing:
            self.simulated = True
            self.names = ["Simulated GPU (未检测到 NVIDIA 设备)"]
            log.warning("未检测到 NVIDIA GPU，已启用模拟显存/利用率数据源。")

    # ---- 静态信息 ----
    def info(self) -> Dict[str, Any]:
        if self.available and self.nvml:
            total_mb = 0.0
            for h in self.handles:
                try:
                    total_mb += self.nvml.nvmlDeviceGetMemoryInfo(h).total / 1048576
                except Exception:  # noqa: BLE001
                    pass
            return {
                "gpu_available": True,
                "simulated": False,
                "gpu_count": len(self.handles),
                "gpu_names": self.names,
                "vram_total_mb": round(total_mb, 1),
            }
        return {
            "gpu_available": False,
            "simulated": self.simulated,
            "gpu_count": 0,
            "gpu_names": self.names,
            "vram_total_mb": config.DEFAULT_GPU_TOTAL_MB if self.simulated else 0.0,
        }

    # ---- 动态采样 ----
    def sample(self, stats: "RuntimeStats") -> Dict[str, Any]:
        if self.available and self.nvml:
            util = 0.0
            used = 0.0
            total = 0.0
            temp = 0.0
            for h in self.handles:
                try:
                    util = max(util, float(self.nvml.nvmlDeviceGetUtilizationRates(h).gpu))
                    mem = self.nvml.nvmlDeviceGetMemoryInfo(h)
                    used += mem.used / 1048576
                    total += mem.total / 1048576
                    try:
                        t = self.nvml.nvmlDeviceGetTemperature(h, self.nvml.NVML_TEMPERATURE_GPU)
                        temp = max(temp, float(t))
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    continue
            return {
                "gpu_util": round(util, 1),
                "vram_used_mb": round(used, 1),
                "vram_total_mb": round(total, 1),
                "gpu_temp": round(temp, 1),
                "gpu_name": self.names[0] if self.names else "",
                "simulated": 0,
            }
        if self.simulated:
            return self._simulate(stats)
        return {
            "gpu_util": None, "vram_used_mb": None, "vram_total_mb": None,
            "gpu_temp": None, "gpu_name": "", "simulated": 0,
        }

    def _simulate(self, stats: "RuntimeStats") -> Dict[str, Any]:
        """基于当前推理负载合成一条可解释的 GPU 曲线。

        空闲时维持低位波动；有活跃请求时利用率/显存/温度随负载上升，
        负载按 10 秒半衰期衰减，从而形成"压测时曲线抬高、结束后回落"的形态。
        """
        load = stats.load_factor()
        total = config.DEFAULT_GPU_TOTAL_MB
        t = time.time()
        wave = math.sin(t / 7.0) * 2.5 + math.sin(t / 2.3) * 1.2
        util = 4.0 + load * 88.0 + wave + random.uniform(-2.5, 2.5)
        util = max(0.0, min(100.0, util))
        base_vram = total * 0.34  # 常驻模型权重占用
        vram = base_vram + load * total * 0.44 + random.uniform(-40, 40)
        vram = max(base_vram * 0.95, min(total * 0.985, vram))
        temp = 40.0 + load * 28.0 + wave * 0.6 + random.uniform(-1.2, 1.2)
        return {
            "gpu_util": round(util, 1),
            "vram_used_mb": round(vram, 1),
            "vram_total_mb": round(total, 1),
            "gpu_temp": round(temp, 1),
            "gpu_name": self.names[0] if self.names else "Simulated GPU",
            "simulated": 1,
        }


# --------------------------------------------------------------------------
# 推理运行时统计
# --------------------------------------------------------------------------
class RuntimeStats:
    """后端自维护的请求计数器，供看板计算 RPS / TPS / 活跃请求数。"""

    def __init__(self) -> None:
        self.active_reqs = 0
        self.total_requests = 0
        self.error_requests = 0
        self.total_tokens = 0
        self.last_ttft_ms = 0.0
        self.last_tps = 0.0
        self.last_e2e_ms = 0.0
        self._done_ts: deque = deque()
        self._tps_ema = 0.0
        self._ttft_ema = 0.0
        self._load_ema = 0.0
        self._last_tick = time.time()

    # -- 请求生命周期 --
    def request_started(self) -> None:
        self.active_reqs += 1

    def request_finished(self, ttft_ms: float = 0.0, tps: float = 0.0,
                         tokens: int = 0, ok: bool = True, e2e_ms: float = 0.0) -> None:
        self.active_reqs = max(0, self.active_reqs - 1)
        self.total_requests += 1
        if not ok:
            self.error_requests += 1
        else:
            self.total_tokens += int(tokens or 0)
        if ttft_ms and ttft_ms > 0:
            self.last_ttft_ms = ttft_ms
            self._ttft_ema = ttft_ms if self._ttft_ema == 0 else \
                self._ttft_ema * (1 - config.TPS_EMA_ALPHA) + ttft_ms * config.TPS_EMA_ALPHA
        if tps and tps > 0:
            self.last_tps = tps
            self._tps_ema = tps if self._tps_ema == 0 else \
                self._tps_ema * (1 - config.TPS_EMA_ALPHA) + tps * config.TPS_EMA_ALPHA
        if e2e_ms:
            self.last_e2e_ms = e2e_ms
        self._done_ts.append(time.time())
        self._trim()

    def _trim(self) -> None:
        cutoff = time.time() - config.RPS_WINDOW
        while self._done_ts and self._done_ts[0] < cutoff:
            self._done_ts.popleft()

    def rps(self) -> float:
        self._trim()
        return round(len(self._done_ts) / config.RPS_WINDOW, 3)

    def load_factor(self) -> float:
        """0~1 的负载强度：活跃请求占主导，完成后按时间衰减。"""
        now = time.time()
        dt = max(0.001, now - self._last_tick)
        self._last_tick = now
        instant = min(1.0, self.active_reqs / 6.0)
        if not self._done_ts:
            decay_target = 0.0
        else:
            age = now - self._done_ts[-1]
            decay_target = max(0.0, 1.0 - age / 12.0) * 0.55
        target = max(instant, decay_target)
        alpha = min(0.9, dt / 3.0)
        self._load_ema += (target - self._load_ema) * alpha
        return max(0.0, min(1.0, self._load_ema))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "active_reqs": self.active_reqs,
            "rps": self.rps(),
            "current_tps": round(self._tps_ema, 2) if self._tps_ema else 0.0,
            "current_ttft_ms": round(self._ttft_ema, 1) if self._ttft_ema else 0.0,
            "last_e2e_ms": round(self.last_e2e_ms, 1),
            "total_requests": self.total_requests,
            "error_requests": self.error_requests,
            "total_tokens": self.total_tokens,
        }


stats = RuntimeStats()
gpu = GPUReader()
_boot_ts = time.time()
# 最近一次采样结果（内存快照），供压测引擎读取峰值资源而不重复触发采样
last_sample: Dict[str, Any] = {}


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def sample_once() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    s: Dict[str, Any] = {
        "ts": time.time(),
        "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
        "ram_percent": round(vm.percent, 1),
        "ram_used_mb": round((vm.total - vm.available) / 1048576, 1),
        "proc_rss_mb": None,
    }
    p = _find_inference_process()
    if p is not None:
        try:
            s["proc_rss_mb"] = round(p.memory_info().rss / 1048576, 1)
        except Exception:  # noqa: BLE001
            pass
    s.update(gpu.sample(stats))
    s.update(stats.snapshot())
    return s


async def collector_loop(broadcaster_ref=broadcaster) -> None:
    """后台采样协程：采样 → 写库 → 广播。"""
    psutil.cpu_percent(interval=None)  # 预热，首次调用才有意义
    log.info("指标采集协程启动，间隔 %.1fs（GPU: %s）", config.SAMPLE_INTERVAL,
             "NVML" if gpu.available else ("模拟" if gpu.simulated else "不可用"))
    last_prune = 0.0
    while True:
        try:
            s = sample_once()
            last_sample.clear()
            last_sample.update(s)
            db.insert_sample(s)
            await broadcaster_ref.broadcast({"type": "metrics", "data": s})
            now = time.time()
            if now - last_prune > 3600:
                last_prune = now
                removed = db.prune_samples(config.SAMPLE_RETENTION_DAYS)
                if removed:
                    log.info("清理过期采样 %d 条", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("采集循环异常: %s", exc)
        await asyncio.sleep(config.SAMPLE_INTERVAL)


# --------------------------------------------------------------------------
# 静态系统信息
# --------------------------------------------------------------------------
def system_info() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    freq = None
    try:
        f = psutil.cpu_freq()
        freq = round(f.max / 1000, 2) if f and f.max else None
    except Exception:  # noqa: BLE001
        pass
    info = {
        "gpu": gpu.info(),
        "cpu": {
            "name": platform.processor() or platform.machine(),
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": psutil.cpu_count(logical=True),
            "freq_ghz": freq,
            "usage_percent": psutil.cpu_percent(interval=None),
        },
        "ram": {
            "total_mb": round(vm.total / 1048576, 1),
            "used_mb": round((vm.total - vm.available) / 1048576, 1),
            "percent": round(vm.percent, 1),
        },
        "os": {
            "platform": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "arch": platform.machine(),
            "hostname": platform.node(),
        },
        "uptime_sec": round(time.time() - _boot_ts, 1),
        "stats": stats.snapshot(),
    }
    return info


def env_snapshot() -> Dict[str, Any]:
    """测试运行时写入 test_runs.env_json 的环境快照。"""
    gi = gpu.info()
    vm = psutil.virtual_memory()
    return {
        "gpu_available": gi["gpu_available"],
        "gpu_simulated": gi["simulated"],
        "gpu_names": gi["gpu_names"],
        "vram_total_mb": gi["vram_total_mb"],
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_total_mb": round(vm.total / 1048576, 1),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "captured_at": time.time(),
    }
