"""Reviewer A round 2 probes on the rework. Synthetic only; remote mocked.

Each test asserts the SAFE outcome; a failure records the unsafe one.
"""
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import caldav
import icalendar
from caldav.lib.error import NotFoundError
from pytz import timezone

from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon

BRATISLAVA = timezone("Europe/Bratislava")


@tagged("post_install", "-at_install", "review_a2")
class TestReviewA2(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user("rev-a2", "synthetic", "synthetic", "https://calendar.example.test/")
        cls.user.tz = "Europe/Bratislava"
        cls.events = cls.env["calendar.event"]

    def setUp(self):
        super().setUp()
        self.calendar = MagicMock()
        self.calendar.get_properties.return_value = {}
        self.gone = set()

        def by_uid(uid):
            if uid in self.gone:
                raise NotFoundError(uid)
            return MagicMock()
        self.calendar.event_by_uid.side_effect = by_uid
        self.client = MagicMock()
        self.client.calendar.return_value = self.calendar
        patcher = patch.object(type(self.user), "_get_caldav_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _component(self, uid, start, title="Synthetic meeting", tz=None):
        c = icalendar.Event()
        c.add("uid", uid)
        c.add("summary", title)
        if tz:
            c.add("dtstart", tz.localize(start))
            c.add("dtend", tz.localize(start + timedelta(hours=1)))
        else:
            c.add("dtstart", start.replace(tzinfo=UTC))
            c.add("dtend", (start + timedelta(hours=1)).replace(tzinfo=UTC))
        c.add("dtstamp", datetime(2040, 1, 1, tzinfo=UTC))
        c.add("attendee", "mailto:" + self.user.partner_id.email)
        return c

    def _resource(self, components):
        payload = icalendar.Calendar()
        for c in components:
            payload.add_component(c)
        return caldav.Event(data=payload.to_ical(), url="https://calendar.example.test/%s.ics" % components[0]["uid"])

    def _series(self, uid, count, start, freq="weekly", tz=None):
        m = self._component(uid, start, tz=tz)
        m.add("rrule", {"freq": freq, "count": count})
        return self._resource([m])

    def _ordinary(self, n, prefix="ord"):
        return [self._resource([self._component("%s-%s" % (prefix, i), datetime(2030, 3, 1, 9) + timedelta(days=i))])
                for i in range(n)]

    def _poll(self, resources):
        self.calendar.events.return_value = resources
        return self.events._poll_user_caldav_server(self.user)

    def _rows(self, uid):
        return self.events.with_context(active_test=False).search([("caldav_uid", "=", uid), ("user_id", "=", self.user.id)])

    def _active(self, like):
        return self.events.search_count([("caldav_uid", "like", like), ("user_id", "=", self.user.id)])

    # P7: a series spanning DST, confirmed gone, then back. Every row must return.
    def test_dst_series_restored_after_reappearance(self):
        series = self._series("dst", 4, datetime(2030, 3, 18, 9), tz=BRATISLAVA)  # DST starts 2030-03-31
        ordinary = self._ordinary(5)
        self._poll(ordinary + [series])
        rows = self._rows("dst")
        self.assertEqual(len(rows), 4)
        self.gone.add("dst")
        self._poll(ordinary)
        archived = len(rows.filtered(lambda e: not e.active))
        self.gone.discard("dst")
        outcome = self._poll(ordinary + [series])
        rows.invalidate_recordset()
        self.assertEqual(
            len(rows.filtered("active")), 4,
            "DST series: archived %s, active after return %s; starts %s; positions %s; outcome %s" % (
                archived, len(rows.filtered("active")), rows.mapped("start"),
                rows.mapped("caldav_recurrence_id"), type(outcome).__name__))

    # P7b: control without DST.
    def test_utc_series_restored_after_reappearance(self):
        series = self._series("utc", 4, datetime(2030, 6, 3, 9))
        ordinary = self._ordinary(5)
        self._poll(ordinary + [series])
        rows = self._rows("utc")
        self.gone.add("utc")
        self._poll(ordinary)
        self.gone.discard("utc")
        self._poll(ordinary + [series])
        rows.invalidate_recordset()
        self.assertEqual(len(rows.filtered("active")), 4, "UTC control active %s" % rows.mapped("active"))
        base = rows.recurrence_id.base_event_id
        self.assertEqual(base, rows.sorted("start")[:1], "base after restore %s" % base)

    # P8: small account, one empty listing. Nothing should disappear.
    def test_small_account_empty_listing(self):
        ordinary = self._ordinary(12)
        self._poll(ordinary)
        outcome = self._poll([])
        hidden = 12 - self._active("ord-%")
        self._poll(ordinary)
        back = self._active("ord-%")
        self.assertEqual(hidden, 0, "one empty listing hid %s of 12 events (outcome %s); after a good poll %s visible" % (
            hidden, type(outcome).__name__, back))

    # P9: rows archived in one poll by a resource-counted breaker.
    def test_rows_per_poll_under_resource_breaker(self):
        series = [self._series("ser-%s" % i, 30, datetime(2030, 4, 1, 9) + timedelta(hours=i), freq="daily")
                  for i in range(20)]
        ordinary = self._ordinary(40)
        self._poll(ordinary + series)
        total = self._active("ser-%")
        self.assertEqual(total, 600)
        self.gone.update("ser-%s" % i for i in range(20))
        outcome = self._poll(ordinary)
        hidden = total - self._active("ser-%")
        self.assertLessEqual(hidden, 60, "one poll archived %s rows of 20 series without approval (outcome %s)" % (
            hidden, type(outcome).__name__))

    # N3: the occurrence-row budget blocks a small number of large series.
    def test_meeting_row_budget_blocks_large_series(self):
        series = [
            self._series("rows-%s" % i, 51, datetime(2030, 6, 1, 9) + timedelta(days=i), freq="daily")
            for i in range(2)
        ]
        ordinary = self._ordinary(10)
        self._poll(ordinary + series)
        rows = self.events.search([("caldav_uid", "like", "rows-%")])
        self.assertEqual(len(rows), 102)
        outcome = self._poll(ordinary)
        self.assertIsInstance(outcome, Exception)
        self.assertEqual(len(rows.filtered("active")), 102)
        state = self.events._caldav_safety_state(self.user)
        self.assertEqual(state["pending"]["meetings_used"], 0)
        self.assertEqual(state["pending"]["meeting_limit"], 100)
        self.assertEqual(state["pending"]["ids"], sorted(rows.ids))

    # P10: approval releases only the exact pending set, once.
    def test_approval_exact_set(self):
        ordinary = self._ordinary(60)
        self._poll(ordinary)
        missing = ordinary[:-25]
        out1 = self._poll(missing)
        self.assertEqual(self._active("ord-%"), 60, "breaker did not hold: %s" % type(out1).__name__)
        admin = self.env.ref("base.user_admin")
        self.events.with_user(admin)._caldav_approve_cleanup(self.user)
        # one more resource disappears before the next poll
        out2 = self._poll(missing[:-1])
        self.assertEqual(self._active("ord-%"), 60, "approval released a different set (%s)" % type(out2).__name__)
        self.events.with_user(admin)._caldav_approve_cleanup(self.user)
        self._poll(missing[:-1])
        self.assertEqual(self._active("ord-%"), 34)
        # approval consumed: a further 25 missing must hold again
        out4 = self._poll(missing[:-1][:-25])
        self.assertEqual(self._active("ord-%"), 34, "approval reused (%s)" % type(out4).__name__)

    # P10b: a pending row edited after approval must not be archived.
    def test_approval_invalidated_by_edit(self):
        ordinary = self._ordinary(60)
        self._poll(ordinary)
        missing = ordinary[:-25]
        self._poll(missing)
        admin = self.env.ref("base.user_admin")
        self.events.with_user(admin)._caldav_approve_cleanup(self.user)
        edited = self.events.search([("caldav_uid", "=", "ord-59"), ("user_id", "=", self.user.id)])
        edited.with_context(caldav_no_sync=True).write({"description": "<p>local note</p>"})
        self._poll(missing)
        self.assertTrue(edited.active, "edited row archived under a stale approval")
        self.assertEqual(self._active("ord-%"), 60)

    # P11: a stop flag set during the poll must prevent the archive.
    def test_stop_flag_before_archive(self):
        ordinary = self._ordinary(10)
        self._poll(ordinary)
        original = type(self.events)._caldav_cleanup_plan

        def plan_then_stop(model, *args, **kwargs):
            res = original(model, *args, **kwargs)
            self.env["ir.config_parameter"].sudo().set_param("caldav_sync.stop_requested", "true")
            self.env.cr.execute("SELECT 1")
            return res
        # _caldav_check_stop reads through a separate cursor: patch it to read this one.
        def check_here(model):
            v = self.env["ir.config_parameter"].sudo().get_param("caldav_sync.stop_requested")
            if v == "true":
                from odoo.addons.caldav_sync.models.calendar_event import CalDAVPollStopped
                raise CalDAVPollStopped()
        with patch.object(type(self.events), "_caldav_cleanup_plan", plan_then_stop), \
                patch.object(type(self.events), "_caldav_check_stop", check_here):
            try:
                self._poll(ordinary[:-3])
            except Exception as exc:  # CalDAVPollStopped propagates to the coordinator
                self.assertEqual(type(exc).__name__, "CalDAVPollStopped")
        self.assertEqual(self._active("ord-%"), 10)
