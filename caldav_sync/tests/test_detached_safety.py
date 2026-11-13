"""Synthetic data only. Every remote operation is mocked."""
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import caldav
import icalendar

from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon

LOGGER = "odoo.addons.caldav_sync.models.calendar_event"


@tagged("post_install", "-at_install")
class TestCalDAVDetachedSafety(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user("detached-safety", "synthetic", "synthetic", "https://calendar.example.test/")
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

    def _component(self, uid, position=None, start=None, title="Synthetic meeting"):
        start = start or datetime(2030, 1, 15, 9)
        component = icalendar.Event()
        component.add("uid", uid)
        component.add("summary", title)
        component.add("dtstart", start.replace(tzinfo=UTC))
        component.add("dtend", (start + timedelta(hours=1)).replace(tzinfo=UTC))
        component.add("dtstamp", datetime(2040, 1, 1, tzinfo=UTC))
        if position is not None:
            component.add("recurrence-id", position.replace(tzinfo=UTC))
        component.add("attendee", "mailto:" + self.user.partner_id.email)
        return component

    def _resource(self, components):
        payload = icalendar.Calendar()
        for component in components:
            payload.add_component(component)
        uid = str(components[0]["uid"])
        return caldav.Event(data=payload.to_ical(), url="https://calendar.example.test/%s.ics" % uid)

    def _poll(self, resources):
        self.calendar.events.return_value = resources
        return self.events._poll_user_caldav_server(self.user)

    def _group(self, size, uid):
        vals = []
        for index in range(size):
            position = datetime(2030, 1, 15, 9) + timedelta(days=index)
            vals.append({"name": "Synthetic meeting", "start": position, "stop": position + timedelta(hours=1),
                         "user_id": self.user.id, "partner_ids": [(6, 0, self.user.partner_id.ids)],
                         "caldav_uid": uid, "caldav_recurrence_id": position})
        events = self.events.with_context(caldav_no_sync=True, no_microsoft_sync=True).create(vals)
        # Reproduce historical detached rows without generating a fresh recurrence.
        self.env.cr.execute("UPDATE calendar_event SET recurrency = true WHERE id = ANY(%s)", (events.ids,))
        events.invalidate_recordset()
        return events

    def test_six_detached_shapes_preserve_ids_owners_and_attendees(self):
        for group, size in enumerate((35, 2, 2, 2, 4, 4)):
            with self.subTest(size=size, group=group):
                events = self._group(size, "shape-%s" % group)
                before = events.read(["name", "start", "stop", "user_id", "partner_ids", "recurrence_id", "caldav_recurrence_id", "active"])
                master = self._component(events[0].caldav_uid, start=events[0].start)
                master.add("rrule", {"freq": "daily", "count": size})
                components = [master] + [self._component(event.caldav_uid, event.caldav_recurrence_id, event.start) for event in events[1:]]
                imported = self.events._sync_event_from_ical(self._resource(components).icalendar_instance, self.user)
                self.assertEqual(set(imported.ids), set(events.ids))
                self.assertEqual(events.read(["name", "start", "stop", "user_id", "partner_ids", "recurrence_id", "caldav_recurrence_id", "active"]), before)
                self.assertEqual(self.events.search_count([("caldav_uid", "=", events[0].caldav_uid)]), size)

    def test_detached_exception_matches_original_position_after_move(self):
        events = self._group(2, "moved")
        target = events[1]
        original_position = target.caldav_recurrence_id
        moved = target.start + timedelta(hours=3)
        payload = self._resource([self._component("moved", original_position, moved, "Remote update")])
        self.events._sync_event_from_ical(payload.icalendar_instance, self.user)
        self.assertEqual(target.start, moved)
        self.assertEqual(target.name, "Remote update")
        self.assertEqual(target.caldav_recurrence_id, original_position)
        self.assertEqual(events[0].name, "Synthetic meeting")

    def test_ambiguous_position_fails_closed(self):
        events = self._group(2, "ambiguous")
        self.env.cr.execute("UPDATE calendar_event SET caldav_recurrence_id = %s WHERE id = %s", (events[0].caldav_recurrence_id, events[1].id))
        events.invalidate_recordset()
        with self.assertRaises(ValueError):
            self.events._sync_event_from_ical(self._resource([self._component("ambiguous", events[0].caldav_recurrence_id)]).icalendar_instance, self.user)
        self.assertTrue(all(events.mapped("active")))

    def test_normal_recurring_series_still_imports(self):
        master = self._component("normal-series")
        master.add("rrule", {"freq": "daily", "count": 4})
        payload = self._resource([master])
        imported = self.events._sync_event_from_ical(payload.icalendar_instance, self.user)
        self.assertEqual(len(imported), 4)
        self.assertEqual(len(imported.recurrence_id), 1)
        self.assertEqual(self.events._sync_event_from_ical(payload.icalendar_instance, self.user), imported)

    def _ordinary_population(self, size=40):
        resources = [self._resource([self._component("ordinary-%s" % i)]) for i in range(size)]
        self._poll(resources)
        events = self.events.search([("user_id", "=", self.user.id), ("caldav_uid", "like", "ordinary-%")])
        self.assertEqual(len(events), size)
        return resources, events

    def test_one_remote_deletion_archives_and_one_update_works(self):
        resources, events = self._ordinary_population()
        removed = events.filtered(lambda event: event.caldav_uid == "ordinary-39")
        resources[0] = self._resource([self._component("ordinary-0", title="Changed remotely")])
        with patch.object(type(self.events), "_sync_unlink_to_caldav") as outbound:
            self._poll(resources[:-1])
        outbound.assert_not_called()
        self.assertTrue(removed.exists())
        self.assertFalse(removed.active)
        self.assertEqual(len(events.filtered("active")), 39)
        self.assertEqual(events.filtered(lambda event: event.caldav_uid == "ordinary-0").name, "Changed remotely")

    def test_empty_and_ten_percent_responses_preserve_every_event(self):
        resources, events = self._ordinary_population()
        for snapshot in ([], resources[:4]):
            with self.assertLogs(LOGGER, "WARNING"):
                outcome = self._poll(snapshot)
            self.assertIsInstance(outcome, Exception)
            self.assertEqual(len(events.exists().filtered("active")), 40)
        notices = self.env["mail.activity"].sudo().search([("res_model", "=", "res.partner"), ("res_id", "=", self.user.partner_id.id), ("summary", "=", "CalDAV: zablokované odstraňovanie udalostí")])
        self.assertEqual(len(notices), 1)

    def test_five_deletions_in_small_calendar_are_allowed(self):
        resources, events = self._ordinary_population(10)
        self._poll(resources[:-5])
        self.assertEqual(len(events.filtered("active")), 5)
        self.assertEqual(len(events.exists()), 10)

    def test_resource_circuit_breaker_is_atomic(self):
        resources, events = self._ordinary_population(40)
        with self.assertLogs(LOGGER, "WARNING"):
            outcome = self._poll(resources[:-21])
        self.assertIsInstance(outcome, Exception)
        self.assertEqual(len(events.filtered("active")), 40)

    def test_incomplete_snapshot_does_not_archive_anything(self):
        resources, events = self._ordinary_population()
        original = self.events._sync_event_from_ical
        def broken(payload, user):
            if str(payload.walk("VEVENT")[0]["uid"]) == "ordinary-0":
                raise ValueError("Synthetic malformed payload")
            return original(payload, user)
        resources[0] = self._resource([self._component("ordinary-0", title="Changed")])
        with patch.object(type(self.events), "_sync_event_from_ical", side_effect=broken), self.assertLogs(LOGGER, "ERROR"):
            outcome = self._poll(resources[:-1])
        self.assertIsInstance(outcome, Exception)
        self.assertEqual(len(events.filtered("active")), 40)

    def test_partial_poll_records_failure_without_rollback_or_backoff(self):
        resources, events = self._ordinary_population()
        resources[0] = self._resource([self._component("ordinary-0", title="Changed")])
        with patch.object(type(self.events), "_sync_event_from_ical", side_effect=ValueError("Synthetic failure")), self.assertLogs(LOGGER, "ERROR"):
            self.calendar.events.return_value = resources
            self.events.poll_caldav_server()
        health = self.user._caldav_health()
        self.assertEqual(health["failure_class"], "CalDAVPartialSyncError")
        self.assertNotIn("last_success", health)
        self.assertNotIn("next_retry", health)
        self.assertEqual(len(events.filtered("active")), 40)

    def test_missing_detached_group_is_never_archived(self):
        detached = self._group(35, "historic-absent")
        resources, ordinary = self._ordinary_population()
        self._poll(resources)
        self.assertEqual(len(detached.exists().filtered("active")), 35)
        self.assertEqual(len(ordinary.filtered("active")), 40)

    def test_base_orphan_does_not_expand_into_series_deletion(self):
        master = self._component("cleanup-series")
        master.add("rrule", {"freq": "daily", "count": 4})
        series = self.events._sync_event_from_ical(self._resource([master]).icalendar_instance, self.user)
        ordinary = self._group(40, "cleanup-budget")
        base = series.recurrence_id.base_event_id
        kept = (series - base) | ordinary
        def forbid_nonempty_unlink(records):
            # Odoo calls unlink on its empty detached-events recordset in write.
            # Any attempt to unlink an actual row must still fail this test.
            self.assertFalse(records, "No event row may be unlinked")
            return True
        with patch.object(type(self.events), "_sync_unlink_to_caldav") as remote_delete, \
                patch.object(type(self.events), "unlink", autospec=True, side_effect=forbid_nonempty_unlink):
            self.events._caldav_cleanup_snapshot(self.user, set(kept.ids), set(), 41, calendar=self.calendar, resource_baseline=41)
        remote_delete.assert_not_called()
        self.assertTrue(base.exists())
        self.assertTrue(base.active)
        self.assertTrue(all(kept.mapped("active")))

    def test_yearly_exclusions_share_the_resource_history(self):
        resources, events = self._ordinary_population()
        # Three resources are below the rolling allowance and archive together.
        yearly_uids = set(events[:2].mapped("caldav_uid"))
        with self.assertLogs(LOGGER, "WARNING"):
            outcome = self.events._caldav_cleanup_snapshot(self.user, set(events[:-1].ids), yearly_uids, 39, 40)
        self.assertIsNone(outcome)
        self.assertEqual(len(events.exists().filtered("active")), 37)
