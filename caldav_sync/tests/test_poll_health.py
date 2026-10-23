from datetime import timedelta
from unittest.mock import patch

from caldav.lib.error import AuthorizationError
from requests.exceptions import ConnectionError as RequestsConnectionError, ReadTimeout

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon

SECRET = "synthetic-health-secret"
EVENT_LOGGER = "odoo.addons.caldav_sync.models.calendar_event"


@tagged("post_install", "-at_install")
class TestCaldavPollHealth(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user(
                "caldav-health", "synthetic", SECRET, "https://calendar.example.test/one"
            )
            cls.other = cls._generate_user(
                "caldav-health-other", "synthetic", SECRET, "https://calendar.example.test/two"
            )
        cls.events = cls.env["calendar.event"]
        cls.Users = type(cls.env["res.users"])
        cls.parameters = cls.env["ir.config_parameter"].sudo()
        # Only the synthetic accounts take part in these polls.
        cls.env["res.users"].search([
            ("is_caldav_enabled", "=", True), ("id", "not in", (cls.user | cls.other).ids),
        ]).write({"caldav_username": False})

    def _poll(self, failing=(), error=None, events=None):
        error = error or RequestsConnectionError("synthetic outage")
        called = []

        def poll_user(user):
            called.append(user.id)
            if user in failing:
                raise error
            return True

        with patch.object(type(self.events), "_poll_user_caldav_server", side_effect=poll_user):
            (self.events if events is None else events).poll_caldav_server()
        return called

    def _make_due(self, user):
        health = user._caldav_health()
        health["next_retry"] = fields.Datetime.to_string(
            fields.Datetime.now() - timedelta(seconds=1)
        )
        user._caldav_set_health(health)

    def test_creating_a_configured_user_enables_it(self):
        # setUpClass creates the accounts with a password in create().
        self.assertTrue(self.user.is_caldav_enabled)
        self.assertTrue(self.other.is_caldav_enabled)

    def test_unreachable_server_does_not_disable_the_account(self):
        with patch("caldav.DAVClient") as client, \
                self.assertLogs("odoo.addons.caldav_sync.models.res_users", "WARNING"):
            client.return_value.principal.side_effect = ReadTimeout("synthetic timeout")
            self.user._compute_is_caldav_enabled()
        self.assertTrue(self.user.is_caldav_enabled)
        with patch("caldav.DAVClient") as client, \
                self.assertLogs("odoo.addons.caldav_sync.models.res_users", "ERROR"):
            client.return_value.principal.side_effect = AuthorizationError("synthetic")
            self.user._compute_is_caldav_enabled()
        self.assertFalse(self.user.is_caldav_enabled)

    def test_transient_failure_keeps_account_and_backs_off(self):
        with self.assertLogs(EVENT_LOGGER, "WARNING"):
            called = self._poll(failing=self.user)
        self.assertCountEqual(called, (self.user | self.other).ids)
        health = self.user._caldav_health()
        self.assertTrue(self.user.is_caldav_enabled)
        self.assertEqual(health["streak"], 1)
        self.assertEqual(health["failure_class"], "ConnectionError")
        self.assertNotIn("last_success", health)
        self.assertGreater(fields.Datetime.to_datetime(health["next_retry"]), fields.Datetime.now())
        stored = self.parameters.get_param(self.user._caldav_health_key())
        self.assertNotIn("synthetic outage", stored)
        self.assertNotIn(SECRET, stored)
        self.assertNotIn("example.test", stored)
        self.assertTrue(self.other._caldav_health().get("last_success"))

        # Within the backoff the account is not polled. Outbound pushes go on:
        # an unpushed event would be deleted as an orphan by the next poll.
        self.assertEqual(self._poll(), [self.other.id])
        self.assertFalse(self.user._caldav_retry_due())
        event = self.events.with_context(caldav_no_sync=True).create({
            "name": "Synthetic backoff event", "user_id": self.user.id,
            "start": "2030-01-15 09:00:00", "stop": "2030-01-15 10:00:00",
        })
        self.assertTrue(event._is_caldav_enabled())
        self.assertIn(self.user, event.caldav_user_ids)

        # Once due it is retried; a second failure doubles the wait.
        def wait(user):
            health = user._caldav_health()
            return (fields.Datetime.to_datetime(health["next_retry"])
                    - fields.Datetime.to_datetime(health["last_failure"]))

        first_wait = wait(self.user)
        self._make_due(self.user)
        with self.assertLogs(EVENT_LOGGER, "WARNING"):
            self._poll(failing=self.user, error=AuthorizationError("synthetic"))
        self.assertEqual(self.user._caldav_health()["streak"], 2)
        self.assertEqual(self.user._caldav_health()["failure_class"], "AuthorizationError")
        self.assertEqual(wait(self.user), first_wait * 2)

        # Recovery clears the failure state.
        self._make_due(self.user)
        self._poll()
        self.assertEqual(list(self.user._caldav_health()), ["last_success"])
        self.assertTrue(self.user._caldav_retry_due())

    def test_backoff_is_capped(self):
        self.user._caldav_set_health({"streak": 40})
        self.user._caldav_record_poll(RequestsConnectionError())
        health = self.user._caldav_health()
        self.assertEqual(
            fields.Datetime.to_datetime(health["next_retry"])
            - fields.Datetime.to_datetime(health["last_failure"]),
            timedelta(hours=6),
        )

    def test_healthy_polls_do_not_rewrite_the_parameter(self):
        self._poll()
        stored = self.parameters.get_param(self.user._caldav_health_key())
        with patch.object(type(self.parameters), "set_param") as set_param:
            self._poll()
        set_param.assert_not_called()
        self.assertEqual(self.parameters.get_param(self.user._caldav_health_key()), stored)

    def test_unexpected_error_is_isolated_and_recorded_without_backoff(self):
        with self.assertLogs(EVENT_LOGGER, "ERROR"):
            called = self._poll(failing=self.user, error=ValueError("synthetic bug"))
        self.assertCountEqual(called, (self.user | self.other).ids)
        health = self.user._caldav_health()
        self.assertEqual(health["failure_class"], "ValueError")
        self.assertNotIn("next_retry", health)
        self.assertTrue(self.user._caldav_retry_due())
        self.assertTrue(self.other._caldav_health().get("last_success"))

    def test_busy_account_is_not_recorded(self):
        with patch.object(type(self.events), "_poll_user_caldav_server", return_value=False):
            self.events.poll_caldav_server()
        self.assertEqual(self.user._caldav_health(), {})

    def test_new_settings_clear_backoff(self):
        self.user._caldav_record_poll(RequestsConnectionError())
        self.assertFalse(self.user._caldav_retry_due())
        with patch("caldav.DAVClient"):
            self.user.with_user(self.user).write({"caldav_password": "replacement-" + SECRET})
        self.assertEqual(self.user._caldav_health(), {})
        self.assertTrue(self.user._caldav_retry_due())

    def test_budget_stops_new_accounts_and_next_run_resumes(self):
        first, second = sorted(self.user | self.other, key=lambda user: user.id)
        clock = iter([0.0, 0.0, 999.0])
        with patch(EVENT_LOGGER + ".monotonic", side_effect=lambda: next(clock)), \
                self.assertLogs(EVENT_LOGGER, "INFO"):
            self.assertEqual(self._poll(), [first.id])
        self.assertEqual(self.parameters.get_param("caldav_sync.poll_cursor"), str(second.id))
        self.assertEqual(self._poll(), [second.id, first.id])
        self.assertFalse(self.parameters.get_param("caldav_sync.poll_cursor"))

    def test_cron_commits_each_account_and_reports_nothing_remaining(self):
        progress = []

        def commit_progress(cron, processed=0, *, remaining=None, deactivate=False):
            progress.append((processed, remaining))
            # Odoo gives a job only ~10 s of "time left"; the poll must not stop on it.
            return 0.0

        with patch.object(type(self.env["ir.cron"]), "_commit_progress", commit_progress):
            called = self._poll(events=self.events.with_context(ir_cron_progress_id=1))
        self.assertCountEqual(called, (self.user | self.other).ids)
        self.assertEqual(progress, [(1, 0), (1, 0)])
        self.assertFalse(self.parameters.get_param("caldav_sync.poll_cursor"))

    def test_health_is_admin_only(self):
        self.user._caldav_record_poll(RequestsConnectionError())
        with self.assertRaises(AccessError):
            self.env["ir.config_parameter"].with_user(self.user).get_param(
                self.user._caldav_health_key()
            )
        # The owner can still read their own retry state through the model.
        self.assertFalse(self.user.with_user(self.user)._caldav_retry_due())
