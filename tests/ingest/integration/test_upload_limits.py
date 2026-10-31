"""The global request-body cap (2 MiB) must not apply to raw uploads: they have their own limit and error code."""
import asyncio

from engine.api.main import _BodyLimit


def _call(mw, path: str, size: int):
    status = {}

    async def app(scope, receive, send):
        n = 0
        while True:
            m = await receive()
            n += len(m.get("body", b""))
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(n).encode()})

    async def run():
        sent = []
        body = {"done": False}

        async def receive():
            if body["done"]:
                return {"type": "http.disconnect"}
            body["done"] = True
            return {"type": "http.request", "body": b"x" * size, "more_body": False}

        async def send(m):
            sent.append(m)
        await _BodyLimit(app, 1024)(
            {"type": "http", "path": path, "headers": [(b"content-length", str(size).encode())]}, receive, send)
        status["s"] = sent[0]["status"]
    asyncio.run(run())
    return status["s"]


def test_upload_path_uses_upload_limit(monkeypatch):
    monkeypatch.setenv("ISNAD_MAX_UPLOAD_BYTES", str(64 * 1024))
    assert _call(None, "/objects/X", 4096) == 413            # global cap (1 KiB here)
    assert _call(None, "/ingest/uploads", 4096) == 200       # under the upload limit
    assert _call(None, "/ingest/uploads", 64 * 1024 + 1024 * 1024 + 1) == 413
