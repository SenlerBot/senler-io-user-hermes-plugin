"""Keep each background execution bound to its own durable processing attempt."""
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
from threading import Event
import time

from .senler_relay_store import PLATFORM

_attempt = ContextVar("senler_processing_attempt", default=None)
MCP_PREFIX = "mcp__senler_io_user__"
SEND_METHODS = {"DialogsMessaging_sendMessage", "DialogsMessagingController_sendMessage"}


@dataclass(frozen=True)
class ProcessingAttempt:
    adapter: object
    subscription: str
    event_id: str
    attempt_id: str
    deadline: float
    _completion: Future = field(default_factory=Future, init=False, repr=False, compare=False)
    _closed: Event = field(default_factory=Event, init=False, repr=False, compare=False)

    def close(self):
        self._closed.set()
        self._completion.cancel()

    def require_open(self):
        if self._closed.is_set():
            raise ValueError("The Senler processing attempt is no longer active")

    def require_active(self):
        self.require_open()
        if self.subscription not in self.adapter.allowed_subscriptions() or time.time() >= self.deadline:
            raise ValueError("The Senler subscription is unavailable or the processing deadline was reached")
        self.adapter.store.require_processing(self.subscription, self.event_id, self.attempt_id)
        self.require_open()  # Cancellation can happen while a worker waits on SQLite.

    def completion_error(self):
        # Seal the observation without waiting: Hermes has already ended delivery.
        # A skipped/timed-out observer must not become a late success for this run.
        self._completion.cancel()
        if self._completion.cancelled():
            return "Hermes completion was not confirmed; review prior actions before retrying"
        return self._completion.result()


@contextmanager
def processing_attempt(adapter, row):
    # Hermes carries contextvars through gateway tasks, model/tool workers and
    # hook callbacks. A late worker retains its own attempt, never the next one.
    run = ProcessingAttempt(adapter, row["subscription"], row["event_id"], row["attempt_id"],
                            min(time.time() + 600, row["expires"]))
    token = _attempt.set(run)
    try:
        yield run
    finally:
        run.close()
        _attempt.reset(token)


def require_event(adapter, event):
    run = _attempt.get()
    if (run is None or run.adapter is not adapter or event is not adapter.current
            or event.message_id != run.event_id
            or event.source.chat_id != run.subscription + ":" + run.event_id):
        raise ValueError("Senler events must start from the durable inbox")
    run.require_active()


def save_report(adapter, chat_id, content):
    run = _attempt.get()
    if run is None or run.adapter is not adapter or chat_id != run.subscription + ":" + run.event_id:
        raise ValueError("No active Senler processing attempt for this report")
    # Reports use the same immutable binding as tools, even if a stale worker's
    # event was recreated. The database serializes each append with cancellation.
    run.require_open()
    run.adapter.store.append_report(run.subscription, run.event_id, run.attempt_id, content)
    return run.event_id


def on_session_end(completed=None, failed=None, interrupted=None, platform=None, **kwargs):
    run = _attempt.get()
    if run is None or platform != PLATFORM:
        return
    # Missing/legacy fields are not evidence of success. Never infer an outcome
    # from the model's text or expose raw provider diagnostics from hook kwargs.
    if not all(type(value) is bool for value in (completed, failed, interrupted)):
        return
    error = None
    if interrupted:
        error = "Hermes processing was interrupted; review prior actions before retrying"
    elif failed:
        error = "Hermes processing failed; review prior actions before retrying"
    elif not completed:
        error = "Hermes processing did not complete; review prior actions before retrying"
    try:
        # The native observer may run in a worker thread. Future provides an
        # atomic, single-use handoff to the exact attempt captured in its context.
        run._completion.set_result(error)
    except InvalidStateError:
        pass  # The attempt ended, or its canonical completion was already seen.


def before_tool(tool_name, args, **kwargs):
    run = _attempt.get()
    if run is None:
        return
    block = lambda message: {"action": "block", "message": message}
    try:
        run.require_active()
    except ValueError as error:
        return block(str(error))
    if tool_name in {"tool_search", "tool_describe"}:
        # These read Hermes's session-scoped catalogue. tool_call itself is NOT
        # allowed: the native executor must unwrap and check each real MCP call.
        return
    if not tool_name.startswith(MCP_PREFIX):
        return block("Use the connected Senler MCP for this background event")
    method = args.get("method_name") if tool_name == MCP_PREFIX + "execute" else tool_name[len(MCP_PREFIX):]
    if method not in SEND_METHODS:
        return
    wrapped = tool_name == MCP_PREFIX + "execute"
    parameters = args.get("parameters") if wrapped else args
    try:
        prepared = run.adapter.store.prepare_reply(run.subscription, run.event_id, run.attempt_id, parameters)
        run.require_open()
    except ValueError as error:
        return block(str(error))
    return {"action": "modify", "args": {"parameters": prepared} if wrapped else prepared}
