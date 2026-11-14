"""Reviewer A round 3 probes: origin backfill and remaining edges.

Synthetic only; remote mocked. Each test asserts the SAFE (or, where marked,
the intended LIVE) outcome; a failure records what actually happened.
"""
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import caldav
import icalendar
from caldav.lib.error import NotFoundError

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon


@tagged("post_install", "-at_install", "review_a3")
class TestReviewA3(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user("rev-a3", "synthetic", "synthetic", "https://calendar.example.test/")
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

    def _component(self, uid, start, title="Synthetic meeting", organizer=None):
        c = icalendar.Event()
        c.add("uid", uid)
        c.add("summary", title)
        c.add("dtstart", start.replace(tzinfo=UTC))
        c.add("dtend", (start + timedelta(hours=1)).replace(tzinfo=UTC))
        c.add("dtstamp", datetime(2040, 1, 1, tzinfo=UTC))
        if organizer:
            c.add("organizer", "mailto:" + organizer)
        c.add("attendee", "mailto:" + self.user.partner_id.email)
        return c

    def _resource(self, components):
        payload = icalendar.Calendar()
        for c in components:
            payload.add_component(c)
        return caldav.Event(data=payload.to_ical(), url="https://calendar.example.test/%s.ics" % components[0]["uid"])

    def _ordinary(self, n, prefix="ord"):
        return [self._resource([self._component("%s-%s" % (prefix, i), datetime(2030, 3, 1, 9) + timedelta(days=i))])
                for i in range(n)]

    def _poll(self, resources, user=None):
        self.calendar.events.return_value = resources
        return self.events._poll_user_caldav_server(user or self.user)

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

    def _detached_master(self, uid, first, count):
        m = self._component(uid, first, title="Historic")
        m.add("rrule", {"freq": "daily", "count": count})
        return self._resource([m])

    def _ledger(self):
        return self.events._caldav_safety_state(self.user)

    # P12: user-5 shape. Six detached groups (49 rows) are backfilled on the
    # first complete sync, then the six resources are omitted from one listing.
    def test_backfilled_detached_groups_survive_listing_omission(self):
        groups = []
        for g, size in enumerate((35, 2, 2, 2, 4, 4)):
            first = datetime(2022, 5, 2, 9) + timedelta(days=60 * g)
            groups.append((self._detached("hist-%s" % g, size, first), self._detached_master("hist-%s" % g, first, 2)))
        detached = self.events.browse()
        for rows, _master in groups:
            detached |= rows
        ordinary = self._ordinary(40)
        masters = [m for _rows, m in groups]
        out1 = self._poll(ordinary + masters)  # first complete sync: backfill
        proven = [i for i in detached.ids if self.events._caldav_origin_matches(
            self.events.browse(i), self._ledger(), self.events._caldav_account_identity(self.user))]
        out2 = self._poll(ordinary)  # one listing without the six historical resources
        detached.invalidate_recordset()
        hidden = len(detached.filtered(lambda e: not e.active))
        out3 = self._poll(ordinary + masters)  # resources back
        detached.invalidate_recordset()
        still_hidden = len(detached.filtered(lambda e: not e.active))
        self.assertEqual(hidden, 0, (
            "49 historical rows: %s backfilled as proven (first poll %s); one omission archived %s (%s); "
            "after the resources came back %s still hidden (%s)") % (
            len(proven), type(out1).__name__, hidden, type(out2).__name__, still_hidden, type(out3).__name__))

    # P13: a partial first sync must not backfill; the next complete one does.
    def test_partial_first_sync_does_not_backfill(self):
        legacy = self.events.with_context(caldav_no_sync=True, no_microsoft_sync=True).create({
            "name": "Legacy", "start": datetime(2030, 5, 5, 9), "stop": datetime(2030, 5, 5, 10),
            "user_id": self.user.id, "partner_id": self.user.partner_id.id, "caldav_uid": "legacy-1",
            "partner_ids": [(6, 0, self.user.partner_id.ids)],
        })
        ordinary = self._ordinary(10)
        legacy_res = self._resource([self._component("legacy-1", datetime(2030, 5, 5, 9), "Legacy")])
        original = type(self.events)._sync_event_from_ical

        def broken(model, payload, user, **kw):
            if str(payload.walk("VEVENT")[0]["uid"]) == "ord-0":
                raise ValueError("synthetic")
            return original(model, payload, user, **kw)
        with patch.object(type(self.events), "_sync_event_from_ical", autospec=True, side_effect=broken):
            self._poll(ordinary + [legacy_res])
        marker = self.env["ir.config_parameter"].sudo().get_param("caldav_sync.origin_backfill.%s" % self.user.id)
        ident = self.events._caldav_account_identity(self.user)
        self.assertFalse(marker, "backfill marker set by a partial sync")
        self.assertFalse(self.events._caldav_origin_matches(legacy, self._ledger(), ident),
                         "legacy row proven by a partial sync")
        self._poll(ordinary + [legacy_res])
        self.assertTrue(self.events._caldav_origin_matches(legacy, self._ledger(), ident),
                        "complete sync did not backfill a present legacy row")

    # P14: a legacy row with user_id = A that A's remote does not contain
    # (it came from B's LIVE import of an A-organised invite) stays unproven.
    def test_backfill_skips_row_absent_from_own_remote(self):
        foreign = self.events.with_context(caldav_no_sync=True, no_microsoft_sync=True).create({
            "name": "Imported by B", "start": datetime(2031, 1, 1, 9), "stop": datetime(2031, 1, 1, 10),
            "user_id": self.user.id, "partner_id": self.user.partner_id.id, "caldav_uid": "inv-from-a",
        })
        ordinary = self._ordinary(40)
        self._poll(ordinary)
        self._poll(ordinary)
        self.assertTrue(foreign.active, "a row A's remote never contained was archived after backfill")
        ident = self.events._caldav_account_identity(self.user)
        self.assertFalse(self.events._caldav_origin_matches(foreign, self._ledger(), ident))

    # P15: empty and near-empty listings.
    def test_near_empty_listing_thresholds(self):
        ordinary = self._ordinary(12)
        self._poll(ordinary)
        self._poll([])
        self.assertEqual(self.events.search_count([("caldav_uid", "like", "ord-%")]), 12, "empty listing archived")
        self._poll(ordinary[:2])  # 2 of 12 (16.7%) is above the 10% guard
        visible = self.events.search_count([("caldav_uid", "like", "ord-%")])
        self.assertEqual(visible, 12, "a 2-of-12 listing archived %s of 12" % (12 - visible))

    # P16 (LIVE semantics expected): origin proof older than 90 days.
    def test_origin_after_90_days(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary)
        later = fields.Datetime.now() + timedelta(days=91)
        with patch.object(fields.Datetime, "now", return_value=later):
            out = self._poll(ordinary[:-1])
        row = self.events.with_context(active_test=False).search([("caldav_uid", "=", "ord-29")])
        self.assertFalse(row.active, "a genuine remote deletion 91 days after import was not applied (%s)" % type(out).__name__)
