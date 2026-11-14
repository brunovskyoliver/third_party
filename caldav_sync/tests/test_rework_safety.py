"""Post-review safety regressions. Synthetic only; no network access."""
from datetime import datetime, timedelta, UTC
import json
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .test_detached_safety import TestCalDAVDetachedSafety, LOGGER
from ..models.calendar_event import CalDAVPartialSyncError


@tagged("post_install", "-at_install", "caldav_rework")
class TestCalDAVReworkSafety(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from .common import CaldavTestCommon
        with patch("caldav.DAVClient"):
            cls.user = CaldavTestCommon._generate_user.__func__(cls, "rework-safety", "synthetic", "synthetic", "https://calendar.example.test/")
        cls.events = cls.env["calendar.event"]

    def setUp(self):
        super().setUp()
        from unittest.mock import MagicMock
        self.calendar = MagicMock()
        self.calendar.get_properties.return_value = {}
        self.client = MagicMock()
        self.client.calendar.return_value = self.calendar
        patcher = patch.object(type(self.user), "_get_caldav_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
    _component = TestCalDAVDetachedSafety._component
    _resource = TestCalDAVDetachedSafety._resource
    _poll = TestCalDAVDetachedSafety._poll
    _ordinary_population = TestCalDAVDetachedSafety._ordinary_population

    def test_recurring_omission_eight_polls_zero_archives(self):
        resources, ordinary = self._ordinary_population()
        master = self._component("eight-polls")
        master.add("rrule", {"freq": "daily", "count": 6})
        series_resource = self._resource([master])
        self._poll(resources + [series_resource])
        series = self.events.search([("caldav_uid", "=", "eight-polls")])
        self.assertEqual(len(series), 6)
        for index in range(8):
            with self.subTest(poll=index):
                outcome = self._poll(resources)
            self.assertIsNone(outcome)
            self.assertEqual(len(series.exists().filtered("active")), 6)
            self.assertEqual(len(ordinary.filtered("active")), 40)
        self.assertEqual(self.events._caldav_safety_state(self.user)["archives"], [])

    def test_archived_series_reappears_four_existing_ids_visible(self):
        master = self._component("restore-series")
        master.add("rrule", {"freq": "daily", "count": 4})
        resource = self._resource([master])
        self._poll([resource])
        series = self.events.search([("caldav_uid", "=", "restore-series")])
        original_ids = set(series.ids)
        original_recurrence = series.recurrence_id
        base = original_recurrence.base_event_id
        base.with_context(caldav_no_sync=True, no_microsoft_sync=True).write({
            "active": False, "recurrence_update": "self_only",
        })
        self.assertEqual(len(series.filtered("active")), 3)
        self._poll([resource])
        self.assertEqual(set(series.exists().filtered("active").ids), original_ids)
        self.assertEqual(series.recurrence_id, original_recurrence)
        self.assertEqual(self.events.search_count([("caldav_uid", "=", "restore-series")]), 4)

    def test_remote_reappearance_restores_plain_id_and_never_refunds_budget(self):
        resources, events = self._ordinary_population()
        original = events.filtered(lambda event: event.caldav_uid == "ordinary-39")
        for run in range(20):
            with self.subTest(run=run), self.assertLogs(LOGGER, "WARNING"):
                self.assertIsNone(self._poll(resources[:-1]))
            self.assertFalse(original.active)
            # A new model/env view sees persisted history, not a Python counter.
            state = self.env["calendar.event"].with_context(fresh_view=run)._caldav_safety_state(self.user)
            self.assertEqual(sum(len(entry["ids"]) for entry in state["archives"]), run + 1)
            self._poll(resources)
            self.assertTrue(original.active)
            self.assertEqual(self.events.search([("caldav_uid", "=", "ordinary-39")]), original)
        with self.assertLogs(LOGGER, "WARNING"):
            outcome = self._poll(resources[:-1])
        self.assertIsInstance(outcome, CalDAVPartialSyncError)
        self.assertTrue(original.active)
        state = self.events._caldav_safety_state(self.user)
        self.assertEqual(sum(len(entry["ids"]) for entry in state["archives"]), 20)

    def test_rolling_budget_expires_after_twenty_four_hours(self):
        resources, events = self._ordinary_population()
        now = fields.Datetime.now()
        state = self.events._caldav_safety_state(self.user)
        state["archives"] = [{"at": fields.Datetime.to_string(now), "ids": list(range(20)), "uids": [str(index) for index in range(20)]}]
        self.events._caldav_save_safety_state(self.user, state)
        with self.assertLogs(LOGGER, "WARNING"):
            self.assertIsInstance(self._poll(resources[:-1]), CalDAVPartialSyncError)
        self.assertEqual(len(events.filtered("active")), 40)
        with patch.object(fields.Datetime, "now", return_value=now + timedelta(hours=24, seconds=1)):
            with self.assertLogs(LOGGER, "WARNING"):
                self.assertIsNone(self._poll(resources[:-1]))
        self.assertEqual(len(events.filtered("active")), 39)

    def test_origin_is_not_inferred_from_owner_or_cache_membership(self):
        resources, events = self._ordinary_population()
        state = self.events._caldav_safety_state(self.user)
        state["created"] = {}
        self.events._caldav_save_safety_state(self.user, state)
        with self.assertLogs(LOGGER, "WARNING"):
            outcome = self._poll(resources[:-1])
        self.assertIsNone(outcome)
        self.assertEqual(len(events.filtered("active")), 40)

    def test_account_change_cannot_reset_archive_budget_or_origin(self):
        resources, events = self._ordinary_population()
        state = self.events._caldav_safety_state(self.user)
        state["archives"] = [{"at": fields.Datetime.to_string(fields.Datetime.now()), "ids": list(range(20)), "uids": [str(index) for index in range(20)]}]
        self.events._caldav_save_safety_state(self.user, state)
        self.user.write({"caldav_username": "changed-synthetic-account"})
        with self.assertLogs(LOGGER, "WARNING"):
            outcome = self._poll(resources[:-1])
        self.assertIsNone(outcome)
        self.assertEqual(len(events.filtered("active")), 40)
        self.assertEqual(len(self.events._caldav_safety_state(self.user)["archives"][0]["uids"]), 20)

    def test_corrupt_archive_history_fails_closed(self):
        resources, events = self._ordinary_population()
        parameters = self.env["ir.config_parameter"].sudo()
        parameters.set_param("caldav_sync.safety.%s" % self.user.id, json.dumps({"created": {}, "archives": [{"at": "invalid", "ids": [], "uids": []}]}))
        with self.assertLogs(LOGGER, "WARNING"):
            outcome = self._poll(resources[:-1])
        self.assertIsInstance(outcome, CalDAVPartialSyncError)
        self.assertTrue(all(events.mapped("active")))

    def test_losing_rrule_cannot_unlink_or_hide_series(self):
        resources, ordinary = self._ordinary_population()
        master = self._component("lost-rrule")
        master.add("rrule", {"freq": "daily", "count": 4})
        self._poll(resources + [self._resource([master])])
        series = self.events.search([("caldav_uid", "=", "lost-rrule")])
        original_ids = set(series.ids)
        master.pop("rrule")
        with patch.object(type(self.events), "_sync_unlink_to_caldav"), \
                patch.object(type(self.events), "_sync_write_to_caldav"), \
                self.assertLogs(LOGGER, "WARNING") as logs:
            outcome = self._poll(resources + [self._resource([master])])
        self.assertIsNone(outcome)
        self.assertTrue(any("review" in line.lower() for line in logs.output))
        self.assertFalse(any("ERROR" in line for line in logs.output))
        self.assertTrue(self.env["mail.activity"].sudo().search_count([
            ("res_id", "=", self.user.partner_id.id),
            ("summary", "=", "CalDAV: vyžaduje kontrolu (recurrence)"),
        ]))
        self.assertEqual(set(series.exists().filtered("active").ids), original_ids)
        self.assertEqual(len(ordinary.filtered("active")), 40)

    def test_changed_rrule_cannot_replace_series(self):
        master = self._component("changed-rrule")
        master.add("rrule", {"freq": "daily", "count": 4})
        self._poll([self._resource([master])])
        series = self.events.search([("caldav_uid", "=", "changed-rrule")])
        original_ids = set(series.ids)
        changed_master = self._component("changed-rrule")
        changed_master.add("rrule", {"freq": "weekly", "count": 4, "interval": 2})
        with patch.object(type(self.events), "_sync_unlink_to_caldav"), \
                patch.object(type(self.events), "_sync_write_to_caldav"), \
                self.assertLogs(LOGGER, "WARNING") as logs:
            outcome = self._poll([self._resource([changed_master])])
        self.assertIsNone(outcome)
        self.assertTrue(any("review" in line.lower() for line in logs.output))
        self.assertFalse(any("ERROR" in line for line in logs.output))
        self.assertTrue(self.env["mail.activity"].sudo().search_count([
            ("res_id", "=", self.user.partner_id.id),
            ("summary", "=", "CalDAV: vyžaduje kontrolu (recurrence)"),
        ]))
        self.assertEqual(set(series.exists().filtered("active").ids), original_ids)
        self.assertEqual(self.events.search_count([("caldav_uid", "=", "changed-rrule")]), 4)

    def test_confirmed_series_deletion_archives_all_and_stays_archived(self):
        from caldav.lib.error import NotFoundError
        resources, ordinary = self._ordinary_population()
        master = self._component("deleted-unit")
        master.add("rrule", {"freq": "daily", "count": 6})
        self._poll(resources + [self._resource([master])])
        series = self.events.search([("caldav_uid", "=", "deleted-unit")])
        self.calendar.event_by_uid.side_effect = NotFoundError("Synthetic confirmed deletion")
        for poll in range(8):
            with self.subTest(poll=poll):
                self._poll(resources)
                self.assertEqual(len(series.exists()), 6)
                self.assertFalse(series.filtered("active"))
                self.assertEqual(len(ordinary.filtered("active")), 40)
        self.assertEqual(sum(len(entry["uids"]) for entry in self.events._caldav_safety_state(self.user)["archives"]), 1)
        self.calendar.event_by_uid.side_effect = None
        self.assertIsNone(self._poll(resources + [self._resource([master])]))
        self.assertEqual(len(series.exists().filtered("active")), 6)
        self.assertEqual(self.events.search([("caldav_uid", "=", "deleted-unit")]), series)

    def test_failed_collection_or_resource_never_archives_series(self):
        from requests.exceptions import ReadTimeout
        master = self._component("incomplete-series")
        master.add("rrule", {"freq": "daily", "count": 6})
        resource = self._resource([master])
        self._poll([resource])
        series = self.events.search([("caldav_uid", "=", "incomplete-series")])
        self.calendar.events.side_effect = ReadTimeout("Synthetic fetch failure")
        for run in range(6):
            with self.assertRaises(ReadTimeout):
                self._poll([])
            self.assertEqual(len(series.filtered("active")), 6)
        self.calendar.events.side_effect = None
        with patch.object(type(self.events), "_sync_event_from_ical", side_effect=ValueError("Synthetic malformed resource")):
            for run in range(6):
                with self.assertLogs(LOGGER, "ERROR"):
                    self._poll([self._resource([self._component("broken-resource")])])
                self.assertEqual(len(series.filtered("active")), 6)

    def test_admin_approval_releases_exact_pending_set_once(self):
        from odoo.exceptions import AccessError, UserError
        resources, events = self._ordinary_population(60)
        with self.assertLogs(LOGGER, "WARNING"):
            self._poll(resources[:-25])
        with self.assertRaises(AccessError):
            self.events.with_user(self.user)._caldav_approve_cleanup(self.user)
        action = self.env.ref("caldav_sync.action_approve_caldav_cleanup")
        self.assertIn(self.env.ref("base.group_system"), action.group_ids)
        action.with_context(active_model="res.users", active_id=self.user.id, active_ids=[self.user.id]).run()
        with self.assertLogs(LOGGER, "WARNING"):
            self.assertIsNone(self._poll(resources[:-25]))
        self.assertEqual(len(events.filtered("active")), 35)
        self.assertEqual(len(events.exists()), 60)
        state = self.events._caldav_safety_state(self.user)
        self.assertNotIn("approval", state)
        self.assertNotIn("pending", state)
        with self.assertRaises(UserError):
            self.events._caldav_approve_cleanup(self.user)
        self._poll(resources)
        self.assertEqual(len(events.filtered("active")), 60)
        with self.assertLogs(LOGGER, "WARNING"):
            self.assertIsInstance(self._poll(resources[:-25]), CalDAVPartialSyncError)
        self.assertEqual(len(events.filtered("active")), 60)

    def test_approval_cannot_release_changed_set_or_edited_rows(self):
        resources, events = self._ordinary_population(60)
        with self.assertLogs(LOGGER, "WARNING"):
            self._poll(resources[:-25])
        self.events._caldav_approve_cleanup(self.user)
        events[-1].with_context(caldav_no_sync=True, no_microsoft_sync=True).write({"name": "Locally edited pending row"})
        with self.assertLogs(LOGGER, "WARNING"):
            self.assertIsInstance(self._poll(resources[:-25]), CalDAVPartialSyncError)
        self.assertEqual(len(events.filtered("active")), 60)
        self.events._caldav_approve_cleanup(self.user)
        with self.assertLogs(LOGGER, "WARNING"):
            self.assertIsInstance(self._poll(resources[:1]), CalDAVPartialSyncError)
        self.assertEqual(len(events.filtered("active")), 60)

    def test_dst_thirty_five_weekly_occurrences_keep_original_ids(self):
        import pytz
        zone = pytz.timezone("Europe/Bratislava")
        first = datetime(2022, 5, 6, 11)
        first_utc = zone.localize(first).astimezone(UTC).replace(tzinfo=None)
        vals, components = [], []
        for index in range(35):
            local_position = first + timedelta(weeks=index)
            utc_start = zone.localize(local_position).astimezone(UTC).replace(tzinfo=None)
            legacy_position = datetime.combine(utc_start.date(), first_utc.time())
            vals.append({"name": "Synthetic meeting", "start": utc_start, "stop": utc_start + timedelta(hours=1),
                         "event_tz": "Europe/Bratislava", "user_id": self.user.id,
                         "partner_ids": [(6, 0, self.user.partner_id.ids)], "caldav_uid": "dst-35",
                         "caldav_recurrence_id": legacy_position})
            component = self._component("dst-35", title="Local override")
            component.pop("dtstart"); component.pop("dtend")
            component.add("dtstart", zone.localize(local_position + timedelta(hours=1)))
            component.add("dtend", zone.localize(local_position + timedelta(hours=2)))
            component.add("recurrence-id", zone.localize(local_position))
            components.append(component)
        series = self.events.with_context(caldav_no_sync=True, no_microsoft_sync=True).create(vals)
        self.env.cr.execute("UPDATE calendar_event SET recurrency=true WHERE id=ANY(%s)", (series.ids,))
        series.invalidate_recordset()
        ids = set(series.ids)
        # Overrides only. The wire payload retains TZID and crosses summer/winter.
        payload = self._resource(components).icalendar_instance
        imported = self.events._sync_event_from_ical(payload, self.user)
        self.assertEqual(set(imported.ids), ids)
        self.assertEqual(len(series.filtered("active")), 35)
        for event, original in zip(series, vals):
            self.assertEqual(event.start, original["start"] + timedelta(hours=1))
            self.assertEqual(event.caldav_recurrence_id, original["caldav_recurrence_id"])
            self.assertEqual(event.partner_ids, self.user.partner_id)
        # The fallback must remain stable after DTSTART has already moved.
        # A second override keeps RECURRENCE-ID but moves DTSTART again.
        for component in components:
            component["dtstart"].dt += timedelta(hours=1)
            component["dtend"].dt += timedelta(hours=1)
        imported = self.events._sync_event_from_ical(self._resource(components).icalendar_instance, self.user)
        self.assertEqual(set(imported.ids), ids)
        for event, original in zip(series, vals):
            self.assertEqual(event.start, original["start"] + timedelta(hours=2))
            self.assertEqual(event.caldav_recurrence_id, original["caldav_recurrence_id"])

    def test_source_mirror_check_precedes_multi_match_guard(self):
        events = self.events.with_context(caldav_no_sync=True, no_microsoft_sync=True).create([
            {"name": "Mirror", "start": datetime(2030, 1, 1, 9), "stop": datetime(2030, 1, 1, 10), "caldav_uid": "mirror-duplicate"},
            {"name": "Duplicate", "start": datetime(2030, 1, 1, 9), "stop": datetime(2030, 1, 1, 10), "caldav_uid": "mirror-duplicate"},
        ])
        with patch.object(type(self.events), "_get_existing_instance", return_value=events), \
                patch.object(type(self.events), "_caldav_preserves_local_event", return_value=True):
            imported = self.events._sync_event_from_ical(self._resource([self._component("mirror-duplicate")]).icalendar_instance, self.user)
        self.assertEqual(imported, events)

    def test_committed_stop_rolls_back_running_account_and_stops_next(self):
        # The stop flag must be visible even after this transaction has read
        # its original snapshot. Never commit the TransactionCase's cursor.
        key = "caldav_sync.stop_requested"
        with self.env.registry.cursor() as cursor:
            cursor.execute("SELECT value FROM ir_config_parameter WHERE key=%s", (key,))
            previous = cursor.fetchone()
        from .common import CaldavTestCommon
        with patch("caldav.DAVClient"):
            other = CaldavTestCommon._generate_user.__func__(
                self, "stop-next", "synthetic", "synthetic", "https://calendar.example.test/next/",
            )
        self.env["res.users"].search([
            ("is_caldav_enabled", "=", True), ("id", "not in", (self.user | other).ids),
        ]).write({"caldav_username": False})
        called = []
        original_health = self.user._caldav_health()

        def running_poll(user):
            called.append(user.id)
            self.events.with_context(caldav_no_sync=True, no_microsoft_sync=True).create({
                "name": "Must roll back on emergency stop", "caldav_uid": "stop-rollback",
                "start": datetime(2030, 1, 1, 9), "stop": datetime(2030, 1, 1, 10),
                "user_id": user.id,
            })
            with self.env.registry.cursor() as cursor:
                cursor.execute("""INSERT INTO ir_config_parameter (key,value) VALUES (%s,'true')
                    ON CONFLICT (key) DO UPDATE SET value='true'""", (key,))
                cursor.commit()
            self.events._caldav_check_stop()

        try:
            with patch.object(type(self.events), "_poll_user_caldav_server", side_effect=running_poll), \
                    self.assertLogs(LOGGER, "WARNING"):
                self.events.poll_caldav_server()
            self.assertEqual(called, [self.user.id])
            self.assertFalse(self.events.search([("caldav_uid", "=", "stop-rollback")]))
            self.assertEqual(self.user._caldav_health(), original_health)
            self.assertEqual(other._caldav_health(), {})
        finally:
            with self.env.registry.cursor() as cursor:
                if previous:
                    cursor.execute("UPDATE ir_config_parameter SET value=%s WHERE key=%s", (previous[0], key))
                else:
                    cursor.execute("DELETE FROM ir_config_parameter WHERE key=%s", (key,))
                cursor.commit()
