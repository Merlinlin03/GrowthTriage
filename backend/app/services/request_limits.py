"""Bounded single-process write protection, including chunked request bodies."""
import time
import uuid
from collections import deque

from starlette.responses import JSONResponse


class RequestLimits:
    def __init__(self, app, max_bytes=3_100_000, writes_per_minute=120):
        self.app = app
        self.max_bytes = max_bytes
        self.writes_per_minute = writes_per_minute
        self.writes = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"} or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        now = time.monotonic()
        while self.writes and self.writes[0] <= now - 60:
            self.writes.popleft()
        if len(self.writes) >= self.writes_per_minute:
            return await self.reject(scope, receive, send, 429, "WRITE_RATE_LIMIT", {"Retry-After": "60"})
        self.writes.append(now)
        headers = dict(scope["headers"])
        try:
            size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            size = -1
        if size < 0 or size > self.max_bytes:
            return await self.reject(scope, receive, send, 413, "REQUEST_TOO_LARGE")
        # Read at most the accepted limit before multipart parsing can spool to disk.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                return await self.reject(scope, receive, send, 413, "REQUEST_TOO_LARGE")
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def reject(scope, receive, send, status, code, headers=None):
        response = JSONResponse({"error": {"code": code, "message": "请求超过本机服务限制，请稍后重试或缩小文件。", "field_errors": []}, "request_id": f"req_{uuid.uuid4().hex}"},
                                status_code=status, headers=headers)
        await response(scope, receive, send)
