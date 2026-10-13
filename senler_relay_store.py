"""Bounded durable inbox and subscription settings; no OAuth tokens are read here."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .senler_notifications import NotificationOutbox, MAX_NOTIFICATIONS, validate_target

PLATFORM = "senler-io"
MAX_PENDING = 1000
MAX_BYTES = 131072
MAX_REPORT_CHARS = 16000
RETENTION = 7 * 86400
UNSET = object()
CONNECTION_FIELDS = {"webhook_id", "project_id", "relay_url", "gateway_id", "gateway_secret", "events", "revision"}


def connection_identity(config):
    return tuple(config[key] for key in ("webhook_id", "project_id", "relay_url", "gateway_id", "gateway_secret"))


def validate_connection(data):
    if not isinstance(data, dict) or set(data) != CONNECTION_FIELDS:
        raise ValueError("Expected the complete Hermes connection response from Senler MCP")
    if not isinstance(data["webhook_id"], str) or not re.fullmatch(r"[a-f0-9]{24}", data["webhook_id"]):
        raise ValueError("Invalid webhook ID")
    UUID(data["project_id"])
    url = urlsplit(data["relay_url"])
    local = url.scheme == "ws" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (url.scheme != "wss" and not local) or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Relay requires WSS (WS is allowed only on localhost for development)")
    if url.path != "/api/hermes-relay/" + data["webhook_id"] or data["gateway_id"] != data["webhook_id"]:
        raise ValueError("Relay address does not match the subscription")
    if not isinstance(data["gateway_secret"], str) or not 32 <= len(data["gateway_secret"]) <= 512:
        raise ValueError("Invalid relay credential")
    if (not isinstance(data["events"], list) or not 1 <= len(data["events"]) <= 100
            or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", item) for item in data["events"])
            or len(set(data["events"])) != len(data["events"])):
        raise ValueError("Invalid subscribed event types")
    if type(data["revision"]) is not int or data["revision"] < 0:
        raise ValueError("Invalid subscription revision")
    return dict(data)


class RelayStore:
    def __init__(self, home):
        self.path = Path(home) / "senler-io-relay.sqlite3"
        # SQLite's process lock coordinates the helper with the running gateway.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY, config TEXT NOT NULL, enabled INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS inbox (
                    subscription TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
                    revision INTEGER NOT NULL, state TEXT NOT NULL, result TEXT, reply_json TEXT, attempt_id TEXT,
                    failed INTEGER NOT NULL DEFAULT 0, reported INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY (subscription, event_id));
                CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(state, created);
                CREATE INDEX IF NOT EXISTS inbox_subscription_reporting ON inbox(subscription, state, reported, updated, event_id);
                CREATE INDEX IF NOT EXISTS inbox_expiry ON inbox(state, updated);
                CREATE INDEX IF NOT EXISTS inbox_subscription_result ON inbox(subscription,state,updated);
                CREATE TABLE IF NOT EXISTS relay_health (
                    subscription TEXT PRIMARY KEY, active INTEGER NOT NULL, revision INTEGER NOT NULL, observed REAL NOT NULL);
            """)
            NotificationOutbox.install(db)
            db.execute("BEGIN IMMEDIATE")
            if "attempt_id" not in {row["name"] for row in db.execute("PRAGMA table_info(inbox)")}:
                db.execute("ALTER TABLE inbox ADD COLUMN attempt_id TEXT")
                db.execute("UPDATE inbox SET attempt_id=lower(hex(randomblob(16)))")
            db.execute("DROP INDEX IF EXISTS inbox_reporting")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def identity(self):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES ('client_id', ?)", (uuid4().hex,))
            return db.execute("SELECT value FROM settings WHERE key='client_id'").fetchone()[0]

    def acquire_gateway_lock(self):
        lock = open(self.path.with_suffix(".lock"), "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise ValueError("Another Hermes gateway already owns this Senler inbox") from None
        return lock

    def configure(self, connection, instructions=None, expected_local_revision=None, notification_target=UNSET):
        connection = validate_connection(connection)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT version,config FROM subscriptions WHERE id=?", (connection["webhook_id"],)).fetchone()
            if (existing[0] if existing else None) != expected_local_revision:
                raise ValueError("Local subscription changed; reconcile the newer operator settings before configuring")
            previous = json.loads(existing[1]) if existing else {}
            if previous and connection_identity(previous) != connection_identity(connection):
                db.execute("DELETE FROM relay_health WHERE subscription=?", (connection["webhook_id"],))
            if instructions is None:
                instructions = previous.get("instructions")
            if not isinstance(instructions, str) or not 1 <= len(instructions.strip()) <= 8000:
                raise ValueError("Provide operator instructions (1–8000 characters)")
            connection.update(instructions=instructions.strip())
            if notification_target is UNSET:
                notification_target = previous.get("notification_target")
            if notification_target is not None:
                connection["notification_target"] = validate_target(notification_target)
            if previous.get("notification_target") != notification_target:
                # Pending summaries never move to a new recipient after a rule change.
                db.execute("DELETE FROM notification_queue WHERE subscription=?", (connection["webhook_id"],))
                db.execute("DELETE FROM notification_receipts WHERE subscription=?", (connection["webhook_id"],))
            if db.execute("SELECT count(*) FROM subscriptions WHERE id != ?", (connection["webhook_id"],)).fetchone()[0] >= 8:
                raise ValueError("At most eight Senler subscriptions are supported per Hermes profile")
            db.execute("INSERT INTO subscriptions(id,config,enabled) VALUES (?, ?, 1) ON CONFLICT(id) DO UPDATE SET config=excluded.config, version=version+1",
                       (connection["webhook_id"], json.dumps(connection, ensure_ascii=False)))
            if previous:
                if (previous["instructions"] != connection["instructions"]
                        or previous.get("notification_target") != connection.get("notification_target")
                        or connection_identity(previous) != connection_identity(connection)):
                    self._cancel_processing(db, connection["webhook_id"], "Subscription settings changed; review prior actions before retrying")
                else:
                    self._cancel_processing(db, connection["webhook_id"], "Event is no longer selected by the subscription", keep=connection)
                self._sync_pending(db, connection["webhook_id"], connection)

    def sync_settings(self, subscription, envelope):
        """Only the authenticated server owns event selection/revision; preserve local policy/pause."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT config FROM subscriptions WHERE id=?", (subscription,)).fetchone()
            if row is None:
                raise ValueError("Unknown local Senler subscription")
            config = json.loads(row[0])
            if envelope.get("webhook_id") != subscription or envelope.get("project_id") != config["project_id"]:
                raise ValueError("Received settings do not match the saved subscription")
            connection = {key: config[key] for key in CONNECTION_FIELDS}
            connection.update(events=envelope.get("events"), revision=envelope.get("revision"))
            validate_connection(connection)
            if connection["revision"] < config["revision"]:
                raise ValueError("Received stale subscription settings")
            if connection["revision"] == config["revision"]:
                if set(connection["events"]) != set(config["events"]):
                    raise ValueError("Event selection changed without a revision")
            if envelope.get("active") is False:
                self._cancel_processing(db, subscription, "Senler paused or rechecking this subscription; review prior actions before retrying")
            if connection["revision"] == config["revision"]:
                return
            config.update(events=connection["events"], revision=connection["revision"])
            db.execute("UPDATE subscriptions SET config=?, version=version+1 WHERE id=?",
                       (json.dumps(config, ensure_ascii=False), subscription))
            self._sync_pending(db, subscription, config)
            self._cancel_processing(db, subscription, "Event is no longer selected by the subscription", keep=config)

    @staticmethod
    def _cancel_processing(db, subscription, reason, keep=None):
        # The processing queue is bounded (one worker). Commit revocation in the
        # same transaction as settings so a quick resume cannot revive this run.
        rows = db.execute("SELECT event_id,attempt_id,payload FROM inbox WHERE subscription=? AND state='processing' LIMIT ?",
                          (subscription, MAX_PENDING)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if keep and payload["project_id"] == keep["project_id"] and payload["event_type"] in keep["events"]:
                continue
            RelayStore._finish(db, subscription, row["event_id"], row["attempt_id"], reason)

    @staticmethod
    def _sync_pending(db, subscription, config):
        # Only the bounded active local queue is touched; completed results remain immutable.
        for event in db.execute("SELECT event_id,payload FROM inbox WHERE subscription=? AND state='pending' LIMIT ?",
                                (subscription, MAX_PENDING)):
            payload = json.loads(event["payload"])
            if payload["event_type"] in config["events"] and payload["project_id"] == config["project_id"]:
                db.execute("UPDATE inbox SET revision=? WHERE subscription=? AND event_id=?",
                           (config["revision"], subscription, event["event_id"]))
            else:
                db.execute("UPDATE inbox SET state='done',failed=1,result='Event is no longer selected by the subscription',updated=? WHERE subscription=? AND event_id=?",
                           (time.time(), subscription, event["event_id"]))

    def subscriptions(self):
        with self.db() as db:
            return {row["id"]: {**json.loads(row["config"]), "enabled": bool(row["enabled"]), "local_revision": row["version"]}
                    for row in db.execute("SELECT * FROM subscriptions")}

    def observe_control(self, subscription, active, revision):
        with self.db() as db:
            db.execute("INSERT INTO relay_health VALUES (?,?,?,?) ON CONFLICT(subscription) DO UPDATE SET "
                       "active=excluded.active,revision=excluded.revision,observed=excluded.observed",
                       (subscription, int(active), revision, time.time()))

    def instructions(self, subscription):
        with self.db() as db:
            row = db.execute("SELECT config,version,enabled FROM subscriptions WHERE id=?", (subscription,)).fetchone()
            if row is None:
                raise ValueError("Unknown local Senler subscription")
            config = json.loads(row["config"])
            return {"webhook_id": subscription, "project_id": config["project_id"], "events": config["events"],
                    "instructions": config["instructions"], "notification_target": config.get("notification_target"),
                    "local_revision": row["version"], "enabled": bool(row["enabled"])}

    def update_instructions(self, subscription, instructions, expected_local_revision):
        if not isinstance(instructions, str) or not 1 <= len(instructions.strip()) <= 8000:
            raise ValueError("Provide operator instructions (1–8000 characters)")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT config,version FROM subscriptions WHERE id=?", (subscription,)).fetchone()
            if (row is None or type(expected_local_revision) is not int
                    or row["version"] != expected_local_revision):
                raise ValueError("Local subscription changed; read its current instructions before updating")
            config = json.loads(row["config"])
            if config["instructions"] != instructions.strip():
                self._cancel_processing(db, subscription, "Operator instructions changed; review prior actions before retrying")
            config["instructions"] = instructions.strip()
            db.execute("UPDATE subscriptions SET config=?,version=version+1 WHERE id=?",
                       (json.dumps(config, ensure_ascii=False), subscription))

    def pause(self, subscription):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE subscriptions SET enabled=0, version=version+1 WHERE id=?", (subscription,))
            db.execute("DELETE FROM relay_health WHERE subscription=?", (subscription,))
            self._cancel_processing(db, subscription, "Processing paused by the operator; review prior actions before retrying")

    def update_notification_target(self, subscription, target, expected_local_revision):
        target = validate_target(target) if target is not None else None
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT config,version FROM subscriptions WHERE id=?", (subscription,)).fetchone()
            if row is None or type(expected_local_revision) is not int or row["version"] != expected_local_revision:
                raise ValueError("Local subscription changed; read its current instructions before updating")
            config = json.loads(row["config"])
            if config.get("notification_target") != target:
                self._cancel_processing(db, subscription, "Notification rules changed; review prior actions before retrying")
                db.execute("DELETE FROM notification_queue WHERE subscription=?", (subscription,))
                db.execute("DELETE FROM notification_receipts WHERE subscription=?", (subscription,))
            config.pop("notification_target", None)
            if target is not None:
                config["notification_target"] = target
            db.execute("UPDATE subscriptions SET config=?,version=version+1 WHERE id=?",
                       (json.dumps(config, ensure_ascii=False), subscription))

    def resume(self, subscription, expected_local_revision):
        with self.db() as db:
            changed = db.execute("UPDATE subscriptions SET enabled=1, version=version+1 WHERE id=? AND version=?", (subscription, expected_local_revision))
            if changed.rowcount != 1:
                raise ValueError("Local subscription changed; reconcile the newer operator settings before resuming")

    def remove(self, subscription):
        with self.db() as db:
            db.execute("DELETE FROM subscriptions WHERE id=?", (subscription,))
            db.execute("DELETE FROM inbox WHERE subscription=?", (subscription,))
            db.execute("DELETE FROM notification_queue WHERE subscription=?", (subscription,))
            db.execute("DELETE FROM notification_receipts WHERE subscription=?", (subscription,))
            db.execute("DELETE FROM relay_health WHERE subscription=?", (subscription,))

    def accept(self, config, envelope):
        if (not isinstance(envelope, dict) or envelope.get("webhook_id") != config["webhook_id"]
                or envelope.get("project_id") != config["project_id"] or envelope.get("revision") != config["revision"]):
            raise ValueError("Received event does not match the saved subscription")
        kind = envelope.get("kind")
        if kind == "probe":
            if envelope.get("event_type") not in config["events"] or set(envelope.get("events", [])) != set(config["events"]):
                raise ValueError("Update the local subscription settings from Senler MCP before testing")
            return "probe"
        if kind == "control":
            if type(envelope.get("active")) is not bool:
                raise ValueError("Invalid relay control state")
            return "control"
        payload = envelope.get("payload")
        if (kind != "event" or not isinstance(payload, dict) or payload.get("project_id") != config["project_id"]
                or payload.get("event_type") not in config["events"] or not isinstance(payload.get("event_id"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", payload["event_id"])):
            raise ValueError("Invalid Senler event")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode()) > MAX_BYTES:
            raise ValueError("Senler event exceeds the inbox size limit")
        try:
            expiry = datetime.fromisoformat(envelope["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError()
            expires = expiry.timestamp()
        except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
            raise ValueError("Event retention deadline is missing or invalid") from None
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM inbox WHERE subscription=? AND event_id=?", (config["webhook_id"], payload["event_id"])).fetchone():
                db.execute("UPDATE inbox SET reported=0 WHERE subscription=? AND event_id=? AND state='done'",
                           (config["webhook_id"], payload["event_id"]))
                return "duplicate"
            if db.execute("SELECT count(*) FROM inbox WHERE state IN ('pending','processing')").fetchone()[0] >= MAX_PENDING:
                raise ValueError("Senler inbox is full; event was not acknowledged")
            now = time.time()
            db.execute("INSERT INTO inbox(subscription,event_id,payload,revision,state,created,updated,expires,attempt_id) VALUES (?,?,?,?,'pending',?,?,?,?)",
                       (config["webhook_id"], payload["event_id"], raw, config["revision"], now, now, expires, uuid4().hex))
        return "event"

    def recover(self):
        NotificationOutbox(self).recover()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM relay_health")
            rows = db.execute("SELECT subscription,event_id,attempt_id FROM inbox WHERE state='processing' LIMIT ?",
                              (MAX_PENDING,)).fetchall()
            for row in rows:
                self._finish(db, row["subscription"], row["event_id"], row["attempt_id"],
                             "Processing was interrupted; review the previous actions before retrying")

    def next_event(self, allowed):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM inbox WHERE state='pending' ORDER BY created LIMIT ?", (MAX_PENDING,)):
                if row["expires"] <= time.time():
                    db.execute("UPDATE inbox SET state='done',failed=1,result='Event retention period expired before processing',updated=? WHERE subscription=? AND event_id=?",
                               (time.time(), row["subscription"], row["event_id"]))
                    continue
                config = allowed.get(row["subscription"])
                if not config:
                    continue
                if config.get("notification_target") and db.execute("SELECT count(*) FROM notification_queue").fetchone()[0] >= MAX_NOTIFICATIONS - 1:
                    continue  # Leave work pending until the bounded notification queue drains.
                if config["revision"] != row["revision"]:
                    db.execute("UPDATE inbox SET state='done', failed=1, result='Subscription settings changed before processing', updated=? WHERE subscription=? AND event_id=?",
                               (time.time(), row["subscription"], row["event_id"]))
                    continue
                return dict(row)
        return None

    def start_processing(self, subscription, event_id, attempt_id, expected_local_revision):
        """Commit the local execution boundary only after the server grants this attempt."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            config = db.execute("SELECT version,enabled FROM subscriptions WHERE id=?", (subscription,)).fetchone()
            if config is None or not config["enabled"] or config["version"] != expected_local_revision:
                raise ValueError("Local subscription changed before processing could start")
            changed = db.execute("UPDATE inbox SET state='processing',updated=? WHERE subscription=? AND event_id=? AND attempt_id=? AND state='pending' AND expires>?",
                                 (time.time(), subscription, event_id, attempt_id, time.time()))
            if changed.rowcount != 1:
                raise ValueError("The Senler event is no longer pending")

    @staticmethod
    def _processing_row(db, subscription, event_id, attempt_id):
        row = db.execute("SELECT i.* FROM inbox i JOIN subscriptions s ON s.id=i.subscription "
                         "WHERE i.subscription=? AND i.event_id=? AND i.attempt_id=? AND i.state='processing' AND s.enabled=1",
                         (subscription, event_id, attempt_id)).fetchone()
        if not row or row["expires"] <= time.time():
            raise ValueError("The Senler processing attempt is no longer active; review prior actions before retrying")
        return row

    def require_processing(self, subscription, event_id, attempt_id):
        with self.db() as db:
            self._processing_row(db, subscription, event_id, attempt_id)

    def prepare_reply(self, subscription, event_id, attempt_id, parameters):
        if not isinstance(parameters, dict):
            raise ValueError("Invalid Senler reply parameters")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._processing_row(db, subscription, event_id, attempt_id)
            payload = json.loads(row["payload"])
            if not payload.get("dialog_id") or parameters.get("id") != payload["dialog_id"]:
                raise ValueError("A reply must target the dialog identified by the Senler event")
            prepared = {**parameters, "idempotency_key": "senler:" + subscription + ":" + event_id + ":reply"}
            serialized = json.dumps(prepared, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if row["reply_json"] is not None and row["reply_json"] != serialized:
                raise ValueError("This event already attempted a different reply; inspect its outcome before sending again")
            db.execute("UPDATE inbox SET reply_json=? WHERE subscription=? AND event_id=?", (serialized, subscription, event_id))
            return prepared

    def append_report(self, subscription, event_id, attempt_id, content):
        """Acknowledge report text only after committing it to its active attempt."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._processing_row(db, subscription, event_id, attempt_id)
            previous = row["result"] or ""
            report = (previous + content)[:MAX_REPORT_CHARS]
            if report != previous:
                db.execute("UPDATE inbox SET result=?,updated=? WHERE subscription=? AND event_id=? AND attempt_id=? AND state='processing'",
                           (report, time.time(), subscription, event_id, attempt_id))

    @staticmethod
    def _finish(db, subscription, event_id, attempt_id, error=None, reported=False):
        # Every caller holds the write transaction, including pause/recovery.
        # A stopped result is immutable; only a running attempt can succeed.
        row = db.execute("SELECT state,result FROM inbox WHERE subscription=? AND event_id=? AND attempt_id=? AND state!='done'",
                         (subscription, event_id, attempt_id)).fetchone()
        if row is None or (error is None and row["state"] != "processing"):
            return
        report = row["result"] or ""
        if error is None:
            config_row = db.execute("SELECT config FROM subscriptions WHERE id=?", (subscription,)).fetchone()
            try:
                NotificationOutbox.final_result(db, subscription, event_id,
                                               json.loads(config_row[0]) if config_row else {}, report)
            except ValueError as exc:
                error = str(exc)
        if error is not None:
            # Keep the reason first so truncation cannot hide the interruption.
            report = error + ("\n\nPartial Hermes report:\n" + report if report else "")
        else:
            report = report or "Hermes completed event processing"
        db.execute("UPDATE inbox SET state='done',result=?,failed=?,reported=?,updated=? WHERE subscription=? AND event_id=? AND attempt_id=? AND state!='done'",
                   (report[:MAX_REPORT_CHARS], int(error is not None), int(reported), time.time(), subscription, event_id, attempt_id))

    def finish(self, subscription, event_id, attempt_id, error=None, reported=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._finish(db, subscription, event_id, attempt_id, error, reported)

    def pending_results(self, subscription, after=None):
        with self.db() as db:
            query = "SELECT * FROM inbox WHERE subscription=? AND state='done' AND reported=0"
            order = " ORDER BY updated,event_id LIMIT 1"
            rows = db.execute(query + " AND (updated,event_id)>(?,?)" + order,
                              (subscription, *after)).fetchall() if after else []
            if not rows:
                rows = db.execute(query + order, (subscription,)).fetchall()
            return [dict(row) for row in rows]

    def reported(self, subscription, event_id, attempt_id):
        with self.db() as db:
            db.execute("UPDATE inbox SET reported=1 WHERE subscription=? AND event_id=? AND attempt_id=?", (subscription, event_id, attempt_id))

    def cleanup(self):
        with self.db() as db:
            # Indexed, bounded retention work, including reports that expired on the server.
            db.execute("DELETE FROM inbox WHERE rowid IN (SELECT rowid FROM inbox WHERE state='done' AND updated < ? ORDER BY updated LIMIT 100)",
                       (time.time() - RETENTION,))
            NotificationOutbox.cleanup(db)

    def status(self):
        configs = self.subscriptions()
        outbox = NotificationOutbox(self)
        with self.db() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT subscription,count(*) FROM inbox WHERE state IN ('pending','processing') GROUP BY subscription")}
            health = {row["subscription"]: dict(row) for row in db.execute("SELECT * FROM relay_health")}
            last_events = {}
            for key in configs:
                row = db.execute("SELECT event_id,failed,updated FROM inbox WHERE subscription=? AND state='done' "
                                 "ORDER BY updated DESC LIMIT 1", (key,)).fetchone()
                last_events[key] = ({"event_id": row["event_id"], "status": "failed" if row["failed"] else "completed",
                                     "at": row["updated"]} if row else None)
        return [{"webhook_id": key, "project_id": value["project_id"], "relay_url": value["relay_url"],
                 "events": value["events"], "revision": value["revision"], "enabled": value["enabled"],
                 "local_revision": value["local_revision"],
                 "notification_target": value.get("notification_target"), "notifications": outbox.status(key),
                 "connection": {"active_recently": bool(value["enabled"] and health.get(key, {}).get("active")
                                  and health[key]["revision"] == value["revision"]
                                  and 0 <= time.time() - health[key]["observed"] < 15),
                                "observed_at": health.get(key, {}).get("observed")},
                 "last_event": last_events[key],
                 "pending": counts.get(key, 0)} for key, value in configs.items()]
