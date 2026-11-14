"""Round 4 retention and recovery checks, using only synthetic payloads."""
from datetime import datetime, timedelta
import json
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon
from . import test_review_a3 as review_a3
from ..models.calendar_event import CalDAVPartialSyncError

LOGGER = "odoo.addons.caldav_sync.models.calendar_event"


@tagged("post_install", "-at_install", "caldav_round4")
class TestCalDAVRound4Safety(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user("round4", "synthetic", "synthetic", "https://calendar.example.test/")
        cls.events = cls.env["calendar.event"]

    def setUp(self):
        super().setUp()
        self.calendar = MagicMock()
        self.calendar.get_properties.return_value = {}
        self.client = MagicMock()
        self.client.calendar.return_value = self.calendar
        patcher = patch.object(type(self.user), "_get_caldav_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    _component = review_a3.TestReviewA3._component
    _resource = review_a3.TestReviewA3._resource
    _ordinary = review_a3.TestReviewA3._ordinary
    _poll = review_a3.TestReviewA3._poll
    _detached = review_a3.TestReviewA3._detached
    _detached_master = review_a3.TestReviewA3._detached_master
    _ledger = review_a3.TestReviewA3._ledger

    def test_detached_rows_with_old_proof_never_enter_cleanup(self):
        first = datetime(2022, 5, 2, 9)
        rows = self._detached("protected-history", 35, first)
        # Model the rejected release having already recorded origin for them.
        state = {"created": {}, "archives": []}
        import hashlib
        for row in rows:
            state["created"][str(row.id)] = {
                "account": self.events._caldav_account_identity(self.user),
                "uid": hashlib.sha256(row.caldav_uid.encode()).hexdigest(),
                "at": fields.Datetime.to_string(fields.Datetime.now()),
            }
        self.events._caldav_save_safety_state(self.user, state)
        ordinary = self._ordinary(40)
        master = self._detached_master("protected-history", first, 2)
        self._poll(ordinary + [master])
        with self.assertLogs(LOGGER, "WARNING") as logs:
            self.assertIsNone(self._poll(ordinary))
        self.assertTrue(any("detached" in line and str(rows.ids[0]) in line for line in logs.output))
        with patch("odoo.addons.caldav_sync.models.calendar_event._logger.warning") as warning:
            for _ in range(6):
                self.assertIsNone(self._poll(ordinary))
        warning.assert_not_called()
        self.assertEqual(len(rows.exists().filtered("active")), 35)
        self.assertFalse(self._ledger()["archives"])
        self.assertEqual(self.env["mail.activity"].sudo().search_count([
            ("res_id", "=", self.user.partner_id.id),
            ("res_model", "=", "res.partner"),
            ("summary", "=", "CalDAV: vyžaduje kontrolu (detached)"),
        ]), 1)

    def test_archived_detached_49_restore_before_matching_is_idempotent(self):
        rows = self.events.browse()
        masters = []
        for index, size in enumerate((35, 2, 2, 2, 4, 4)):
            first = datetime(2022, 5, 2, 9) + timedelta(days=60 * index)
            uid = "restore-hist-%s" % index
            rows |= self._detached(uid, size, first)
            masters.append(self._detached_master(uid, first, 2))
        ordinary = self._ordinary(10)
        self._poll(ordinary + masters)
        baseline = rows.read(["user_id", "partner_ids", "caldav_uid", "caldav_recurrence_id", "recurrence_id"])
        rows.with_context(caldav_no_sync=True, no_microsoft_sync=True).write({"active": False})
        for poll in range(3):
            with self.subTest(poll=poll):
                self.assertIsNone(self._poll(ordinary + masters))
                self.assertEqual(len(rows.exists().filtered("active")), 49)
                self.assertEqual(rows.read(["user_id", "partner_ids", "caldav_uid", "caldav_recurrence_id", "recurrence_id"]), baseline)
        self.assertFalse(set(map(str, rows.ids)) & set(self._ledger()["created"]))
        # Direct import exercises the same path without the poll's inactive cache.
        self.assertEqual(set(self.events._sync_event_from_ical(masters[0].icalendar_instance, self.user).ids), set(rows[:35].ids))

    def test_relative_drop_cannot_become_success_on_later_polls(self):
        ordinary = self._ordinary(12)
        self._poll(ordinary)
        for _ in range(6):
            outcome = self._poll(ordinary[:2])
            self.assertIsInstance(outcome, CalDAVPartialSyncError)
            self.assertEqual(self.events.search_count([("user_id", "=", self.user.id), ("caldav_uid", "like", "ord-%")]), 12)
        self.assertFalse(self._ledger()["archives"])

    def test_exact_half_listing_still_allows_small_confirmed_cleanup(self):
        ordinary = self._ordinary(10)
        self._poll(ordinary)
        self.assertIsNone(self._poll(ordinary[:5]))
        self.assertEqual(self.events.search_count([("user_id", "=", self.user.id), ("caldav_uid", "like", "ord-%")]), 5)

    def test_proof_survives_active_age_and_prunes_only_gone_or_old_inactive(self):
        ordinary = self._ordinary(4)
        self._poll(ordinary)
        rows = self.events.search([("user_id", "=", self.user.id), ("caldav_uid", "like", "ord-%")], order="id")
        active, recent, old, gone = rows
        now = fields.Datetime.now()
        state = self._ledger()
        for entry in state["created"].values():
            entry["at"] = fields.Datetime.to_string(now - timedelta(days=200))
        state["positions"] = {str(row.id): {"id": row.id, "stored": "synthetic"} for row in rows}
        (recent | old).with_context(caldav_no_sync=True, no_microsoft_sync=True).write({"active": False})
        self.env.flush_all()
        self.env.cr.execute("UPDATE calendar_event SET write_date=%s WHERE id=%s", (now - timedelta(days=91), old.id))
        self.env.cr.execute("UPDATE calendar_event SET write_date=%s WHERE id=%s", (now - timedelta(days=89), recent.id))
        rows.invalidate_recordset()
        gone_id = gone.id
        gone.with_context(caldav_no_sync=True, no_microsoft_sync=True).unlink()
        self.env["ir.config_parameter"].sudo().set_param("caldav_sync.safety.%s" % self.user.id, json.dumps(state))
        pruned = self._ledger()
        self.assertEqual(set(pruned["created"]), {str(active.id), str(recent.id)})
        self.assertEqual(set(pruned["positions"]), {str(active.id), str(recent.id)})
        self.assertNotIn(str(gone_id), pruned["created"])

    def test_notice_keys_clear_on_recovery_and_cache_keeps_active_review(self):
        ordinary = self._ordinary(12)
        self._poll(ordinary)
        self._poll(ordinary[:2])
        params = self.env["ir.config_parameter"].sudo()
        domain = [("key", "=like", "caldav_sync.review.%s.%%" % self.user.id)]
        self.assertEqual(params.search_count(domain), 1)
        self._poll(ordinary)
        self.assertEqual(params.search_count(domain), 0)
        # A real COUNT change stays unimplemented and visible through cache
        # hits; its notice must not disappear until the rule is restored.
        master = self._component("notice-series", datetime(2030, 3, 4, 9))
        master.add("rrule", {"freq": "weekly", "count": 3})
        self._poll(ordinary + [self._resource([master])])
        changed = self._component("notice-series", datetime(2030, 3, 4, 9))
        changed.add("rrule", {"freq": "weekly", "count": 6})
        with self.assertLogs(LOGGER, "WARNING"):
            self._poll(ordinary + [self._resource([changed])])
        with patch("odoo.addons.caldav_sync.models.calendar_event._logger.warning") as warning:
            self._poll(ordinary + [self._resource([changed])])
        warning.assert_not_called()
        self.assertEqual(params.search_count(domain), 1)
        self._poll(ordinary + [self._resource([master])])
        self.assertEqual(params.search_count(domain), 0)

    def test_unproven_local_rewrite_never_adopts_origin(self):
        ordinary = self._ordinary(30)
        master = self._component("unproven-rewrite", datetime(2030, 3, 4, 9))
        master.add("rrule", {"freq": "weekly", "count": 4})
        self._poll(ordinary + [self._resource([master])])
        rows = self.events.search([("caldav_uid", "=", "unproven-rewrite")], order="start")
        params = self.env["ir.config_parameter"].sudo()
        state = self._ledger()
        for row in rows:
            state["created"].pop(str(row.id), None)
        params.set_param("caldav_sync.safety.%s" % self.user.id, json.dumps(state))
        with patch.object(type(self.events), "_sync_unlink_to_caldav"), \
                patch.object(type(self.events), "_sync_write_to_caldav"):
            rows[0].write({"start": rows[0].start + timedelta(hours=2),
                           "stop": rows[0].stop + timedelta(hours=2),
                           "recurrence_update": "all_events"})
        regenerated = self.events.search([("user_id", "=", self.user.id), ("recurrence_id", "!=", False)])
        self.assertFalse(set(map(str, regenerated.ids)) & set(self._ledger()["created"]))
        self._poll(ordinary)
        self.assertEqual(len(regenerated.filtered("active")), 4)

    def test_old_approval_cannot_release_detached_rows(self):
        rows = self._detached("detached-approved", 4, datetime(2022, 5, 2, 9))
        ordinary = self._ordinary(30)
        self._poll(ordinary + [self._detached_master("detached-approved", rows[0].start, 2)])
        # Approval never bypasses the candidate exclusion. Even a stale
        # rejected-release signature cannot make these rows eligible.
        state = self._ledger()
        state["approval"] = "stale-rejected-release-signature"
        self.events._caldav_save_safety_state(self.user, state)
        self.assertIsNone(self._poll(ordinary))
        self.assertEqual(len(rows.filtered("active")), 4)
        self.assertFalse(self._ledger()["archives"])
