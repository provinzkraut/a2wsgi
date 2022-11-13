import collections
import os
import sys
import typing

from .types import Environ, Message, Receive, Scope, Send, StartResponse, WSGIApp
import anyio


class Body:
    def __init__(self, receive: Receive) -> None:
        self.buffer = bytearray()
        self.receive = receive
        self._has_more = True

    @property
    def has_more(self) -> bool:
        if self._has_more or self.buffer:
            return True
        return False

    def _receive_more_data(self) -> bytes:
        if not self._has_more:
            return b""
        with anyio.start_blocking_portal() as portal:
            message = portal.call(self.receive)
        self._has_more = message.get("more_body", False)
        return message.get("body", b"")

    def read(self, size: int = -1) -> bytes:
        while size == -1 or size > len(self.buffer):
            self.buffer.extend(self._receive_more_data())
            if not self._has_more:
                break
        if size == -1:
            result = bytes(self.buffer)
            self.buffer.clear()
        else:
            result = bytes(self.buffer[:size])
            del self.buffer[:size]
        return result

    def readline(self, limit: int = -1) -> bytes:
        while True:
            lf_index = self.buffer.find(b"\n", 0, limit if limit > -1 else None)
            if lf_index != -1:
                result = bytes(self.buffer[: lf_index + 1])
                del self.buffer[: lf_index + 1]
                return result
            elif limit != -1:
                result = bytes(self.buffer[:limit])
                del self.buffer[:limit]
                return result
            if not self._has_more:
                break
            self.buffer.extend(self._receive_more_data())

        result = bytes(self.buffer)
        self.buffer.clear()
        return result

    def readlines(self, hint: int = -1) -> typing.List[bytes]:
        if not self.has_more:
            return []
        if hint == -1:
            raw_data = self.read(-1)
            bytelist = raw_data.split(b"\n")
            if raw_data[-1] == 10:  # 10 -> b"\n"
                bytelist.pop(len(bytelist) - 1)
            return [line + b"\n" for line in bytelist]
        return [self.readline() for _ in range(hint)]

    def __iter__(self) -> typing.Generator[bytes, None, None]:
        while self.has_more:
            yield self.readline()


ENC, ESC = sys.getfilesystemencoding(), "surrogateescape"


def unicode_to_wsgi(u):
    """Convert an environment variable to a WSGI "bytes-as-unicode" string"""
    return u.encode(ENC, ESC).decode("iso-8859-1")


def build_environ(scope: Scope, body: Body) -> Environ:
    """
    Builds a scope and request body into a WSGI environ object.
    """
    allow_rewrite_environ = {
        "SCRIPT_NAME": scope.get("root_path", "").encode("utf8").decode("latin1"),
    }
    for key in allow_rewrite_environ.keys():
        environ_var = os.environ.get(key, "")
        if environ_var:
            allow_rewrite_environ[key] = unicode_to_wsgi(environ_var)
    environ = {
        **allow_rewrite_environ,
        "asgi.scope": scope,
        "REQUEST_METHOD": scope["method"],
        "PATH_INFO": scope["path"].encode("utf8").decode("latin1"),
        "QUERY_STRING": scope["query_string"].decode("ascii"),
        "SERVER_PROTOCOL": f"HTTP/{scope['http_version']}",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": body,
        "wsgi.errors": sys.stdout,
        "wsgi.multithread": True,
        "wsgi.multiprocess": True,
        "wsgi.run_once": False,
    }

    # Get server name and port - required in WSGI, not in ASGI
    server = scope.get("server") or ("localhost", 80)
    environ["SERVER_NAME"] = server[0]
    environ["SERVER_PORT"] = server[1]

    # Get client IP address
    if scope.get("client"):
        environ["REMOTE_ADDR"] = scope["client"][0]

    # Go through headers and make them into environ entries
    for name, value in scope.get("headers", []):
        name = name.decode("latin1")
        if name == "content-length":
            corrected_name = "CONTENT_LENGTH"
        elif name == "content-type":
            corrected_name = "CONTENT_TYPE"
        else:
            corrected_name = f"HTTP_{name}".upper().replace("-", "_")
        # HTTPbis say only ASCII chars are allowed in headers, but we latin1 just in case
        value = value.decode("latin1")
        if corrected_name in environ:
            value = environ[corrected_name] + "," + value
        environ[corrected_name] = value
    return environ


class WSGIMiddleware:
    """
    Convert WSGIApp to ASGIApp.
    """

    def __init__(self, app: WSGIApp, workers: int = 10) -> None:
        self.app = app
        self.limiter = anyio.CapacityLimiter(workers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            responder = WSGIResponder(self.app, self.limiter)
            return await responder(scope, receive, send)

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1000})
            return

        if scope["type"] == "lifespan":
            message = await receive()
            assert message["type"] == "lifespan.startup"
            await send({"type": "lifespan.startup.complete"})
            message = await receive()
            assert message["type"] == "lifespan.shutdown"
            await send({"type": "lifespan.shutdown.complete"})
            return


class WSGIResponder:
    def __init__(self, app: WSGIApp, limiter: anyio.CapacityLimiter) -> None:
        self.app = app
        self.send_queue: typing.Deque[typing.Union[Message, None]] = collections.deque()
        self.response_started = False
        self.exc_info: typing.Any = None
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = Body(receive)
        environ = build_environ(scope, body)
        async with anyio.create_task_group() as tg:
            with anyio.CancelScope(shield=True):
                tg.start_soon(self.sender, send)

                await anyio.to_thread.run_sync(
                    self.wsgi, environ, self.start_response, limiter=self.limiter
                )

            self.send(None)

        if self.exc_info is not None:
            raise self.exc_info[0].with_traceback(self.exc_info[1], self.exc_info[2])

    def send(self, message: typing.Optional[Message]) -> None:
        self.send_queue.append(message)

    async def sender(self, send: Send) -> None:
        while True:
            if self.send_queue:
                message = self.send_queue.popleft()
                if message is None:
                    return
                await send(message)
            else:
                await anyio.sleep(0)

    def start_response(
        self,
        status: str,
        response_headers: typing.List[typing.Tuple[str, str]],
        exc_info: typing.Any = None,
    ) -> None:
        self.exc_info = exc_info
        if not self.response_started:
            self.response_started = True
            status_code_string, _ = status.split(" ", 1)
            status_code = int(status_code_string)
            headers = [
                (name.strip().encode("latin1").lower(), value.strip().encode("latin1"))
                for name, value in response_headers
            ]
            self.send(
                {
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": headers,
                }
            )

    def wsgi(self, environ: Environ, start_response: StartResponse) -> None:
        for chunk in self.app(environ, start_response):
            self.send({"type": "http.response.body", "body": chunk, "more_body": True})

        self.send({"type": "http.response.body", "body": b""})
