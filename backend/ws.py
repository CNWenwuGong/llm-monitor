"""WebSocket 连接管理与广播。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket

log = logging.getLogger("llm-monitor.ws")


class Broadcaster:
    """维护所有活跃 WebSocket 连接，支持一次性广播与单播。

    广播失败（客户端已断开）时自动剔除，避免连接泄漏。
    """

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        return len(self._clients)

    async def register(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def send_to(self, ws: WebSocket, msg: Dict[str, Any]) -> None:
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001 - 客户端断开时静默忽略
            await self.unregister(ws)

    async def broadcast(self, msg: Dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = json.dumps(msg, ensure_ascii=False, default=str)
        async with self._lock:
            targets = list(self._clients)
        dead = []
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


broadcaster = Broadcaster()
