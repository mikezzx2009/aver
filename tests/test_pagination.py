"""History remains reachable beyond the original display limits."""
import tempfile
import unittest

from store import NotFound, Store, ValidationError


class HistoryPaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.alice = self.store.create_user("alice", "a private password")
        self.bob = self.store.create_user("bob", "another private password")
        self.uid = self.alice["id"]
        self.cid = self.store.create_conversation(self.uid)["id"]

    def test_messages_beyond_1000_are_available_and_live_tail_does_not_freeze(self):
        with self.store._transaction() as database:
            database.executemany("INSERT INTO messages(user_id,conversation_id,role,content,created_at) VALUES(?,?,'assistant',?,?)",
                                 [(self.uid, self.cid, f"Message {number}", number) for number in range(1105)])
        initial = self.store.list_messages_page(self.uid, self.cid)
        self.assertEqual("Message 1005", initial["items"][0]["content"])
        self.assertEqual("Message 1104", initial["items"][-1]["content"])
        latest_id = initial["items"][-1]["id"]
        collected = initial["items"]
        page = initial
        while page["has_more"]:
            page = self.store.list_messages_page(self.uid, self.cid, before=page["next_cursor"], limit=73)
            collected = page["items"] + collected
        self.assertEqual([f"Message {number}" for number in range(1105)], [item["content"] for item in collected])
        new = self.store.add_message(self.uid, self.cid, "assistant", "Latest update beyond the old ceiling")
        live = self.store.list_messages_page(self.uid, self.cid, after=latest_id)
        self.assertEqual([new["id"]], [item["id"] for item in live["items"]])
        with self.assertRaises(NotFound):
            self.store.list_messages_page(self.bob["id"], self.cid)

    def test_task_cursor_reaches_every_record_with_equal_timestamps(self):
        ids = [self.store.submit_task(self.uid, self.cid, f"Task {number}", f"task-{number}")["id"] for number in range(210)]
        with self.store._transaction() as database:
            database.execute("UPDATE tasks SET created_at=1234 WHERE user_id=?", (self.uid,))
        initial = self.store.list_tasks_page(self.uid, conversation_id=self.cid, limit=37)
        collected = initial["items"]
        # A concurrent new submission must not disturb an older-page cursor.
        new = self.store.submit_task(self.uid, self.cid, "New submission", "new-submission")
        page = initial
        while page["has_more"]:
            page = self.store.list_tasks_page(self.uid, conversation_id=self.cid, cursor=page["next_cursor"], limit=37)
            collected.extend(page["items"])
        self.assertEqual(sorted(ids, reverse=True), [item["id"] for item in collected])
        self.assertEqual(new["id"], self.store.list_tasks_page(self.uid, conversation_id=self.cid)["items"][0]["id"])
        # The oldest running task can leave the latest page and still be refreshed.
        claimed = self.store.claim_task("worker")
        self.store.finish_task(claimed["id"], "worker", result="completed")
        self.assertEqual("succeeded", self.store.get_tasks(self.uid, [claimed["id"]], conversation_id=self.cid)[0]["status"])
        with self.assertRaises(NotFound):
            self.store.get_tasks(self.bob["id"], [claimed["id"]])
        with self.assertRaises(NotFound):
            self.store.list_tasks_page(self.bob["id"], conversation_id=self.cid)

    def test_events_beyond_500_allow_earlier_history_and_new_progress(self):
        task = self.store.submit_task(self.uid, self.cid, "Work", "work")
        with self.store._transaction() as database:
            database.executemany("INSERT INTO events(user_id,task_id,event_type,payload_json,created_at) VALUES(?,?,'output',?,?)",
                                 [(self.uid, task["id"], '{"number":' + str(number) + '}', number) for number in range(605)])
        initial = self.store.list_events_page(self.uid, task_id=task["id"])
        self.assertEqual(604, initial["items"][-1]["payload"]["number"])
        latest_id = initial["items"][-1]["id"]
        collected, page = initial["items"], initial
        while page["has_more"]:
            page = self.store.list_events_page(self.uid, task_id=task["id"], before=page["next_cursor"], limit=61)
            collected = page["items"] + collected
        self.assertEqual(list(range(605)), [item["payload"]["number"] for item in collected if item["type"] == "output"])
        cursor = self.store.append_event(self.uid, task["id"], "output", {"number": 605})
        live = self.store.list_events_page(self.uid, task_id=task["id"], after=latest_id)
        self.assertEqual([cursor], [item["cursor"] for item in live["items"]])
        with self.assertRaises(NotFound):
            self.store.list_events_page(self.bob["id"], task_id=task["id"])

    def test_invalid_cursors_and_ambiguous_directions_are_rejected(self):
        for cursor in ("invalid!", "WzAsICJhIl0", "W05hTiwgImFhIl0"):
            with self.subTest(cursor=cursor), self.assertRaises(ValidationError):
                self.store.list_tasks_page(self.uid, cursor=cursor)
        for operation in (
            lambda: self.store.list_messages_page(self.uid, self.cid, before=10, after=20),
            lambda: self.store.list_events_page(self.uid, before=10, after=20),
            lambda: self.store.list_messages_page(self.uid, self.cid, limit=201),
            lambda: self.store.list_events_page(self.uid, after=-1),
            lambda: self.store.get_tasks(self.uid, [{}]),
        ):
            with self.assertRaises(ValidationError):
                operation()


if __name__ == "__main__":
    unittest.main()
