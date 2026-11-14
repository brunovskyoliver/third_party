"""Reviewer B round 3 probes. Synthetic data only; every remote operation is mocked."""
import json
import logging
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import caldav
import icalendar
from caldav.elements import dav
from caldav.lib.error import NotFoundError

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon

_logger = logging.getLogger("REVIEWB3")
MODEL_LOGGER = "odoo.addons.caldav_sync.models.calendar_event"


class _Count(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            self.records.append((record.levelname, record.getMessage()[:140]))


@tagged("post_install", "-at_install", "reviewb3")
class TestReviewB3Probes(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user("reviewb3", "synthetic", "synthetic", "https://calendar.example.test/")
        cls.events = cls.env["calendar.event"]

    def setUp(self):
        super().setUp()
        self.calendar = MagicMock()
        self.calendar.event_by_uid.side_effect = NotFoundError("synthetic: absent on server")
        self.client = MagicMock()
        self.client.calendar.return_value = self.calendar
        patcher = patch.object(type(self.user), "_get_caldav_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.handler = _Count()
        for name in (MODEL_LOGGER,):
            logging.getLogger(name).addHandler(self.handler)
            self.addCleanup(logging.getLogger(name).removeHandler, self.handler)
        self._token = 0

    def _component(self, uid, start=None, title="Synthetic meeting", hours=1):
        start = start or datetime(2030, 1, 15, 9)
        component = icalendar.Event()
        component.add("uid", uid)
        component.add("summary", title)
        component.add("dtstart", start.replace(tzinfo=UTC))
        component.add("dtend", (start + timedelta(hours=hours)).replace(tzinfo=UTC))
        component.add("dtstamp", datetime(2040, 1, 1, tzinfo=UTC))
        component.add("attendee", "mailto:" + self.user.partner_id.email)
        return component

    def _resource(self, components):
        payload = icalendar.Calendar()
        for component in components:
            payload.add_component(component)
        uid = str(components[0]["uid"])
        return caldav.Event(data=payload.to_ical(), url="https://calendar.example.test/%s.ics" % uid)

    def _poll(self, resources):
        self._token += 1
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-%s" % self._token}
        self.calendar.events.return_value = resources
        self.handler.records.clear()
        outcome = self.events._poll_user_caldav_server(self.user)
        return repr(outcome), [level for level, _ in self.handler.records], list(self.handler.records)

    def _ordinary(self, size, prefix="ord"):
        return [self._resource([self._component("%s-%s" % (prefix, i), start=datetime(2030, 2, 1, 9) + timedelta(days=i))])
                for i in range(size)]

    def _series(self, uid, rule, start=datetime(2030, 3, 4, 9), title="Series"):
        master = self._component(uid, start=start, title=title)
        master.add("rrule", rule)
        return master

    def _report(self, label, **values):
        _logger.warning("REVIEWB3 %s %s", label, values)

    def _ledger(self):
        text = self.env["ir.config_parameter"].sudo().get_param("caldav_sync.safety.%s" % self.user.id, "")
        return json.loads(text) if text else {}

    # Q1: meeting created in Odoo through the real push path, later deleted on the phone
    def test_q1_real_push_records_origin_and_deletion_propagates(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary)
        pushed = MagicMock()
        pushed.vobject_instance.vevent.uid.value = "pushed-uid-1"
        self.calendar.save_event.return_value = pushed
        event = self.events.create({
            "name": "Created in Odoo", "start": datetime(2030, 4, 1, 9), "stop": datetime(2030, 4, 1, 10),
            "user_id": self.user.id, "partner_ids": [(6, 0, self.user.partner_id.ids)],
        })
        recorded = str(event.id) in self._ledger().get("created", {})
        echo = self._resource([self._component(event.caldav_uid, start=datetime(2030, 4, 1, 9), title="Created in Odoo")])
        self._poll(ordinary + [echo])
        result = [self._poll(ordinary) for _ in range(2)]
        self._report("Q1", uid=event.caldav_uid, recorded=recorded, active=event.exists().active, results=result)
        self.assertTrue(recorded)
        self.assertFalse(event.active)

    # Q2: origin older than 90 days, then a remote deletion -----------------------
    def test_q2_origin_older_than_90_days(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary)
        gone = self.events.search([("caldav_uid", "=", "ord-29")])
        state = self._ledger()
        old = fields.Datetime.to_string(fields.Datetime.now() - timedelta(days=100))
        for entry in state["created"].values():
            entry["at"] = old  # equivalent to 100 days passing since import/backfill
        self.env["ir.config_parameter"].sudo().set_param("caldav_sync.safety.%s" % self.user.id, json.dumps(state))
        result = [self._poll(ordinary[:-1]) for _ in range(2)]
        self._report("Q2", active=gone.active, created_entries=len(self._ledger().get("created", {})), results=result)
        self.assertFalse(gone.active, "a meeting imported >90 days ago never deletes")

    # Q2b: same, but by creating rows long ago (create_date fallback) -------------
    def test_q2b_backfilled_legacy_rows_after_90_days(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary)
        # Simulate pre-deploy LIVE rows: wipe ledger and backfill marker, rows created 200 days ago
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("caldav_sync.safety.%s" % self.user.id, "")
        params.set_param("caldav_sync.origin_backfill.%s" % self.user.id, "")
        ids = self.events.search([("user_id", "=", self.user.id)]).ids
        self.env.cr.execute("UPDATE calendar_event SET create_date = now() - interval '200 days' WHERE id = ANY(%s)", (ids,))
        self.env.invalidate_all()
        first = self._poll(ordinary)  # first complete sync after deploy -> backfill
        backfilled = len(self._ledger().get("created", {}))
        gone = self.events.search([("caldav_uid", "=", "ord-29")])
        later = self._poll(ordinary[:-1])
        self._report("Q2b", backfilled=backfilled, first=first, later=later, active=gone.active)
        self.assertFalse(gone.active)

    # Q3: local "all events" time change in Odoo, then the series is deleted on the phone
    def test_q3_local_rewrite_then_remote_series_deletion(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary + [self._resource([self._series("rw-series", {"freq": "weekly", "count": 4})])])
        series = self.events.search([("caldav_uid", "=", "rw-series")], order="start")
        with patch.object(type(self.events), "_sync_unlink_to_caldav", autospec=True), \
                patch.object(type(self.events), "_sync_write_to_caldav", autospec=True):
            series[0].write({"start": series[0].start + timedelta(hours=2), "stop": series[0].stop + timedelta(hours=2),
                             "recurrence_update": "all_events"})
        new_rows = self.events.search([("user_id", "=", self.user.id), ("recurrence_id", "!=", False)])
        uid = new_rows[:1].caldav_uid
        led = self._ledger().get("created", {})
        proven = [str(i) in led for i in new_rows.ids]
        echo = self._series(uid, {"freq": "weekly", "count": 4}, start=datetime(2030, 3, 4, 11))
        r1 = self._poll(ordinary + [self._resource([echo])])
        r2 = [self._poll(ordinary) for _ in range(2)]
        left = new_rows.exists().filtered("active")
        self._report("Q3", rows=len(new_rows), proven=proven, after_echo=r1, after_delete=r2, active_left=len(left))
        # Final B R2: carry only existing proof across the same owner/UID
        # rewrite. New/unproven and historical detached rows remain excluded.
        self.assertEqual(r1[0], "None", "remote echo of a proven local rewrite must stay complete")
        self.assertEqual(uid, "rw-series")
        self.assertTrue(all(str(row.id) in self._ledger()["created"] for row in new_rows))
        self.assertFalse(left, "proven regenerated series should archive after confirmed remote deletion")
        restored = self._poll(ordinary + [self._resource([echo])])
        self.assertEqual(restored[0], "None")
        self.assertEqual(set(new_rows.filtered("active").ids), set(new_rows.ids))
        superseded = series - new_rows
        self.assertFalse(superseded.exists().filtered("active"))

    # Q4: remote UNTIL shortening / COUNT extension -----------------------------
    def test_q4_until_and_count_changes(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary + [self._resource([self._series("cut", {"freq": "weekly", "count": 6}),
                                               ]), self._resource([self._series("grow", {"freq": "weekly", "count": 3}, start=datetime(2030, 5, 6, 9))])])
        cut = self._series("cut", {"freq": "weekly", "until": datetime(2030, 3, 19, 23, tzinfo=UTC)})
        grow = self._series("grow", {"freq": "weekly", "count": 6}, start=datetime(2030, 5, 6, 9))
        results = [self._poll(ordinary + [self._resource([cut]), self._resource([grow])]) for _ in range(2)]
        n_cut = len(self.events.search([("caldav_uid", "=", "cut")]))
        n_grow = len(self.events.search([("caldav_uid", "=", "grow")]))
        self._report("Q4", cut_active=n_cut, cut_expected=3, grow_active=n_grow, grow_expected=6, results=results)
        # Owner round-4 decision: preserve occurrence sets for remote rule
        # edits. A stale visible occurrence is safer than destructive rewriting.
        self.assertEqual((n_cut, n_grow), (6, 3))
        self.assertNotIn("ERROR", sum((poll[1] for poll in results), []))
        self.assertTrue(self.env["mail.activity"].sudo().search_count([
            ("res_id", "=", self.user.partner_id.id),
            ("summary", "=", "CalDAV: vyžaduje kontrolu (recurrence)"),
        ]))

    # Q5: forever series deleted remotely: meeting budget + approval -------------
    def test_q5_forever_series_deleted_needs_approval(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary + [self._resource([self._series("forever", {"freq": "weekly"})])])
        rows = self.events.search([("caldav_uid", "=", "forever")])
        r1 = [self._poll(ordinary) for _ in range(2)]
        blocked_active = len(rows.filtered("active"))
        approval_error = None
        try:
            self.events._caldav_approve_cleanup(self.user)
        except Exception as exc:
            approval_error = repr(exc)
        r2 = self._poll(ordinary)
        self._report("Q5", rows=len(rows), after_two_polls_active=blocked_active, polls=r1,
                     approval_error=approval_error, after_approval=r2, active=len(rows.filtered("active")))
        self.assertNotIn("ERROR", sum((p[1] for p in r1), []) + r2[1])
        self.assertEqual(sum(p[1].count("WARNING") for p in r1), 1)
        self.assertEqual(blocked_active, len(rows))
        self.assertIsNone(approval_error)
        self.assertFalse(rows.filtered("active"))

    # Q6: refusal paths stay ERROR-free and warn once ----------------------------
    def test_q6_refusals_error_gate(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary + [self._resource([self._series("int", {"freq": "weekly", "count": 4})])])
        changed = self._series("int", {"freq": "weekly", "interval": 2, "count": 4})
        interval = [self._poll(ordinary + [self._resource([changed])]) for _ in range(3)]
        empty = [self._poll([]) for _ in range(2)]
        self._report("Q6", interval=interval, empty=empty,
                     active=len(self.events.search([("user_id", "=", self.user.id)])))
        levels = sum((p[1] for p in interval + empty), [])
        self.assertNotIn("ERROR", levels)
        self.assertLessEqual(sum(1 for p in interval for lv in p[1] if lv == "WARNING"), 1)
