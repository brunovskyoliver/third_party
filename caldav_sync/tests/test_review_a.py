"""Reviewer A adversarial probes. Synthetic only; every remote call mocked.

These tests ASSERT THE SAFE OUTCOME. A failure means the unsafe behaviour
happened; the failure message records what was observed.
"""
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import caldav
import icalendar

from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon


@tagged("post_install", "-at_install", "review_a")
class TestReviewA(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user("rev-a", "synthetic", "synthetic", "https://calendar.example.test/")
        cls.events = cls.env["calendar.event"]

    def setUp(self):
        super().setUp()
        self.calendar = MagicMock()
        self.calendar.get_properties.return_value = {}  # no sync token, like user 5
        self.client = MagicMock()
        self.client.calendar.return_value = self.calendar
        patcher = patch.object(type(self.user), "_get_caldav_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _component(self, uid, position=None, start=None, title="Synthetic meeting", organizer=None):
        start = start or datetime(2030, 1, 15, 9)
        c = icalendar.Event()
        c.add("uid", uid)
        c.add("summary", title)
        c.add("dtstart", start.replace(tzinfo=UTC))
        c.add("dtend", (start + timedelta(hours=1)).replace(tzinfo=UTC))
        c.add("dtstamp", datetime(2040, 1, 1, tzinfo=UTC))
        if position is not None:
            c.add("recurrence-id", position.replace(tzinfo=UTC))
        if organizer:
            c.add("organizer", "mailto:" + organizer)
        c.add("attendee", "mailto:" + self.user.partner_id.email)
        return c

    def _resource(self, components):
        payload = icalendar.Calendar()
        for c in components:
            payload.add_component(c)
        uid = str(components[0]["uid"])
        return caldav.Event(data=payload.to_ical(), url="https://calendar.example.test/%s.ics" % uid)

    def _poll(self, resources):
        self.calendar.events.return_value = resources
        return self.events._poll_user_caldav_server(self.user)

    def _series_master(self, uid, count, start=None):
        m = self._component(uid, start=start)
        m.add("rrule", {"freq": "daily", "count": count})
        return m

    def _detached(self, uid, size, first):
        vals = []
        for i in range(size):
            p = first + timedelta(days=i)
            vals.append({"name": "Historic", "start": p, "stop": p + timedelta(hours=1),
                         "user_id": self.user.id, "partner_ids": [(6, 0, self.user.partner_id.ids)],
                         "caldav_uid": uid, "caldav_recurrence_id": p})
        ev = self.events.with_context(caldav_no_sync=True, no_microsoft_sync=True).create(vals)
        self.env.cr.execute("UPDATE calendar_event SET recurrency = true, recurrence_id = NULL WHERE id = ANY(%s)", (ev.ids,))
        ev.invalidate_recordset()
        return ev

    def _ordinary(self, n=40):
        return [self._resource([self._component("ord-%s" % i, start=datetime(2030, 3, 1, 9) + timedelta(days=i))]) for i in range(n)]

    def _drop_cache(self):
        # Simulates the first poll after the v2->v3 cache bump.
        self.env["ir.config_parameter"].sudo().search([("key", "=", "caldav_sync.poll_cache.%s" % self.user.id)]).unlink()

    def _uid_rows(self, uid):
        return self.events.with_context(active_test=False).search([("caldav_uid", "=", uid), ("user_id", "=", self.user.id)])

    # 1. A missing recurring series: the base is archived, Odoo selects a new
    # base, and the next poll archives that one too. Per-run cap never trips.
    def test_series_cascade_across_polls(self):
        ordinary = self._ordinary(40)
        series = self._resource([self._series_master("casc", 6)])
        self._poll(ordinary + [series])
        rows = self._uid_rows("casc")
        self.assertEqual(len(rows), 6)
        trace = []
        for _run in range(8):
            outcome = self._poll(ordinary)  # series resource absent from listing
            trace.append((len(rows.filtered("active")), type(outcome).__name__))
        self.assertGreaterEqual(
            len(rows.filtered("active")), 5,
            "series cascade: active rows after each poll %s" % trace)

    # 2. Same UID: active detached rows plus a live linked recurrence. The
    # remote master is present, yet the live series is not marked synced.
    def test_mixed_detached_and_live_series(self):
        ordinary = self._ordinary(40)
        self._poll(ordinary + [self._resource([self._series_master("mix", 4)])])
        live = self._uid_rows("mix")
        self.assertEqual(len(live), 4)
        self.assertTrue(live.recurrence_id)
        detached = self._detached("mix", 3, datetime(2029, 6, 1, 9))
        trace = []
        for _run in range(6):
            self._drop_cache()
            outcome = self._poll(ordinary + [self._resource([self._series_master("mix", 4)])])
            trace.append((len(live.filtered("active")), type(outcome).__name__))
        self.assertTrue(all(detached.mapped("active")))
        self.assertEqual(
            len(live.filtered("active")), 4,
            "live series rows with a detached sibling UID archived while remote master is present: %s" % trace)

    # 3. Same UID: active detached rows plus one plain (non-recurring) row.
    def test_mixed_detached_and_single_event(self):
        ordinary = self._ordinary(40)
        single_res = self._resource([self._component("mix1", start=datetime(2031, 5, 5, 9))])
        self._poll(ordinary + [single_res])
        single = self._uid_rows("mix1")
        self.assertEqual(len(single), 1)
        self.assertFalse(single.recurrency)
        self._detached("mix1", 2, datetime(2029, 6, 1, 9))
        self._drop_cache()
        outcome = self._poll(ordinary + [single_res])
        self.assertTrue(
            single.active,
            "present single event archived because a detached sibling UID exists (outcome %s)" % type(outcome).__name__)

    # 4. Persistent omission of up to `limit` plain resources per run: total
    # archived over time is unbounded by the cap.
    def test_cap_is_per_run_only(self):
        ordinary = self._ordinary(400)  # limit = 20
        self._poll(ordinary)
        archived = []
        for run in range(3):
            shown = ordinary[: 400 - 15 * (run + 1)]
            self._poll(shown)
            archived.append(self.events.with_context(active_test=False).search_count(
                [("user_id", "=", self.user.id), ("active", "=", False), ("caldav_uid", "like", "ord-%")]))
        # Combined owner direction: 400 resources have a 40-resource rolling
        # allowance, replacing review A's provisional 20-row allowance.
        self.assertEqual(archived, [15, 30, 30], "archived after each run: %s" % archived)

    # 5. Archived row comes back remotely: is it shown again?
    def test_reappearing_series_after_false_omission(self):
        ordinary = self._ordinary(40)
        series = self._resource([self._series_master("back", 4)])
        self._poll(ordinary + [series])
        rows = self._uid_rows("back")
        self._poll(ordinary)           # one false omission
        self._poll(ordinary + [series])  # resource is back
        visible = self.events.search_count([("caldav_uid", "=", "back"), ("user_id", "=", self.user.id)])
        self.assertEqual(visible, 4, "visible rows after the series came back: %s; rows %s active %s" % (
            visible, len(rows), rows.mapped("active")))

    # 6. Another user's sync. User B imports an event organised by user A
    # (an Odoo user with CalDAV). A's own remote calendar does not hold it.
    def test_other_users_poll_archives_imported_event(self):
        with patch("caldav.DAVClient"):
            other = self._generate_user("rev-b", "synthetic-b", "synthetic-b", "https://calendar.example.test/b/")
        ordinary = self._ordinary(40)
        self._poll(ordinary)
        # B's calendar holds an invitation organised by A.
        inv = self._resource([self._component("inv-from-a", start=datetime(2031, 1, 1, 9),
                                              organizer=self.user.partner_id.email)])
        self.calendar.events.return_value = [inv]
        self.events._poll_user_caldav_server(other)
        row = self.events.search([("caldav_uid", "=", "inv-from-a")])
        self.assertEqual(len(row), 1)
        owner = row.user_id
        with self.assertLogs("odoo.addons.caldav_sync.models.calendar_event", "WARNING") as logs:
            self._poll(ordinary)  # A's own snapshot, complete, without that UID
        self.assertTrue(any("event IDs [%s]" % row.id in line and "origin" in line for line in logs.output))
        self.assertTrue(
            row.active,
            "B's imported event (user_id=%s, A=%s) archived by A's cleanup" % (owner.id, self.user.id))
