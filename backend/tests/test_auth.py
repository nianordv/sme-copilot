"""The owner's sign-in, checked through the HTTP boundary. No Ollama is needed."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import random
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import agent, auth, main  # Importing main registers the real actions.
from backend.actions import registry
from backend.tests.helpers import ANY_BOOKS, OWNER_PIN, BooksTestCase

START = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
CHAT = "chat-1"
WRONG = "00000000"


class AuthTestCase(BooksTestCase):
    """The real server over a temporary database, with the owner's PIN set and
    a clock the test moves."""

    rows = ANY_BOOKS

    def setUp(self):
        super().setUp()
        self.clock = START
        for patched in (patch.dict(os.environ, {auth.PIN_VARIABLE: OWNER_PIN}),
                        patch.object(auth, "_now", lambda: self.clock),
                        patch.object(registry, "_now", lambda: self.clock)):
            patched.start()
            self.addCleanup(patched.stop)
        self.client = self.new_client()

    def new_client(self):
        client = TestClient(main.app, base_url="http://127.0.0.1:8000")
        self.addCleanup(client.close)
        return client

    def later(self, **step):
        self.clock += timedelta(**step)

    def login(self, pin=OWNER_PIN, client=None, **options):
        return (client or self.client).post("/login", json={"pin": pin}, **options)

    def unlock(self, pin=OWNER_PIN, session_id=CHAT):
        return self.client.post("/unlock", json={"session_id": session_id, "pin": pin})

    def signed_in(self, client=None):
        return (client or self.client).get("/me").json()["signed_in"]

    def owner_rows(self):
        return [(row["action"], row["decision"], row["result"])
                for row in registry.audit_log(registry.OWNER_SESSION)]

    def lock_the_chat(self):
        for _ in range(registry.LOCK_AFTER_REFUSALS):
            registry.execute("transfer_money", {}, CHAT)

    def decisions(self, session_id=CHAT):
        return [(row["action"], row["decision"]) for row in registry.audit_log(session_id)]


class GuardTests(AuthTestCase):
    def test_every_endpoint_but_three_needs_the_owner(self):
        guarded = set()
        # Every endpoint the server has, whatever is added later included.
        for path, methods in main.app.openapi()["paths"].items():
            if path in auth.OPEN_PATHS:
                continue
            for method in methods:
                response = self.client.request(method.upper(), path)
                self.assertEqual((response.status_code, response.json()),
                                 (401, {"detail": "Not signed in."}), f"{method} {path}")
            guarded.add(path)
        self.assertLessEqual({"/chat", "/chat/stream", "/confirm", "/logout", "/unlock"}, guarded)
        self.assertEqual(set(main.app.openapi()["paths"]) - guarded, {"/health", "/login", "/me"})

    def test_an_endpoint_added_under_a_prefix_is_not_taken_for_an_open_one(self):
        app = FastAPI(dependencies=[Depends(auth.require_owner)])
        added = APIRouter()
        added.add_api_route("/health", lambda: {"company": "figures"})
        added.add_api_route("/me", lambda: {"company": "figures"})
        app.include_router(added, prefix="/admin")
        app.include_router(added)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            self.assertEqual(client.get("/admin/health").status_code, 401)
            self.assertEqual(client.get("/admin/me").status_code, 401)
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/health/").status_code, 200)  # Sent on to /health.
            self.assertEqual(client.get("//health").status_code, 404)

    def test_a_stranger_gets_the_open_endpoints_and_nothing_else(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/me").json(), {"signed_in": False})

    def test_a_stranger_cannot_approve_a_waiting_request(self):
        waiting = registry.execute("send_reminder", {"customer": "CL0001"}, CHAT)
        self.assertEqual(waiting["status"], "needs_confirmation")
        response = self.client.post("/confirm", json={
            "confirmation_id": waiting["confirmation_id"], "approved": True, "session_id": CHAT})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.decisions(), [("send_reminder", "waiting")])

    def test_a_stranger_cannot_reach_the_model(self):
        model = AsyncMock()
        with patch.object(agent, "request_action_turn", model):
            for path in ("/chat", "/chat/stream"):
                response = self.client.post(path, json={
                    "session_id": CHAT, "messages": [{"role": "user", "content": "What is my balance?"}]})
                self.assertEqual(response.status_code, 401)
        model.assert_not_awaited()
        self.assertEqual(registry.audit_log(), [])

    def test_not_signed_in_is_said_before_anything_about_the_request(self):
        # A request with mistakes in it gets the same answer as any other.
        self.assertEqual(self.client.post("/confirm", json={"approved": "yes"}).status_code, 401)
        self.assertEqual(self.client.post("/chat", content=b"not json").status_code, 401)

    def test_a_made_up_cookie_is_not_a_login(self):
        self.client.cookies.set(auth.COOKIE, "made-up-token")
        self.assertFalse(self.signed_in())
        self.assertEqual(self.client.post("/logout").status_code, 401)

    def test_without_the_books_nobody_is_signed_in(self):
        with patch.dict(os.environ, {"RIGHTSIZE_DB": str(self.folder / "missing.db")}):
            self.assertEqual(self.client.get("/me").json(), {"signed_in": False})
            response = self.login()
            self.assertEqual(response.status_code, 503)
            self.assertIn("python -m backend.bank.seed", response.json()["detail"])
            self.assertEqual(self.client.post("/logout").status_code, 401)


class SignInTests(AuthTestCase):
    def test_the_right_pin_signs_in(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"signed_in": True, "expires_at": "2026-10-14T17:00:00.000000+00:00"})
        self.assertEqual(self.client.get("/me").json(), response.json())
        self.assertEqual(self.owner_rows(), [("sign_in", "signed_in", "signed in for 8 hours")])
        # A guarded endpoint now answers the request itself.
        answer = self.client.post("/confirm", json={
            "confirmation_id": "never-issued", "approved": True, "session_id": CHAT})
        self.assertEqual((answer.status_code, answer.json()["status"]), (200, "error"))

    def test_the_cookie_is_hidden_from_scripts_and_from_other_websites(self):
        cookie = self.login().headers["set-cookie"]
        parts = [part.strip().lower() for part in cookie.split(";")]
        self.assertTrue(parts[0].startswith(f"{auth.COOKIE}="))
        for wanted in ("httponly", "samesite=strict", "path=/", "max-age=28800"):
            self.assertIn(wanted, parts)

    def test_a_wrong_pin_does_not_sign_in(self):
        response = self.login(WRONG)
        self.assertEqual((response.status_code, response.json()), (401, {"detail": "Wrong PIN."}))
        self.assertNotIn("set-cookie", response.headers)
        self.assertFalse(self.signed_in())
        self.assertEqual(self.owner_rows(), [("sign_in", "refused", "wrong PIN")])
        self.assertEqual(self.query("SELECT COUNT(*) FROM logins"), [(0,)])

    def test_a_pin_that_only_starts_or_ends_like_the_right_one_is_wrong(self):
        # The last one has the Arabic-Indic digit two in place of "2".
        for pin in (OWNER_PIN[:-1], OWNER_PIN + "0", f" {OWNER_PIN}", OWNER_PIN.replace("2", chr(0x0662))):
            self.later(minutes=10)  # Apart, so the limit on wrong PINs plays no part here.
            self.assertEqual(self.login(pin).status_code, 401, pin)

    def test_neither_the_pin_nor_the_token_is_stored(self):
        self.login(WRONG)
        self.login()
        token = self.client.cookies.get(auth.COOKIE)
        self.assertGreaterEqual(len(token), 40)
        stored = self.database.read_bytes()
        for secret in (OWNER_PIN, WRONG, token):
            self.assertNotIn(secret.encode(), stored)
        self.assertEqual(self.query("SELECT token_hash FROM logins"),
                         [(hashlib.sha256(token.encode()).hexdigest(),)])
        self.assertEqual([row["arguments"] for row in registry.audit_log()], [{}, {}])

    def test_without_a_pin_on_the_server_nobody_can_sign_in(self):
        for value, said in ((None, "is not set on the server"), ("", "is not set on the server"),
                            ("12345", "must have 6 to 64 characters"), ("9" * 65, "must have 6 to 64 characters")):
            setting = {} if value is None else {auth.PIN_VARIABLE: value}
            with patch.dict(os.environ, setting):
                if value is None:
                    del os.environ[auth.PIN_VARIABLE]
                for pin in ("12345", "", OWNER_PIN, "None", value):
                    response = self.client.post("/login", json={"pin": pin})
                    if pin and len(pin) <= auth.PIN_MAX_LENGTH:
                        self.assertEqual(response.status_code, 503, (value, pin))
                        self.assertIn(f"Nobody can sign in: {auth.PIN_VARIABLE}", response.json()["detail"])
                        self.assertIn(said, response.json()["detail"])
                    else:
                        self.assertEqual(response.status_code, 422)
                    self.assertFalse(self.signed_in())
        self.assertEqual(registry.audit_log(), [])

    def test_only_a_pin_as_text_is_looked_at(self):
        for body in ({"pin": int(OWNER_PIN)}, {"pin": [OWNER_PIN]}, {"pin": "9" * 65}, {},
                     {"pin": OWNER_PIN, "remember": True}):
            self.assertEqual(self.client.post("/login", json=body).status_code, 422, body)
        # The PIN goes in the request's body, never in its address, which servers write to their logs.
        self.assertEqual(self.client.post(f"/login?pin={OWNER_PIN}").status_code, 422)
        self.assertEqual(self.client.get(f"/login?pin={OWNER_PIN}").status_code, 405)
        self.assertFalse(self.signed_in())

    def test_a_request_with_mistakes_is_not_repeated_back_or_logged(self):
        half_a_pair = b"\\ud83d"  # Cannot be stored or sent as it is.
        typed = "8642" * 20
        with self.assertNoLogs(level="ERROR"):
            for body in (b'{"pin": "' + typed.encode() + b'"}',
                         b'{"pin": "' + half_a_pair + typed.encode()[:8] + b'"}',
                         b'{"pin": "1234", "' + half_a_pair + typed.encode()[:8] + b'": 1}',
                         b'{"pin": ' + typed.encode() + b'}'):
                response = self.client.post("/login", content=body, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, body)
                self.assertNotIn(typed[:8], response.text)
                self.assertEqual({"type", "msg", "loc"}, set(response.json()["detail"][0]))
        self.assertEqual(response.json()["detail"][0]["loc"], ["body", "pin"])
        self.assertFalse(self.signed_in())
        self.assertEqual(registry.audit_log(), [])

    def test_a_login_ends_after_eight_hours(self):
        self.login()
        self.later(hours=7, minutes=59)
        self.assertTrue(self.signed_in())
        self.later(minutes=1)
        self.assertFalse(self.signed_in())
        self.assertEqual(self.client.post("/logout").status_code, 401)
        # The next sign-in clears the ended one away.
        self.login()
        self.assertEqual(self.query("SELECT COUNT(*) FROM logins"), [(1,)])

    def test_two_browsers_each_have_their_own_login(self):
        other = self.new_client()
        self.login()
        self.login(client=other)
        self.assertNotEqual(self.client.cookies.get(auth.COOKIE), other.cookies.get(auth.COOKIE))
        self.client.post("/logout")
        self.assertEqual((self.signed_in(), self.signed_in(other)), (False, True))

    def test_no_log_row_no_login(self):
        with patch.object(registry, "_log", side_effect=sqlite3.OperationalError("disk I/O error")):
            response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("set-cookie", response.headers)
        # Nothing of the attempt was kept, the new table included.
        self.assertEqual(self.query("SELECT name FROM sqlite_master WHERE name = 'logins'"), [])
        self.assertFalse(self.signed_in())


class SignOutTests(AuthTestCase):
    def test_signing_out_ends_the_login(self):
        self.login()
        token = self.client.cookies.get(auth.COOKIE)
        response = self.client.post("/logout")
        self.assertEqual((response.status_code, response.json()), (200, {"signed_in": False}))
        self.assertIn("max-age=0", response.headers["set-cookie"].lower())
        self.assertFalse(self.signed_in())
        # A copy of the old cookie is worth nothing afterwards.
        thief = self.new_client()
        thief.cookies.set(auth.COOKIE, token)
        self.assertFalse(self.signed_in(thief))
        self.assertEqual(thief.post("/confirm", json={
            "confirmation_id": "any", "approved": True, "session_id": CHAT}).status_code, 401)
        self.assertEqual(self.owner_rows(), [("sign_in", "signed_in", "signed in for 8 hours"),
                                             ("sign_out", "signed_out", "signed out")])


class WrongPinLimitTests(AuthTestCase):
    def wrong(self, times, seconds_apart=10):
        codes = []
        for _ in range(times):
            codes.append(self.login(WRONG).status_code)
            self.later(seconds=seconds_apart)
        return codes

    def test_five_wrong_pins_block_the_right_one(self):
        self.assertEqual(self.wrong(4), [401] * 4)
        fifth = self.login(WRONG)
        self.assertEqual(fifth.status_code, 429)
        self.assertEqual(fifth.json()["detail"], "Wrong PIN. That was 5 wrong PINs within 5 minutes, "
                                                 "so every attempt is refused for 5 minutes.")
        self.later(seconds=1)
        blocked = self.login()
        self.assertEqual((blocked.status_code, blocked.json()),
                         (429, {"detail": "Too many wrong PINs. Try again in 5 minutes."}))
        self.assertEqual(blocked.headers["retry-after"], "299")
        self.assertNotIn("set-cookie", blocked.headers)
        self.assertFalse(self.signed_in())
        self.assertEqual(self.owner_rows(), [("sign_in", "refused", "wrong PIN")] * 4 + [
            ("sign_in", "refused", "wrong PIN; every attempt is refused for 5 minutes")])

    def test_the_block_ends_after_five_minutes(self):
        self.wrong(5)
        self.later(minutes=4)
        last_minute = self.login()
        self.assertEqual(last_minute.json(), {"detail": "Too many wrong PINs. Try again in 1 minute."})
        self.later(minutes=1)
        self.assertEqual(self.login().status_code, 200)
        self.assertTrue(self.signed_in())

    def test_attempts_made_during_the_block_add_no_rows_and_do_not_lengthen_it(self):
        self.wrong(5)
        before = registry.audit_log()
        for pin in (WRONG, OWNER_PIN) * 10:
            self.assertEqual(self.login(pin).status_code, 429)
        self.assertEqual(registry.audit_log(), before)
        self.later(minutes=5)
        self.assertEqual(self.login().status_code, 200)

    def test_wrong_pins_sent_at_the_same_moment_get_five_tries_between_them(self):
        def guess(_):
            try:
                auth._check_pin(WRONG, "sign_in", {}, wrong_status=401)
            except HTTPException as refused:
                return refused.status_code, refused.detail
            return 200, ""

        with ThreadPoolExecutor(max_workers=16) as together:
            answers = list(together.map(guess, range(40)))
        self.assertEqual(sorted(code for code, _ in answers), [401] * 4 + [429] * 36)
        self.assertEqual(sum(said == auth.NOW_BLOCKED for _, said in answers), 1)
        self.assertEqual(len(self.owner_rows()), 5)
        self.assertEqual(self.login().status_code, 429)
        self.assertTrue(registry.verify_log()["intact"])

    def test_no_way_of_spacing_attempts_gets_more_than_five_tries_in_five_minutes(self):
        """Two thousand attempts at sign-in and at unlock, in bursts, at a steady
        pace and just either side of the five minutes, with the right PIN now
        and then. Whatever the timing, a PIN is looked at five times in any
        five minutes at most, and the right PIN opens nothing while the block holds."""
        self.login()
        chance = random.Random(2026)
        looked_at = []  # When a PIN was compared with the owner's, right or wrong.
        for _ in range(2000):
            self.later(seconds=chance.choice((0, 0, 0, 1, 3, 59, 60, 61, 74, 75, 76, 299, 300, 301)))
            action, wrong_status = chance.choice((("sign_in", 401), ("unlock", 403)))
            right = chance.random() < 0.05
            blocked = auth._seconds_blocked(self.wrong_pin_times(), self.clock) > 0
            try:
                auth._check_pin(OWNER_PIN if right else WRONG, action, {}, wrong_status=wrong_status)
                answer = 200
            except HTTPException as refused:
                answer = refused.status_code if refused.detail != auth.NOW_BLOCKED else "fifth"
            if blocked:
                self.assertEqual(answer, 429, "an attempt got through while the block held")
                continue
            self.assertEqual(answer, 200 if right else answer)
            self.assertIn(answer, (200,) if right else (wrong_status, "fifth"))
            if not right:
                looked_at.append(self.clock)
        self.assertGreater(len(looked_at), 300)
        for index, moment in enumerate(looked_at):
            recent = [earlier for earlier in looked_at[:index + 1] if moment - earlier < timedelta(minutes=5)]
            self.assertLessEqual(len(recent), 5, f"six wrong PINs were looked at within five minutes of {moment}")
        # Every one of them is in the log, and nothing else is.
        self.assertEqual(len(looked_at), sum(decision == "refused" for _, decision, _ in self.owner_rows()))
        self.assertTrue(registry.verify_log()["intact"])

    def wrong_pin_times(self):
        with registry.owner_records() as (conn, _):
            return auth._wrong_pin_times(conn, auth.WRONG_PINS)

    def test_after_the_block_it_takes_five_new_wrong_pins_to_start_another(self):
        self.wrong(5)
        self.later(minutes=5)
        self.assertEqual(self.wrong(4), [401] * 4)
        self.assertEqual(self.login(WRONG).status_code, 429)

    def test_wrong_pins_spread_over_a_longer_time_do_not_block(self):
        self.assertEqual(self.wrong(8, seconds_apart=90), [401] * 8)
        self.assertEqual(self.login().status_code, 200)

    def test_the_count_is_read_from_the_log_so_a_restart_does_not_reset_it(self):
        # Five wrong PINs that only the database knows of, as after a restart.
        with registry.owner_records() as (_, log):
            for _ in range(5):
                log("sign_in", "refused", "wrong PIN")
        self.later(seconds=30)
        self.assertEqual(self.login(client=self.new_client()).status_code, 429)

    def test_a_right_pin_between_wrong_ones_does_not_reset_the_count(self):
        self.wrong(2)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.wrong(2), [401] * 2)
        self.assertEqual(self.login(WRONG).status_code, 429)
        # The block is on typing a PIN. The owner who signed in before it stays signed in.
        self.assertTrue(self.signed_in())

    def test_a_wrong_pin_at_unlock_counts_toward_the_same_limit(self):
        self.lock_the_chat()
        self.login()
        self.assertEqual([self.unlock(WRONG).status_code for _ in range(5)], [403] * 4 + [429])
        self.assertEqual(self.unlock().status_code, 429)
        self.assertEqual(self.login(client=self.new_client()).status_code, 429)
        self.assertNotIn(("unlock", "unlocked"), self.decisions())
        self.later(minutes=5)
        self.assertEqual(self.unlock().json(), {"status": "ok", "result": {"was_locked": True}})


class UnlockTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.lock_the_chat()
        locked = registry.execute("send_reminder", {"customer": "CL0001"}, CHAT)
        self.assertEqual(locked, {"status": "refused", "reason": registry.LOCKED})

    def test_the_right_pin_unlocks_the_chat(self):
        self.login()
        response = self.unlock()
        self.assertEqual((response.status_code, response.json()),
                         (200, {"status": "ok", "result": {"was_locked": True}}))
        self.assertEqual(self.decisions()[-1], ("unlock", "unlocked"))
        self.assertEqual(registry.execute("send_reminder", {"customer": "CL0001"}, CHAT)["status"],
                         "needs_confirmation")

    def test_a_wrong_pin_leaves_it_locked(self):
        self.login()
        response = self.unlock(WRONG)
        self.assertEqual((response.status_code, response.json()), (403, {"detail": "Wrong PIN."}))
        self.assertNotIn(("unlock", "unlocked"), self.decisions())
        self.assertEqual(registry.execute("send_reminder", {"customer": "CL0001"}, CHAT),
                         {"status": "refused", "reason": registry.LOCKED})
        attempt = registry.audit_log(registry.OWNER_SESSION)[-1]
        self.assertEqual((attempt["action"], attempt["level"], attempt["decision"], attempt["result"],
                          attempt["arguments"]),
                         ("unlock", "owner", "refused", "wrong PIN", {"session_id": CHAT}))
        # Still signed in: a wrong PIN here is not answered with "not signed in".
        self.assertTrue(self.signed_in())

    def test_the_pin_alone_without_a_login_does_not_unlock(self):
        self.assertEqual(self.unlock().status_code, 401)
        self.assertNotIn(("unlock", "unlocked"), self.decisions())

    def test_a_login_alone_without_the_pin_does_not_unlock(self):
        self.login()
        for body in ({"session_id": CHAT}, {"session_id": CHAT, "pin": ""},
                     {"session_id": CHAT, "pin": OWNER_PIN, "approved": True},
                     {"session_id": "not a chat's ID", "pin": OWNER_PIN}):
            self.assertEqual(self.client.post("/unlock", json=body).status_code, 422, body)
        self.assertNotIn(("unlock", "unlocked"), self.decisions())

    def test_nothing_the_model_asks_for_unlocks(self):
        # No action is called unlock, and an action's own code cannot reach the owner's rows.
        self.assertNotIn("unlock", [action["name"] for action in registry.list_actions()])
        self.assertEqual(registry.execute("unlock", {"pin": OWNER_PIN}, CHAT)["status"], "error")

        def forge(**_):
            with registry.owner_records() as (_, log):
                log("sign_in", "signed_in", "forged")
            return {}

        added = patch.dict(registry._actions)
        added.start()
        self.addCleanup(added.stop)
        registry.register(registry.Action("forge", "Test.", {"type": "object"}, "green", forge))
        with self.assertLogs(registry.logger, "ERROR"):
            self.assertEqual(registry.execute("forge", {}, CHAT), {"status": "error", "message": registry.FAILED})
        self.assertEqual(self.owner_rows(), [])
        self.assertNotIn(("unlock", "unlocked"), self.decisions())


class OtherWebsiteTests(AuthTestCase):
    FOREIGN = ("https://evil.example", "http://localhost.evil.example", "http://127.0.0.1.evil.example:8000",
               "http://localhost@evil.example", "null", "", "http://[localhost")
    LOCAL = ("http://localhost:5173", "http://127.0.0.1:5173", "http://127.0.0.1:8000", "http://LOCALHOST:5173")

    def test_a_page_on_another_website_cannot_sign_in_even_with_the_pin(self):
        for origin in self.FOREIGN:
            response = self.login(headers={"Origin": origin})
            self.assertEqual(response.status_code, 403, origin)
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(registry.audit_log(), [])

    def test_a_page_on_another_website_cannot_use_the_owners_login(self):
        self.login()
        waiting = registry.execute("send_reminder", {"customer": "CL0001"}, CHAT)
        press = {"confirmation_id": waiting["confirmation_id"], "approved": True, "session_id": CHAT}
        for origin in self.FOREIGN:
            self.assertEqual(self.client.post("/confirm", json=press, headers={"Origin": origin}).status_code,
                             403, origin)
            self.assertEqual(self.client.post("/logout", headers={"Origin": origin}).status_code, 403, origin)
        self.assertEqual(self.decisions(), [("send_reminder", "waiting")])
        self.assertTrue(self.signed_in())

    def test_the_local_screen_and_a_program_with_no_origin_are_let_through(self):
        for origin in self.LOCAL:
            self.assertEqual(self.login(headers={"Origin": origin}).status_code, 200, origin)
        self.assertEqual(self.login().status_code, 200)

    def test_a_json_request_is_the_only_kind_that_is_read(self):
        # A page elsewhere can send plain text without the browser asking the server first.
        self.login()
        waiting = registry.execute("send_reminder", {"customer": "CL0001"}, CHAT)
        press = ('{"confirmation_id": "%s", "approved": true, "session_id": "%s"}'
                 % (waiting["confirmation_id"], CHAT))
        for kind in ("text/plain", "application/x-www-form-urlencoded"):
            response = self.client.post("/confirm", content=press, headers={"Content-Type": kind})
            self.assertEqual(response.status_code, 422, kind)
        self.assertEqual(self.decisions(), [("send_reminder", "waiting")])


class LogTests(AuthTestCase):
    def test_every_step_leaves_a_row_and_the_log_stays_intact(self):
        self.lock_the_chat()
        self.login(WRONG)
        self.login()
        self.unlock(WRONG)
        self.unlock()
        self.client.post("/logout")
        self.assertEqual(self.owner_rows(), [
            ("sign_in", "refused", "wrong PIN"),
            ("sign_in", "signed_in", "signed in for 8 hours"),
            ("unlock", "refused", "wrong PIN"),
            ("sign_out", "signed_out", "signed out"),
        ])
        self.assertEqual({row["level"] for row in registry.audit_log(registry.OWNER_SESSION)}, {"owner"})
        self.assertEqual(self.decisions(), [("transfer_money", "refused")] * 3 + [("unlock", "unlocked")])
        check = registry.verify_log()
        self.assertEqual((check["intact"], check["rows"]), (True, 8))

    def test_the_owners_rows_do_not_count_toward_any_chats_lock(self):
        self.login()
        for _ in range(3):
            self.unlock(WRONG)
        self.assertEqual(registry.execute("send_reminder", {"customer": "CL0001"}, CHAT)["status"],
                         "needs_confirmation")
        # And no chat can be held under the owner's own session.
        self.assertEqual(registry.execute("get_balance", {}, registry.OWNER_SESSION)["status"], "error")
        self.assertEqual(self.client.post("/unlock", json={
            "session_id": registry.OWNER_SESSION, "pin": OWNER_PIN}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
