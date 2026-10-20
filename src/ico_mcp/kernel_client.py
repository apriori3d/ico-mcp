"""Server side of the kernel: start, request, timeout kill, restart.

This module must not import operator code (nor ``ico.registry``): all of it
runs in the kernel process.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from types import TracebackType
from typing import IO, Self

import anyio
from anyio.abc import Process
from anyio.streams.buffered import BufferedByteReceiveStream

from ico_mcp.config import Config
from ico_mcp.protocol import JsonObject, JsonValue, KernelError, decode, encode

BASE_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "VIRTUAL_ENV")
"""Variables always passed to the kernel; others only via ``ico.toml`` ``env``."""


def asset_env(config: Config) -> dict[str, str]:
    """The asset manifest and root, and the resource budgets, for the
    kernel (from ``ico.toml``, not through the ``env`` allowlist)."""
    env: dict[str, str] = {}
    if config.assets_manifest.is_file():
        env["ICO_ASSETS_MANIFEST"] = str(config.assets_manifest)
    root = config.assets.root
    if root is not None:
        env["ICO_ASSETS_ROOT"] = str(root)
    elif "ICO_ASSETS_ROOT" in os.environ:
        env["ICO_ASSETS_ROOT"] = os.environ["ICO_ASSETS_ROOT"]
    if config.resources.ram:
        env["ICO_RESOURCES_RAM"] = config.resources.ram
    if config.resources.gpu:
        env["ICO_RESOURCES_GPU"] = config.resources.gpu
    return env


MAX_LINE = 256 * 1024 * 1024
STDERR_TAIL = 8 * 1024

OnNotification = Callable[[JsonObject], Awaitable[None]]
"""Receives kernel notifications (e.g. ``progress``) while a request runs."""

logger = logging.getLogger(__name__)


class KernelClient:
    """A restartable kernel process; one request at a time.

    The kernel is started on the first request (or by :meth:`start`) and
    after it was killed by a timeout or exited. Use it as an async context
    manager, or call :meth:`aclose`.

    Example:
        >>> async with KernelClient(config) as kernel:
        ...     cards = await kernel.request("search", {"text": "resize"})
    """

    def __init__(self, config: Config, modules: Sequence[str] | None = None) -> None:
        """Args:
        config: The repository configuration.
        modules: Operator modules to load (``domain_draft.slug``); ``None``
            loads both packages entirely. Used again on every restart.
        """
        self.config = config
        self.modules = modules
        self._lock = anyio.Lock()
        self._write_lock = anyio.Lock()
        """Lines to the kernel's stdin: requests and one-way messages."""
        self._process: Process | None = None
        self._stdout: BufferedByteReceiveStream | None = None
        self._stderr: IO[bytes] | None = None
        self._next_id = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> JsonValue:
        """(Re)start the kernel and load the operator packages.

        Returns:
            The load report: ``{"stable": ..., "draft": ...}``.
        """
        async with self._lock:
            await self._stop()
            return await self._spawn()

    async def request(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        on_notification: OnNotification | None = None,
    ) -> JsonValue:
        """Send a request, starting the kernel if needed.

        Notifications the kernel sends before the response are passed to
        ``on_notification``; its errors are logged and ignored, since
        notifications are best-effort.

        Raises:
            KernelError: The error returned by the kernel, ``timeout`` (the
                kernel was killed) or ``crashed`` (it exited).
        """
        async with self._lock:
            if not self.running:
                await self._stop()
                await self._spawn()
            return await self._call(method, params or {}, on_notification)

    async def send(self, method: str, params: JsonObject) -> None:
        """Send a one-way message (no response), also while a request runs;
        the kernel answers it with notifications of that request.

        Raises:
            KernelError: ``not_running`` when the kernel is not running.
        """
        process = self._process
        if process is None or process.stdin is None or not self.running:
            raise KernelError("not_running", "the kernel is not running")
        async with self._write_lock:
            await process.stdin.send(encode({"method": method, "params": params}))

    async def aclose(self) -> None:
        async with self._lock:
            await self._stop()

    def terminate(self) -> None:
        """Ask the kernel to cancel its run (SIGTERM to the kernel only, not
        to its workers, which the kernel stops cleanly)."""
        if self._process is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(self._process.pid, signal.SIGTERM)

    def kill(self) -> None:
        """Kill the kernel and its worker processes now, without waiting for
        the lock: a request in progress fails with ``crashed``."""
        if self._process is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._process.pid, signal.SIGKILL)

    # ─── Internals (called under the lock) ───

    async def _spawn(self) -> JsonValue:
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 (closed in _stop)
        self._process = await anyio.open_process(
            [sys.executable, "-P", "-m", "ico_mcp.kernel", str(self.config.root)],
            stderr=self._stderr,
            cwd=self.config.root,
            env=self._env(),
            # Own process group: a timeout kills worker processes too.
            start_new_session=True,
        )
        assert self._process.stdout is not None
        self._stdout = BufferedByteReceiveStream(self._process.stdout)
        params: JsonObject = {}
        if self.modules is not None:
            params["modules"] = list[JsonValue](self.modules)
        return await self._call("load", params)

    async def _call(
        self,
        method: str,
        params: JsonObject,
        on_notification: OnNotification | None = None,
    ) -> JsonValue:
        process, stdout = self._process, self._stdout
        assert process is not None and process.stdin is not None and stdout is not None
        self._next_id += 1
        request_id = self._next_id
        timeout = self._timeout(method)
        try:
            with anyio.fail_after(timeout):
                async with self._write_lock:
                    await process.stdin.send(
                        encode({"id": request_id, "method": method, "params": params})
                    )
                while True:
                    message = decode(await stdout.receive_until(b"\n", MAX_LINE))
                    if "id" in message:
                        break
                    if on_notification is not None:
                        await _notify(on_notification, message)
        except TimeoutError:
            await self._stop()
            raise KernelError(
                "timeout",
                f"'{method}' timed out after {timeout:g} s; the kernel was "
                "killed and restarts on the next request",
                {"timeout": timeout},
            ) from None
        except (anyio.EndOfStream, anyio.IncompleteRead, anyio.BrokenResourceError):
            raise await self._crashed(method) from None
        except KernelError as e:  # not a JSON line: the protocol is broken
            await self._stop()
            raise KernelError("internal", f"invalid kernel output: {e}") from None

        if message.get("id") != request_id:
            await self._stop()
            raise KernelError("internal", f"unexpected kernel response: {message!r}")
        error = message.get("error")
        if isinstance(error, dict):
            raise KernelError.from_dict(error)
        return message.get("result")

    async def _crashed(self, method: str) -> KernelError:
        process = self._process
        returncode: int | None = None
        if process is not None:
            with anyio.move_on_after(5):
                returncode = await process.wait()
        stderr = self._stderr_tail()
        await self._stop()
        return KernelError(
            "crashed",
            f"the kernel exited (code {returncode}) during '{method}'; it "
            "restarts on the next request",
            {"returncode": returncode, "stderr": stderr},
        )

    async def _stop(self) -> None:
        process, self._process, self._stdout = self._process, None, None
        if process is not None:
            # The whole group: worker processes may outlive a crashed kernel.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with anyio.CancelScope(shield=True):
                await process.aclose()
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None

    def _stderr_tail(self) -> str:
        if self._stderr is None:
            return ""
        self._stderr.seek(0, os.SEEK_END)
        size = self._stderr.tell()
        self._stderr.seek(max(size - STDERR_TAIL, 0))
        return self._stderr.read().decode(errors="replace")

    def _timeout(self, method: str) -> float:
        timeouts = self.config.timeouts
        if method == "run":
            return timeouts.run
        if method == "load":
            return timeouts.load
        return timeouts.validate

    def _env(self) -> dict[str, str]:
        names = (*BASE_ENV, *self.config.env)
        env = {name: os.environ[name] for name in names if name in os.environ}
        return {**env, **asset_env(self.config)}


async def _notify(on_notification: OnNotification, message: JsonObject) -> None:
    try:
        await on_notification(message)
    except Exception:
        logger.exception("kernel notification handler failed")
