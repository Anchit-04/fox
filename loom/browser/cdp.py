import itertools
import json
import queue
import threading
import time
from typing import Callable

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

_CLOSED = object()


class CDPError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message if code is None else f"{message} (code {code})")
        self.message = message
        self.code = code


class CDPTimeout(CDPError):
    pass


class CDPClosed(CDPError):
    pass


class CDPAborted(CDPError):
    pass


class JavaScriptError(CDPError):
    pass


class NavigationFailed(CDPError):
    pass


class EventWaiter:
    def __init__(self, conn: "CDPConnection", method: str, session_id: str | None):
        self._conn = conn
        self.method = method
        self.session_id = session_id
        self._queue: queue.Queue = queue.Queue()

    def matches(self, method: str | None, session_id: str | None) -> bool:
        return method == self.method and session_id == self.session_id

    def deliver(self, item) -> None:
        self._queue.put(item)

    def wait(self, timeout: float = 30.0, where: Callable[[dict], bool] | None = None) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPTimeout(f"No {self.method} within {timeout:g}s")
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise CDPTimeout(f"No {self.method} within {timeout:g}s") from None
            if item is _CLOSED:
                raise CDPClosed(f"The browser connection closed while waiting for {self.method}")
            if where is None or where(item):
                return item

    def cancel(self) -> None:
        self._conn._remove_waiter(self)

    def __enter__(self) -> "EventWaiter":
        return self

    def __exit__(self, *exc) -> None:
        self.cancel()


class CDPConnection:
    def __init__(self, ws_url: str, open_timeout: float = 10.0):
        try:
            self._ws = connect(
                ws_url,
                max_size=None,
                compression=None,
                ping_interval=None,
                open_timeout=open_timeout,
            )
        except (OSError, TimeoutError, WebSocketException) as e:
            raise CDPClosed(f"Could not connect to the browser at {ws_url}: {e}") from e
        self._ids = itertools.count(1)
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._waiters: list[EventWaiter] = []
        self._listeners: list[tuple[str, str | None, Callable[[dict], None]]] = []
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()
        self._headless: bool | None = None

    @property
    def headless(self) -> bool:
        if self._headless is None:
            self._headless = "Headless" in self.send("Browser.getVersion", timeout=10).get("userAgent", "")
        return self._headless

    @property
    def closed(self) -> bool:
        return self._closed

    def send(self, method: str, params: dict | None = None, session_id: str | None = None,
             timeout: float = 30.0, abort_if: Callable[[], bool] | None = None) -> dict:
        reply: queue.Queue = queue.Queue(maxsize=1)
        message_id = self._write(method, params, session_id, reply)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._forget(message_id)
                raise CDPTimeout(f"{method} got no reply within {timeout:g}s")
            try:
                item = reply.get(timeout=min(remaining, 0.05) if abort_if else remaining)
                break
            except queue.Empty:
                if abort_if is not None and abort_if():
                    self._forget(message_id)
                    raise CDPAborted(f"Stopped waiting for {method}") from None
        if item is _CLOSED:
            raise CDPClosed(f"The browser connection closed before {method} replied")
        if "error" in item:
            error = item["error"]
            raise CDPError(f"{method}: {error.get('message', 'unknown error')}", error.get("code"))
        return item.get("result", {})

    def post(self, method: str, params: dict | None = None, session_id: str | None = None) -> None:
        self._write(method, params, session_id, None)

    def on(self, method: str, callback: Callable[[dict], None],
           session_id: str | None = None) -> Callable[[], None]:
        entry = (method, session_id, callback)
        with self._state_lock:
            self._listeners.append(entry)

        def unsubscribe() -> None:
            with self._state_lock:
                if entry in self._listeners:
                    self._listeners.remove(entry)

        return unsubscribe

    def _write(self, method: str, params: dict | None, session_id: str | None,
               reply: queue.Queue | None) -> int:
        with self._state_lock:
            if self._closed:
                raise CDPClosed(f"The browser connection is closed; cannot send {method}")
            message_id = next(self._ids)
            if reply is not None:
                self._pending[message_id] = reply
        message = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        try:
            with self._send_lock:
                self._ws.send(json.dumps(message))
        except (ConnectionClosed, OSError) as e:
            self._forget(message_id)
            raise CDPClosed(f"The browser connection closed while sending {method}") from e
        return message_id

    def expect(self, method: str, session_id: str | None = None) -> EventWaiter:
        waiter = EventWaiter(self, method, session_id)
        with self._state_lock:
            if self._closed:
                waiter.deliver(_CLOSED)
            else:
                self._waiters.append(waiter)
        return waiter

    def close(self) -> None:
        try:
            self._ws.close()
        except (OSError, WebSocketException):
            pass
        self._reader.join(timeout=5)
        self._shut()

    def __enter__(self) -> "CDPConnection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _forget(self, message_id: int) -> None:
        with self._state_lock:
            self._pending.pop(message_id, None)

    def _remove_waiter(self, waiter: EventWaiter) -> None:
        with self._state_lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _read_loop(self) -> None:
        try:
            for raw in self._ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                self._dispatch(message)
        except (ConnectionClosed, OSError):
            pass
        finally:
            self._shut()

    def _dispatch(self, message: dict) -> None:
        if "id" in message:
            with self._state_lock:
                reply = self._pending.pop(message["id"], None)
            if reply is not None:
                reply.put(message)
            return
        method = message.get("method")
        session_id = message.get("sessionId")
        params = message.get("params", {})
        with self._state_lock:
            waiters = [w for w in self._waiters if w.matches(method, session_id)]
            listeners = [cb for m, sid, cb in self._listeners if m == method and sid == session_id]
        for waiter in waiters:
            waiter.deliver(params)
        for callback in listeners:
            try:
                callback(params)
            except Exception:
                pass

    def _shut(self) -> None:
        with self._state_lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
            waiters = list(self._waiters)
            self._waiters.clear()
            self._listeners.clear()
        for reply in pending:
            reply.put(_CLOSED)
        for waiter in waiters:
            waiter.deliver(_CLOSED)


