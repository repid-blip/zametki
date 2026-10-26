"""Заглушка. В Android-версии режим HTTP/2 для медиа через Cloudflare отключён
(он требует httpx/h2). Остальные способы подключения работают как обычно."""


class CfH2Pool:
    def __init__(self, *a, **k):
        raise RuntimeError("HTTP/2-режим недоступен в Android-версии")


async def bridge_h2(*a, **k):
    raise RuntimeError("HTTP/2-режим недоступен в Android-версии")
