import concurrent.futures
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from store import Conflict, NotFound, Store, ValidationError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.alice = self.store.create_user("Alice", "correct horse battery", is_admin=True)
        self.bob = self.store.create_user("bob", "another strong password")
        self.conversation = self.store.create_conversation(self.alice["id"], "Alice tasks")
        self.bob_conversation = self.store.create_conversation(self.bob["id"], "Bob tasks")

    def submit(self, key="request-one", prompt="Create a file"):
        return self.store.submit_task(self.alice["id"], self.conversation["id"], prompt, key)

    def test_passwords_and_sessions_are_hashed_and_survive_restart(self):
        self.assertEqual("alice", self.alice["username"])
        self.assertTrue(self.alice["is_admin"])
        self.assertNotIn("password_hash", self.alice)
        self.assertIsNone(self.store.authenticate_user("alice", "wrong password"))
        self.assertEqual(self.alice["id"], self.store.authenticate_user("ALICE", "correct horse battery")["id"])
        token = self.store.create_session(self.alice["id"], ttl=60)
        with sqlite3.connect(self.store.db_path) as database:
            digest = database.execute("SELECT token_hash FROM sessions").fetchone()[0]
            self.assertNotEqual(token, digest)
            password_hash = database.execute("SELECT password_hash FROM users WHERE id=?", (self.alice["id"],)).fetchone()[0]
            self.assertNotEqual(b"correct horse battery", password_hash)
        reopened = Store(self.temp.name)
        self.assertEqual(self.alice["id"], reopened.get_session(token)["id"])
        self.assertIsNone(reopened.get_session(token, now=time.time() + 61))
        reopened.revoke_session(token)
        self.assertIsNone(self.store.get_session(token))

    def test_unique_normalized_username_and_validation(self):
        with self.assertRaises(Conflict):
            self.store.create_user("ALICE", "a duplicate password")
        for invalid in ("aa", "../root", "a@b", "a" * 33):
            with self.assertRaises(ValidationError):
                self.store.create_user(invalid, "a secure password")
        with self.assertRaises(ValidationError):
            self.store.create_user("charlie", "short")

    def test_tenant_ownership_and_event_isolation(self):
        task = self.submit()
        for operation in (
            lambda: self.store.get_conversation(self.bob["id"], self.conversation["id"]),
            lambda: self.store.list_messages(self.bob["id"], self.conversation["id"]),
            lambda: self.store.add_message(self.bob["id"], self.conversation["id"], "user", "attack"),
            lambda: self.store.get_task(self.bob["id"], task["id"]),
            lambda: self.store.cancel_task(self.bob["id"], task["id"]),
            lambda: self.store.list_events(self.bob["id"], task_id=task["id"]),
            lambda: self.store.submit_task(self.bob["id"], self.conversation["id"], "attack", "attack"),
            lambda: self.store.append_event(self.bob["id"], task["id"], "output", "attack"),
            lambda: self.store.add_message(self.bob["id"], self.bob_conversation["id"], "assistant", "attack", task_id=task["id"]),
        ):
            with self.assertRaises(NotFound):
                operation()
        self.assertEqual([], self.store.list_events(self.bob["id"]))
        self.assertEqual([], self.store.list_tasks(self.bob["id"]))
        self.assertEqual(1, len(self.store.list_messages(self.alice["id"], self.conversation["id"])))

    def test_idempotency_does_not_duplicate_tasks_messages_or_events(self):
        first = self.submit()
        second = self.submit()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, len(self.store.list_tasks(self.alice["id"])))
        self.assertEqual(1, len(self.store.list_messages(self.alice["id"], self.conversation["id"])))
        self.assertEqual(2, len(self.store.list_events(self.alice["id"])))
        with self.assertRaises(Conflict):
            self.submit(prompt="Different content")
        # Idempotency keys are scoped to the user, not global across tenants.
        other = self.store.submit_task(self.bob["id"], self.bob_conversation["id"], "Bob task", "request-one")
        self.assertNotEqual(first["id"], other["id"])

    def test_atomic_claim_with_contending_worker_threads(self):
        for number in range(8):
            self.submit(key=f"task-{number}")
        barrier = threading.Barrier(8)

        def attempt(number):
            barrier.wait()
            return self.store.claim_task(f"worker-{number}", max_concurrency=1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(attempt, range(8)))
        winners = [task for task in claimed if task]
        self.assertEqual(1, len(winners))
        self.assertEqual(1, self.store.queue_counts()["running"])
        self.assertEqual(7, self.store.queue_counts()["queued"])
        task = winners[0]
        self.store.finish_task(task["id"], task["worker_id"], result="done")
        self.assertIsNotNone(self.store.claim_task("next-worker"))

    def test_per_user_execution_remains_serial_when_global_concurrency_increases(self):
        self.submit("alice-one")
        self.submit("alice-two")
        self.store.submit_task(self.bob["id"], self.bob_conversation["id"], "Bob task", "bob-one")
        first = self.store.claim_task("worker-a", max_concurrency=3)
        second = self.store.claim_task("worker-b", max_concurrency=3)
        self.assertNotEqual(first["user_id"], second["user_id"])
        self.assertIsNone(self.store.claim_task("worker-c", max_concurrency=3))

    def test_stale_lease_is_interrupted_and_never_automatically_replayed(self):
        task = self.submit()
        claimed = self.store.claim_task("worker-original")
        self.assertEqual(1, self.store.recover_stale_tasks(now=claimed["lease_expires_at"] + 1))
        recovered = self.store.get_task(self.alice["id"], task["id"])
        self.assertEqual("interrupted", recovered["status"])
        self.assertIsNone(self.store.claim_task("worker-new"))
        self.assertFalse(self.store.heartbeat(task["id"], "worker-original"))
        with self.assertRaises(Conflict):
            self.store.finish_task(task["id"], "worker-original")
        retry = self.store.retry_task(self.alice["id"], task["id"], "explicit-retry")
        self.assertEqual(task["id"], retry["metadata"]["retry_of"])
        self.assertNotEqual(task["id"], retry["id"])
        self.assertEqual(retry["id"], self.store.claim_task("worker-new")["id"])

    def test_worker_ownership_and_cancel_acknowledgement_keep_slot_reserved(self):
        original = self.submit()
        next_task = self.submit("next-task")
        claimed = self.store.claim_task("owner")
        self.assertFalse(self.store.heartbeat(original["id"], "impostor"))
        with self.assertRaises(Conflict):
            self.store.finish_task(original["id"], "impostor")
        cancellation = self.store.cancel_task(self.alice["id"], original["id"])
        self.assertTrue(cancellation["cancel_requested"])
        self.assertEqual("running", cancellation["status"])
        self.assertFalse(self.store.heartbeat(original["id"], "owner"))
        self.assertIsNone(self.store.claim_task("worker-new"))
        # Worker calls this only after terminating its child execution.
        self.store.finish_task(claimed["id"], "owner", "cancelled")
        self.assertEqual(next_task["id"], self.store.claim_task("worker-new")["id"])

    def test_waiting_configuration_requeues_without_losing_original_message(self):
        original = self.submit()
        self.store.claim_task("worker")
        self.store.mark_waiting_config(original["id"], "worker")
        self.assertEqual("waiting_config", self.store.get_task(self.alice["id"], original["id"])["status"])
        self.assertIsNone(self.store.claim_task("worker"))
        self.assertEqual(1, self.store.requeue_waiting_tasks())
        self.assertEqual(original["id"], self.store.claim_task("worker")["id"])
        self.assertEqual(1, len(self.store.list_messages(self.alice["id"], self.conversation["id"])))

    def test_cancellation_wins_race_with_worker_completion(self):
        task = self.submit()
        self.store.claim_task("worker")
        self.store.cancel_task(self.alice["id"], task["id"])
        final = self.store.finish_task(task["id"], "worker", result="Already completed output")
        self.assertEqual("cancelled", final["status"])
        self.assertEqual("Already completed output", final["result"])

    def test_records_workspace_events_and_settings_persist_across_reopen(self):
        task = self.submit()
        marker = self.store.workspace_path(self.alice["id"]) / "files" / "keep.txt"
        marker.write_text("persistent user content", encoding="utf-8")
        cursor = self.store.append_event(self.alice["id"], task["id"], "output", {"text": "saved progress"})
        self.store.set_setting("proxy_token", "alice-private-token", user_id=self.alice["id"])
        self.store.set_setting("model", {"name": "deepseek-chat"})
        self.store.set_user_permissions(self.alice["id"], {"network": True})
        reopened = Store(self.temp.name)
        self.assertEqual("queued", reopened.get_task(self.alice["id"], task["id"])["status"])
        self.assertEqual("persistent user content", marker.read_text(encoding="utf-8"))
        self.assertEqual("saved progress", reopened.list_events(self.alice["id"], after=cursor - 1)[0]["payload"]["text"])
        self.assertEqual("alice-private-token", reopened.get_setting("proxy_token", user_id=self.alice["id"]))
        self.assertIsNone(reopened.get_setting("proxy_token", user_id=self.bob["id"]))
        self.assertEqual({"proxy_token": "alice-private-token"}, reopened.list_settings(user_id=self.alice["id"]))
        self.assertEqual({"name": "deepseek-chat"}, reopened.get_setting("model"))
        self.assertEqual({"network": True}, reopened.user_permissions(self.alice["id"]))
        self.assertEqual(2, len(reopened.list_all_users()))

    def test_consistent_online_backup_includes_uncheckpointed_commits(self):
        task = self.submit()
        backup = self.store.backup(Path(self.temp.name) / "backups" / "snapshot.sqlite3")
        with sqlite3.connect(backup) as database:
            self.assertEqual(task["id"], database.execute("SELECT id FROM tasks").fetchone()[0])
            self.assertEqual("ok", database.execute("PRAGMA integrity_check").fetchone()[0])
        with self.assertRaises(ValidationError):
            self.store.backup(self.store.db_path)


if __name__ == "__main__":
    unittest.main()