def _raise_for_exception(result: dict) -> None:
    if "exceptionDetails" in result:
        details = result["exceptionDetails"]
        text = details.get("exception", {}).get("description") or details.get("text") or "JavaScript error"
        raise JavaScriptError(text)


WINDOW_BOUNDS = {"left": 60, "top": 40, "width": 1280, "height": 860}


class Page:
    def __init__(self, conn: CDPConnection):
        self.conn = conn
        self.target_id: str | None = None
        self.session_id: str | None = None

    def open(self, timeout: float = 30.0, visible: bool = False) -> "Page":
        params = {"url": "about:blank", "newWindow": True, **WINDOW_BOUNDS}
        if not visible and not self.conn.headless:
            params["windowState"] = "minimized"
        created = self.conn.send("Target.createTarget", params, timeout=timeout)
        self.target_id = created["targetId"]
        try:
            attached = self.conn.send(
                "Target.attachToTarget", {"targetId": self.target_id, "flatten": True}, timeout=timeout
            )
            self.session_id = attached["sessionId"]
            self.call("Page.enable", timeout=timeout)
            self.call("Page.setLifecycleEventsEnabled", {"enabled": True}, timeout=timeout)
            self.call("Runtime.enable", timeout=timeout)
        except CDPError:
            self.close()
            raise
        return self

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0,
             abort_if: Callable[[], bool] | None = None) -> dict:
        return self.conn.send(method, params, session_id=self.session_id, timeout=timeout, abort_if=abort_if)

    def post(self, method: str, params: dict | None = None) -> None:
        self.conn.post(method, params, session_id=self.session_id)

    def on(self, method: str, callback: Callable[[dict], None]) -> Callable[[], None]:
        return self.conn.on(method, callback, self.session_id)

    def window_id(self) -> int:
        return self.conn.send("Browser.getWindowForTarget", {"targetId": self.target_id})["windowId"]

    def show(self) -> None:
        window = self.window_id()
        self.conn.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "normal"}})
        self.call("Page.bringToFront")
        self.conn.send("Target.activateTarget", {"targetId": self.target_id})

    def hide(self) -> None:
        if self.conn.headless:
            return
        window = self.window_id()
        self.conn.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "minimized"}})

    def expect(self, method: str) -> EventWaiter:
        return self.conn.expect(method, self.session_id)

    def navigate(self, url: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        with self.expect("Page.lifecycleEvent") as lifecycle:
            result = self.call("Page.navigate", {"url": url}, timeout=timeout)
            if result.get("errorText"):
                raise NavigationFailed(f"Could not open {url}: {result['errorText']}")
            loader_id = result.get("loaderId")
            if not loader_id:
                return
            lifecycle.wait(
                max(deadline - time.monotonic(), 0.01),
                where=lambda event: event.get("name") == "load" and event.get("loaderId") == loader_id,
            )

    def main_frame_id(self, timeout: float = 30.0) -> str:
        return self.call("Page.getFrameTree", timeout=timeout)["frameTree"]["frame"]["id"]

    def create_world(self, name: str = "fox", timeout: float = 30.0) -> int:
        created = self.call(
            "Page.createIsolatedWorld",
            {"frameId": self.main_frame_id(timeout), "worldName": name},
            timeout=timeout,
        )
        return created["executionContextId"]

    def evaluate(self, expression: str, timeout: float = 30.0, context_id: int | None = None):
        params = {"expression": expression, "returnByValue": True, "awaitPromise": True}
        if context_id is not None:
            params["contextId"] = context_id
        result = self.call("Runtime.evaluate", params, timeout=timeout)
        _raise_for_exception(result)
        return result.get("result", {}).get("value")

    def evaluate_handle(self, expression: str, context_id: int | None = None, timeout: float = 30.0) -> str | None:
        params = {"expression": expression, "returnByValue": False}
        if context_id is not None:
            params["contextId"] = context_id
        result = self.call("Runtime.evaluate", params, timeout=timeout)
        _raise_for_exception(result)
        return result.get("result", {}).get("objectId")

    def call_on(self, object_id: str, function: str, *args, timeout: float = 30.0,
                abort_if: Callable[[], bool] | None = None):
        result = self.call(
            "Runtime.callFunctionOn",
            {"objectId": object_id, "functionDeclaration": function,
             "arguments": [{"value": a} for a in args], "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
            abort_if=abort_if,
        )
        _raise_for_exception(result)
        return result.get("result", {}).get("value")

    def release(self, object_id: str) -> None:
        try:
            self.post("Runtime.releaseObject", {"objectId": object_id})
        except CDPError:
            pass

    def close(self) -> None:
        target = self.target_id
        self.target_id = None
        self.session_id = None
        if target is None or self.conn.closed:
            return
        try:
            self.conn.send("Target.closeTarget", {"targetId": target}, timeout=5)
        except CDPError:
            pass

    def __enter__(self) -> "Page":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
