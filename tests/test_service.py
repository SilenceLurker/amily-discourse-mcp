from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from discourse_mcp.discourse import Discourse
from discourse_mcp.errors import AppError, RemoteError
from discourse_mcp.service import Service
from discourse_mcp.store import Store, valid_origin
from discourse_mcp.vault import DPAPIVault


class Forum(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.calls.append(("GET", self.path, self.headers.get("Api-Username"), self.headers.get("Api-Key"), None))
        if self.path.startswith("/t/"):
            topic_id = int(self.path.split("/")[2].split(".")[0])
            topic = self.server.topics.get(topic_id)
            if topic:
                self.respond(200, topic)
                return
        elif self.path.startswith("/posts/"):
            post_id = int(self.path.split("/")[2].split(".")[0])
            if post_id in self.server.posts:
                self.respond(200, self.server.posts[post_id])
                return
        self.respond(404, {"errors": ["not found"]})

    def do_POST(self):
        self.write_post()

    def do_PUT(self):
        self.write_post()

    def write_post(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.command, self.path, self.headers.get("Api-Username"), self.headers.get("Api-Key"), body))
        if self.server.mode == "redirect":
            self.send_response(307)
            self.send_header("Location", self.server.redirect_target)
            self.end_headers()
            return
        if self.server.mode == "denied":
            self.respond(403, {"errors": [self.headers.get("Api-Key")]})
            return
        if self.server.mode == "server_error":
            self.respond(500, {"errors": [self.headers.get("Api-Key")]})
            return
        if self.command == "PUT":
            post_id = int(self.path.split("/")[2].split(".")[0])
            post = self.server.posts[post_id]
            post.update(raw=body["post"]["raw"], version=post["version"] + 1)
        else:
            post_id = 100 + len(self.server.posts)
            topic_id = body.get("topic_id", 200 + post_id)
            post = {"id": post_id, "topic_id": topic_id, "username": self.headers["Api-Username"],
                    "post_number": 2 if "topic_id" in body else 1, "raw": body["raw"], "version": 1}
            self.server.posts[post_id] = post
            if "topic_id" not in body:
                self.server.topics[topic_id] = {"id": topic_id, "archetype": "regular", "category_id": body["category"],
                                               "title": body["title"], "post_stream": {"posts": [post]}}
        if self.server.mode == "disconnect":
            self.close_connection = True
            return
        if self.server.mode == "queued":
            self.respond(200, {"action": "enqueued", "pending_count": 1})
            return
        self.respond(200, {"post": post} if self.command == "PUT" else post)


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vault = DPAPIVault()
        cls.forum = ThreadingHTTPServer(("127.0.0.1", 0), Forum)
        cls.thread = threading.Thread(target=cls.forum.serve_forever, daemon=True)
        cls.thread.start()
        cls.site = f"http://127.0.0.1:{cls.forum.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.forum.shutdown()
        cls.forum.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name))
        self.keys = {"audit": "synthetic_audit_credential_123456789", "personal": "synthetic_personal_credential_123456789"}
        for alias, user, categories in [("audit", "agent-audit", [60]), ("personal", "personal-user", [61])]:
            self.store.add_account(alias, {"site": self.site, "username": user, "categories": categories,
                "actions": ["read", "create", "reply", "edit"], "require_approval": True},
                self.vault.encrypt(alias, self.keys[alias]))
        self.service = Service(self.store, self.vault, client_factory=partial(Discourse, allow_http=True))
        self.forum.calls = []
        self.forum.mode = "normal"
        self.forum.topics = {
            1: {"id": 1, "title": "Audit topic", "archetype": "regular", "category_id": 60, "post_stream": {"posts": []}},
            2: {"id": 2, "title": "Personal topic", "archetype": "regular", "category_id": 61, "post_stream": {"posts": []}},
            3: {"id": 3, "archetype": "private_message", "category_id": 60},
        }
        self.forum.posts = {10: {"id": 10, "topic_id": 1, "username": "agent-audit", "post_number": 1,
                                  "raw": "Original text", "version": 1},
                            11: {"id": 11, "topic_id": 1, "username": "someone-else", "post_number": 2,
                                  "raw": "Another person's text", "version": 1}}

    def draft(self, alias="audit", **kwargs):
        return self.service.prepare(alias, "create", "A released fix and its severity.",
                                    title="Release security notice", category_id=60 if alias == "audit" else 61, **kwargs)

    def approve(self, draft):
        self.store.approve(draft["account"], draft["id"], draft["revision"], draft["hash"])

    def publish(self, draft, request="request_12345"):
        return self.service.publish(draft["account"], draft["id"], draft["revision"], request)

    def writes(self):
        return [call for call in self.forum.calls if call[0] != "GET"]

    def test_dpapi_roundtrip_and_account_entropy(self):
        encrypted = self.vault.encrypt("audit", self.keys["audit"])
        self.assertNotIn(self.keys["audit"].encode(), encrypted)
        self.assertEqual(self.vault.decrypt("audit", encrypted), self.keys["audit"])
        with self.assertRaises(AppError):
            self.vault.decrypt("personal", encrypted)
        with self.assertRaises(AppError):
            self.vault.decrypt("audit", encrypted[:-8] + b"tampered")

    def test_account_metadata_never_contains_credentials(self):
        self.assertNotIn("secret", json.dumps(self.service.accounts()))
        for key in self.keys.values():
            self.assertNotIn(key, json.dumps(self.service.accounts()))
            self.assertNotIn(key.encode(), self.store.path.read_bytes())

    def test_multiple_accounts_use_own_headers_and_categories(self):
        for alias in ("audit", "personal"):
            draft = self.draft(alias)
            self.approve(draft)
            result = self.publish(draft, "request_" + alias)
            self.assertEqual(result["status"], "published")
        for call, alias, user in zip(self.writes(), self.keys, ("agent-audit", "personal-user")):
            self.assertEqual(call[2:4], (user, self.keys[alias]))
        self.assertEqual([call[4]["category"] for call in self.writes()], [60, 61])

    def test_client_account_allowlist_and_draft_binding(self):
        narrowed = Service(self.store, self.vault, allowed_accounts=["audit"])
        self.assertEqual([a["alias"] for a in narrowed.accounts()], ["audit"])
        with self.assertRaises(AppError):
            narrowed.read_topic("personal", 2)
        draft = self.draft()
        with self.assertRaises(AppError):
            self.service.draft("personal", draft["id"])
        with self.assertRaises(AppError):
            self.service.publish("personal", draft["id"], 1, "other_request")
        self.assertEqual(self.writes(), [])

    def test_categories_private_messages_and_foreign_edits_blocked(self):
        for function in [lambda: self.service.prepare("audit", "create", "Text", title="Title", category_id=61),
                         lambda: self.service.read_topic("audit", 2),
                         lambda: self.service.prepare("audit", "reply", "Text", topic_id=2),
                         lambda: self.service.read_topic("audit", 3),
                         lambda: self.service.prepare("audit", "edit", "Text", post_id=11)]:
            with self.assertRaises(AppError):
                function()
        self.assertEqual(self.writes(), [])

    def test_approval_required_and_revision_change_invalidates_approval(self):
        draft = self.draft()
        with self.assertRaisesRegex(AppError, "approval"):
            self.publish(draft)
        self.approve(draft)
        changed = self.draft(draft_id=draft["id"], expected_revision=1)
        self.assertEqual(changed["revision"], 2)
        self.assertFalse(changed["approved"])
        with self.assertRaises(AppError):
            self.store.approve("audit", draft["id"], 1, draft["hash"])
        with self.assertRaises(AppError):
            self.draft(draft_id=draft["id"], expected_revision=1)
        with self.assertRaises(AppError):
            self.publish(changed)
        self.assertEqual(self.writes(), [])

    def test_key_rotation_and_disable_invalidate_pending_drafts(self):
        draft = self.draft()
        self.approve(draft)
        self.store.rotate("audit", self.vault.encrypt("audit", "new_synthetic_key_123456789"))
        with self.assertRaises(AppError):
            self.publish(draft)
        self.store.enable("audit", False)
        with self.assertRaises(AppError):
            self.service.read_topic("audit", 1)
        self.assertEqual(self.writes(), [])

    def test_idempotent_replay_and_no_new_request_for_same_revision(self):
        draft = self.draft()
        self.approve(draft)
        first = self.publish(draft)
        again = self.publish(draft)
        self.assertEqual(first["result"], again["result"])
        self.assertTrue(again["replayed"])
        with self.assertRaises(AppError):
            self.publish(draft, "different_request")
        other = self.draft()
        self.approve(other)
        with self.assertRaises(AppError):
            self.publish(other)
        self.assertEqual(len(self.writes()), 1)

    def test_disconnect_records_unknown_and_blocks_resend(self):
        draft = self.draft()
        self.approve(draft)
        self.forum.mode = "disconnect"
        result = self.publish(draft)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(self.publish(draft)["replayed"])
        with self.assertRaises(AppError):
            self.publish(draft, "second_request")
        with self.assertRaises(AppError):
            self.draft(draft_id=draft["id"], expected_revision=1)
        self.assertEqual(len(self.writes()), 1)
        self.forum.mode = "normal"
        receipt = self.service.resolve("audit", draft["id"], max(self.forum.posts))
        self.assertEqual(receipt["status"], "published")

    def test_error_bodies_redacted_and_status_classified(self):
        for mode, status in [("denied", "rejected"), ("server_error", "unknown"), ("queued", "unknown")]:
            draft = self.draft()
            self.approve(draft)
            self.forum.mode = mode
            result = self.publish(draft, "request_" + mode)
            self.assertEqual(result["status"], status)
            self.assertNotIn(self.keys["audit"], json.dumps(result))

    def test_redirect_does_not_forward_credentials_or_repeat_write(self):
        self.forum.mode = "redirect"
        self.forum.redirect_target = self.site + "/redirect-destination"
        draft = self.draft()
        self.approve(draft)
        self.assertEqual(self.publish(draft)["status"], "unknown")
        self.assertEqual(len(self.writes()), 1)

    def test_reply_edit_and_remote_change_guard(self):
        reply = self.service.prepare("audit", "reply", "Reply text", topic_id=1)
        self.approve(reply)
        self.assertEqual(self.publish(reply)["status"], "published")
        edit = self.service.prepare("audit", "edit", "Edited text", post_id=10)
        self.approve(edit)
        self.forum.posts[10]["version"] += 1
        with self.assertRaisesRegex(AppError, "changed"):
            self.publish(edit, "request_edit")
        edit = self.service.prepare("audit", "edit", "Edited text", post_id=10,
                                    draft_id=edit["id"], expected_revision=1)
        self.approve(edit)
        self.assertEqual(self.publish(edit, "request_edit_new")["status"], "published")
        self.assertEqual(self.forum.posts[10]["raw"], "Edited text")

    def test_topic_moved_after_prepare_is_blocked(self):
        draft = self.service.prepare("audit", "reply", "Reply", topic_id=1)
        self.approve(draft)
        self.forum.topics[1]["category_id"] = 61
        with self.assertRaises(AppError):
            self.publish(draft)
        self.assertEqual(self.writes(), [])

    def test_parallel_submissions_use_single_atomic_claim(self):
        draft = self.draft()
        self.approve(draft)
        def submit(index):
            service = Service(Store(Path(self.temporary.name)), self.vault,
                              client_factory=partial(Discourse, allow_http=True))
            try:
                return service.publish("audit", draft["id"], 1, f"parallel_{index}")["status"]
            except AppError:
                return "blocked"
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(submit, range(4)))
        self.assertEqual(outcomes.count("published"), 1)
        self.assertEqual(len(self.writes()), 1)

    def test_cross_process_claims_share_database(self):
        draft = self.draft()
        self.approve(draft)
        script = ("import sys; from pathlib import Path; from discourse_mcp.store import Store; "
                  "s=Store(Path(sys.argv[1])); "
                  "s.claim('audit',sys.argv[2],1,sys.argv[3]); print('claimed')")
        processes = [subprocess.Popen([sys.executable, "-c", script, self.temporary.name, draft["id"], f"process_{i}"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        outputs = [p.communicate(timeout=15) for p in processes]
        self.assertEqual(sum(p.returncode == 0 for p in processes), 1)
        self.assertEqual(sum(b"claimed" in output[0] for output in outputs), 1)

    def test_read_filters_suggested_topics_and_returns_raw_post(self):
        self.forum.topics[1]["suggested_topics"] = [{"title": "Outside-category secret"}]
        result = self.service.read_topic("audit", 1)
        self.assertNotIn("Outside-category secret", json.dumps(result))
        self.assertEqual(self.service.read_post("audit", 10)["raw"], "Original text")

    def test_origin_and_input_validation(self):
        for origin in ["http://forum.example", "https://user:pass@forum.example", "https://forum.example/path",
                       "https://forum.example?x=1", "https://forum.example/#x", "https://forum.example\\other"]:
            with self.assertRaises(AppError):
                valid_origin(origin)
        self.assertEqual(valid_origin("https://Forum.Example/"), "https://forum.example")
        with self.assertRaises(AppError):
            self.service.prepare("audit", "create", "", title="Title", category_id=60)

    def test_action_policy_and_local_unattended_opt_in(self):
        self.store.add_account("read-only", {"site": self.site, "username": "reader", "categories": [60],
            "actions": ["read"], "require_approval": True}, self.vault.encrypt("read-only", "synthetic_reader_key_123456789"))
        with self.assertRaises(AppError):
            self.service.prepare("read-only", "create", "Body", title="Title", category_id=60)
        self.store.add_account("auto", {"site": self.site, "username": "auto-user", "categories": [60],
            "actions": ["create"], "require_approval": False}, self.vault.encrypt("auto", "synthetic_auto_key_123456789"))
        draft = self.service.prepare("auto", "create", "Body", title="Title", category_id=60)
        self.assertEqual(self.publish(draft, "request_auto")["status"], "published")

    def test_resolve_unknown_as_not_sent_requires_new_revision(self):
        draft = self.draft()
        self.approve(draft)
        self.store.claim("audit", draft["id"], 1, "crashed_request")
        result = self.service.resolve("audit", draft["id"], None)
        self.assertEqual(result["status"], "rejected")
        revised = self.draft(draft_id=draft["id"], expected_revision=1)
        self.assertEqual(revised["revision"], 2)
        self.assertFalse(revised["approved"])


if __name__ == "__main__":
    unittest.main()
