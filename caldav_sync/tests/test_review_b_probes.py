"""Reviewer B probes. Synthetic data only; every remote operation is mocked."""
import logging
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import caldav
import icalendar
import pytz
from caldav.elements import dav

from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon

_logger = logging.getLogger("REVIEWB")
LOGGER = "odoo.addons.caldav_sync.models.calendar_event"


@tagged("post_install", "-at_install", "reviewb")
class TestReviewBProbes(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user("reviewb", "synthetic", "synthetic", "https://calendar.example.test/")
        cls.events = cls.env["calendar.event"]

    def setUp(self):
        super().setUp()
        self.calendar = MagicMock()
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-1"}
        from caldav.lib.error import NotFoundError
        self.calendar.event_by_uid.side_effect = NotFoundError("synthetic: absent on server")
        self.client = MagicMock()
        self.client.calendar.return_value = self.calendar
        patcher = patch.object(type(self.user), "_get_caldav_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _component(self, uid, position=None, start=None, title="Synthetic meeting", tz=None):
        start = start or datetime(2030, 1, 15, 9)
        component = icalendar.Event()
        component.add("uid", uid)
        component.add("summary", title)
        if tz:
            zone = pytz.timezone(tz)
            component.add("dtstart", zone.localize(start))
            component.add("dtend", zone.localize(start + timedelta(hours=1)))
        else:
            component.add("dtstart", start.replace(tzinfo=UTC))
            component.add("dtend", (start + timedelta(hours=1)).replace(tzinfo=UTC))
        component.add("dtstamp", datetime(2040, 1, 1, tzinfo=UTC))
        if position is not None:
            if tz:
                component.add("recurrence-id", pytz.timezone(tz).localize(position))
            else:
                component.add("recurrence-id", position.replace(tzinfo=UTC))
        component.add("attendee", "mailto:" + self.user.partner_id.email)
        return component

    def _resource(self, components):
        payload = icalendar.Calendar()
        for component in components:
            payload.add_component(component)
        uid = str(components[0]["uid"])
        return caldav.Event(data=payload.to_ical(), url="https://calendar.example.test/%s.ics" % uid)

    def _poll(self, resources, same_token=False):
        if not same_token:
            self._token = getattr(self, "_token", 0) + 1
            self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-%s" % self._token}
        self.calendar.events.return_value = resources
        return self.events._poll_user_caldav_server(self.user)

    def _ordinary(self, size, prefix="ord"):
        return [self._resource([self._component("%s-%s" % (prefix, i), start=datetime(2030, 2, 1, 9) + timedelta(days=i))]) for i in range(size)]

    # P1 ---------------------------------------------------------------
    def test_p1_remote_series_deletion_removes_every_occurrence(self):
        """A whole recurring meeting deleted remotely must not stay visible in Odoo."""
        ordinary = self._ordinary(40)
        master = self._component("weekly-series", start=datetime(2030, 3, 4, 9))
        master.add("rrule", {"freq": "weekly", "count": 5})
        series_resource = self._resource([master])
        self._poll(ordinary + [series_resource])
        series = self.events.search([("caldav_uid", "=", "weekly-series")])
        self.assertEqual(len(series), 5)
        outcome = self._poll(ordinary)  # the series was deleted remotely
        left = series.exists().filtered("active")
        _logger.warning("REVIEWB P1 outcome=%r active occurrences left=%s of 5 (ids %s); base active=%s",
                        outcome, len(left), left.ids, series.exists().recurrence_id.base_event_id.active)
        self.assertFalse(left, "remotely deleted series still shows %s occurrences" % len(left))

    # P2 ---------------------------------------------------------------
    def test_p2_small_calendar_single_deletion_propagates(self):
        """One remote deletion in a 10-event calendar."""
        resources = self._ordinary(10)
        self._poll(resources)
        removed = self.events.search([("caldav_uid", "=", "ord-9")])
        outcomes = []
        for index in range(3):
            self.calendar.events.reset_mock()
            outcomes.append(self._poll(resources[:-1], same_token=index > 0))
            fetched = self.calendar.events.called
        health = self.user._caldav_health()
        _logger.warning("REVIEWB P2 outcomes=%r removed.active=%s third poll fetched full calendar=%s",
                        outcomes, removed.exists().active if removed.exists() else "deleted", fetched)
        self.assertFalse(removed.exists().filtered("active"), "single remote deletion never propagates")

    # P2b --------------------------------------------------------------
    def test_p2b_series_of_25_deleted_in_large_calendar(self):
        """A 25-occurrence series whose occurrences differ from base (e.g. per-occurrence notes)."""
        ordinary = self._ordinary(400)
        master = self._component("daily-series", start=datetime(2030, 3, 4, 9))
        master.add("rrule", {"freq": "daily", "count": 25})
        self._poll(ordinary + [self._resource([master])])
        series = self.events.search([("caldav_uid", "=", "daily-series")])
        self.assertEqual(len(series), 25)
        # Make every occurrence a differing one (Odoo-side edit, e.g. location)
        (series - series.recurrence_id.base_event_id).with_context(caldav_no_sync=True).write(
            {"location": "Room B", "recurrence_update": "self_only"})
        outcomes = [self._poll(ordinary)]
        left = series.exists().filtered("active")
        _logger.warning("REVIEWB P2b outcomes=%r active left=%s of 25", outcomes, len(left))
        self.assertFalse(left)

    # P4 ---------------------------------------------------------------
    def test_p4_detached_override_with_local_tz_across_dst(self):
        """Detached rows whose stored positions came from a Europe/Bratislava series (UTC
        time of the first occurrence + occurrence date), remote override sent with TZID."""
        tz = pytz.timezone("Europe/Bratislava")
        vals, first_local = [], datetime(2030, 10, 18, 11)
        first_utc = tz.localize(first_local).astimezone(pytz.utc).replace(tzinfo=None)
        for week in range(4):  # 18.10, 25.10, 1.11, 8.11 2030 (DST ends 27.10.2030)
            local = first_local + timedelta(weeks=week)
            start = tz.localize(local).astimezone(pytz.utc).replace(tzinfo=None)
            position = datetime.combine(start.date(), first_utc.time())  # _recompute_caldav_ids
            vals.append({"name": "Synthetic meeting", "start": start, "stop": start + timedelta(hours=1),
                         "user_id": self.user.id, "partner_ids": [(6, 0, self.user.partner_id.ids)],
                         "caldav_uid": "dst-uid", "caldav_recurrence_id": position, "event_tz": "Europe/Bratislava"})
        events = self.events.with_context(caldav_no_sync=True, no_microsoft_sync=True).create(vals)
        self.env.cr.execute("UPDATE calendar_event SET recurrency = true WHERE id = ANY(%s)", (events.ids,))
        events.invalidate_recordset()
        master = self._component("dst-uid", start=first_local, tz="Europe/Bratislava")
        master.add("rrule", {"freq": "weekly", "count": 4})
        winter = first_local + timedelta(weeks=3)
        override = self._component("dst-uid", position=winter, start=winter + timedelta(hours=1),
                                   title="Moved", tz="Europe/Bratislava")
        error = None
        try:
            with self.env.cr.savepoint():
                self.events._sync_event_from_ical(self._resource([master, override]).icalendar_instance, self.user)
        except Exception as exc:
            error = exc
        _logger.warning("REVIEWB P4 error=%r", error)
        self.assertIsNone(error)

    # P6 ---------------------------------------------------------------
    def test_p6_local_changes_push_same_remote_calls(self):
        """Record remote unlink/write calls for local user actions (compare old vs new)."""
        calls = []
        cls = type(self.events)
        orig_unlink, orig_write = cls._sync_unlink_to_caldav, cls._sync_write_to_caldav

        def rec_unlink(rec):
            calls.append(("unlink", rec.id, bool(rec.env.context.get("caldav_delete_all"))))

        def rec_write(rec):
            calls.append(("write", rec.id))

        master = self._component("local-series", start=datetime(2030, 3, 4, 9))
        master.add("rrule", {"freq": "weekly", "count": 4})
        self._poll([self._resource([master])] + self._ordinary(3))
        series = self.events.search([("caldav_uid", "=", "local-series")], order="start")
        single = self.events.search([("caldav_uid", "=", "ord-0")])
        with patch.object(cls, "_sync_unlink_to_caldav", autospec=True, side_effect=rec_unlink), \
                patch.object(cls, "_sync_write_to_caldav", autospec=True, side_effect=rec_write):
            single.action_archive()
            _logger.warning("REVIEWB P6a archive single: %s", calls); calls.clear()
            try:
                with self.env.cr.savepoint():
                    series[2].write({"name": "Future changed", "start": series[2].start + timedelta(hours=1),
                                     "stop": series[2].stop + timedelta(hours=1), "recurrence_update": "future_events"})
            except RecursionError as exc:
                _logger.warning("REVIEWB P6b future_events raised %r", exc)
            _logger.warning("REVIEWB P6b future_events: %s", calls); calls.clear()
            series = self.events.search([("caldav_uid", "=", "local-series")], order="start")
            series[0].write({"start": series[0].start + timedelta(hours=2), "stop": series[0].stop + timedelta(hours=2),
                             "recurrence_update": "all_events"})
            _logger.warning("REVIEWB P6c all_events: %s", calls); calls.clear()
            series = self.events.search([("caldav_uid", "!=", False), ("recurrence_id", "!=", False)], order="start")
            series[1].action_mass_archive("future_events")
            _logger.warning("REVIEWB P6d mass archive future: %s", calls); calls.clear()

    # P7 ---------------------------------------------------------------
    def test_p7_query_counts(self):
        resources = self._ordinary(150)
        master = self._component("perf-series", start=datetime(2030, 3, 4, 9))
        master.add("rrule", {"freq": "weekly", "count": 30})
        resources.append(self._resource([master]))
        self.env.flush_all()
        q0 = self.env.cr.sql_log_count
        self._poll(resources)
        self.env.flush_all()
        q1 = self.env.cr.sql_log_count
        resources[0] = self._resource([self._component("ord-0", start=datetime(2030, 2, 1, 9), title="Changed")])
        self._poll(resources)
        self.env.flush_all()
        q2 = self.env.cr.sql_log_count
        self._poll(resources, same_token=True)
        q3 = self.env.cr.sql_log_count
        _logger.warning("REVIEWB P7 queries: full import=%s, one changed=%s, unchanged=%s", q1 - q0, q2 - q1, q3 - q2)

    # P8 ---------------------------------------------------------------
    def test_p8_mixed_uid_chain_plus_one_detached_row(self):
        """A linked chain and one stray detached row share a UID (e.g. a half-finished
        detach). The chain must still be imported and never become a cleanup candidate."""
        ordinary = self._ordinary(40)
        master = self._component("mixed-series", start=datetime(2030, 3, 4, 9))
        master.add("rrule", {"freq": "weekly", "count": 4})
        resource = self._resource([master])
        self._poll(ordinary + [resource])
        chain = self.events.search([("caldav_uid", "=", "mixed-series")], order="start")
        self.assertEqual(len(chain), 4)
        stray = chain[-1]
        self.env.cr.execute("UPDATE calendar_event SET recurrence_id = NULL WHERE id = %s", (stray.id,))
        self.env.invalidate_all()
        resource = self._resource([master])  # unchanged remote, new digest not needed: token rotates
        master2 = self._component("mixed-series", start=datetime(2030, 3, 4, 9), title="Renamed remotely")
        master2.add("rrule", {"freq": "weekly", "count": 4})
        outcome = self._poll(ordinary + [self._resource([master2])])
        linked = (chain - stray).exists()
        _logger.warning("REVIEWB P8 outcome=%r linked active=%s/3 names=%s stray active=%s",
                        outcome, len(linked.filtered("active")), linked.mapped("name"), stray.exists().active if stray.exists() else "deleted")
        self.assertEqual(len(linked.filtered("active")), 3)
        # Combined owner direction requires mixed UIDs to fail closed, so the
        # remote rename is intentionally not applied to any row in this resource.
        self.assertIsInstance(outcome, Exception)
        self.assertEqual(set(linked.mapped("name")), {"Synthetic meeting"})
        self.assertTrue(stray.active)
