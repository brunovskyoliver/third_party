"""Reviewer B round 2 probes. Synthetic data only; every remote operation is mocked."""
import logging
from datetime import date, datetime, timedelta, UTC
from unittest.mock import MagicMock, patch

import caldav
import icalendar
import pytz
from caldav.elements import dav
from caldav.lib.error import NotFoundError

from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon

_logger = logging.getLogger("REVIEWB2")
MODEL_LOGGER = "odoo.addons.caldav_sync.models.calendar_event"


class _Count(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()[:160]))


@tagged("post_install", "-at_install", "reviewb2")
class TestReviewB2Probes(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user("reviewb2", "synthetic", "synthetic", "https://calendar.example.test/")
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
        logging.getLogger(MODEL_LOGGER).addHandler(self.handler)
        self.addCleanup(logging.getLogger(MODEL_LOGGER).removeHandler, self.handler)
        self._token = 0

    def _component(self, uid, start=None, title="Synthetic meeting", tz=None, hours=1, allday=False,
                   position=None, stamp=None):
        start = start or datetime(2030, 1, 15, 9)
        component = icalendar.Event()
        component.add("uid", uid)
        component.add("summary", title)
        if allday:
            component.add("dtstart", start.date() if isinstance(start, datetime) else start)
            component.add("dtend", (start.date() if isinstance(start, datetime) else start) + timedelta(days=1))
        elif tz:
            zone = pytz.timezone(tz)
            component.add("dtstart", zone.localize(start))
            component.add("dtend", zone.localize(start + timedelta(hours=hours)))
        else:
            component.add("dtstart", start.replace(tzinfo=UTC))
            component.add("dtend", (start + timedelta(hours=hours)).replace(tzinfo=UTC))
        if position is not None:
            component.add("recurrence-id", pytz.timezone(tz).localize(position) if tz else position.replace(tzinfo=UTC))
        component.add("dtstamp", stamp or datetime(2040, 1, 1, tzinfo=UTC))
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
        return outcome, [level for level, _msg in self.handler.records], list(self.handler.records)

    def _ordinary(self, size, prefix="ord"):
        return [self._resource([self._component("%s-%s" % (prefix, i), start=datetime(2030, 2, 1, 9) + timedelta(days=i))])
                for i in range(size)]

    def _series(self, uid, rrule, start=datetime(2030, 3, 4, 9), title="Series", tz=None, hours=1, allday=False):
        master = self._component(uid, start=start, title=title, tz=tz, hours=hours, allday=allday)
        master.add("rrule", rrule)
        return master

    def _report(self, name, **values):
        _logger.warning("REVIEWB2 %s %s", name, values)

    # P9: local "all events" duration change, remote echoes the new DTEND ----
    def test_p9_local_all_events_duration_change_no_duplicates(self):
        ordinary = self._ordinary(30)
        master = self._series("dur-series", {"freq": "weekly", "count": 4})
        self._poll(ordinary + [self._resource([master])])
        series = self.events.search([("caldav_uid", "=", "dur-series")], order="start")
        self.assertEqual(len(series), 4)
        with patch.object(type(self.events), "_sync_unlink_to_caldav", autospec=True), \
                patch.object(type(self.events), "_sync_write_to_caldav", autospec=True):
            series[0].write({"stop": series[0].stop + timedelta(minutes=30), "recurrence_update": "all_events"})
        new_rows = self.events.search([("user_id", "=", self.user.id), ("recurrence_id", "!=", False)])
        new_uid = new_rows[:1].caldav_uid
        echoed = self._series(new_uid, {"freq": "weekly", "count": 4}, hours=1.5)
        results = [self._poll(ordinary + [self._resource([echoed])]) for _ in range(2)]
        active = self.events.search([("caldav_uid", "in", [new_uid, "dur-series"])])
        self._report("P9", new_uid_differs=new_uid != "dur-series", local_rows=len(new_rows), active=len(active),
                     outcomes=[repr(r[0]) for r in results], levels=[r[1] for r in results], msgs=results[-1][2][:4])
        self.assertEqual(len(active), 4)
        self.assertNotIn("ERROR", sum((r[1] for r in results), []))

    # P10: meeting created locally in Odoo (pushed to CalDAV), later deleted on the phone
    def test_p10_local_created_event_deleted_remotely(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary)
        pushed = MagicMock()
        pushed.vobject_instance.vevent.uid.value = "odoo-local-1"
        self.calendar.save_event.return_value = pushed
        local = self.events.with_context(no_microsoft_sync=True).create({
            "name": "Created in Odoo", "start": datetime(2030, 4, 1, 9), "stop": datetime(2030, 4, 1, 10),
            "user_id": self.user.id, "partner_ids": [(6, 0, self.user.partner_id.ids)],
        })
        self.assertIn(str(local.id), self.events._caldav_safety_state(self.user)["created"])
        echoed = self._resource([self._component("odoo-local-1", start=datetime(2030, 4, 1, 9), title="Created in Odoo")])
        self._poll(ordinary + [echoed])
        results = [self._poll(ordinary) for _ in range(2)]
        self._report("P10", active=local.active, outcomes=[repr(r[0]) for r in results], levels=[r[1] for r in results])
        self.assertFalse(local.active, "Odoo-created meeting deleted remotely stays visible")

    # P15: meeting imported before deploy (no ledger entry), later deleted remotely
    def test_p15_legacy_imported_event_deleted_remotely(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary)
        self.env["ir.config_parameter"].sudo().set_param("caldav_sync.safety.%s" % self.user.id, "")
        gone = self.events.search([("caldav_uid", "=", "ord-29")])
        results = [self._poll(ordinary[:-1]) for _ in range(3)]
        health = self.user._caldav_health()
        self._report("P15", active=gone.active, outcomes=[repr(r[0]) for r in results],
                     levels=[r[1] for r in results], health=health.get("failure_class"), streak=health.get("streak"))
        # No origin proof means the safer result is a visible stale row. The
        # warning is deduplicated and does not poison health forever.
        self.assertTrue(gone.active, "unproven pre-deploy meeting must remain visible")
        self.assertNotIn("failure_class", health)

    # P11: remote shortens a normal series ("delete this and following") -------
    def test_p11_remote_until_change_on_normal_series(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary + [self._resource([self._series("cut-series", {"freq": "weekly", "count": 6})])])
        series = self.events.search([("caldav_uid", "=", "cut-series")])
        self.assertEqual(len(series), 6)
        cut = self._series("cut-series", {"freq": "weekly", "until": datetime(2030, 3, 19, 23, tzinfo=UTC)})
        results = [self._poll(ordinary + [self._resource([cut])]) for _ in range(2)]
        active = self.events.search([("caldav_uid", "=", "cut-series")])
        self._report("P11", active=len(active), outcomes=[repr(r[0]) for r in results], levels=[r[1] for r in results],
                     msgs=results[-1][2][:3])
        self.assertNotIn("ERROR", sum((r[1] for r in results), []))

    # P11b: remote changes weekday / interval of a series -----------------------
    def test_p11b_remote_interval_change(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary + [self._resource([self._series("int-series", {"freq": "weekly", "count": 4})])])
        changed = self._series("int-series", {"freq": "weekly", "interval": 2, "count": 4})
        results = [self._poll(ordinary + [self._resource([changed])]) for _ in range(2)]
        self._report("P11b", active=len(self.events.search([("caldav_uid", "=", "int-series")])),
                     outcomes=[repr(r[0]) for r in results], levels=[r[1] for r in results])
        self.assertNotIn("ERROR", sum((r[1] for r in results), []))

    # P12: remote renames of ordinary series shapes (no recurrence change) -----
    def test_p12_remote_rename_series_shapes(self):
        shapes = {
            "count": {"freq": "weekly", "count": 4},
            "forever": {"freq": "weekly"},
            "until": {"freq": "weekly", "until": datetime(2030, 4, 30, 23, tzinfo=UTC)},
            "byday": {"freq": "weekly", "byday": ["MO", "WE"], "count": 6},
            "monthly": {"freq": "monthly", "bymonthday": 4, "count": 3},
        }
        ordinary = self._ordinary(30)
        for name, rule in shapes.items():
            with self.subTest(shape=name):
                uid = "rename-%s" % name
                self._poll(ordinary + [self._resource([self._series(uid, rule, tz="Europe/Bratislava")])])
                before = len(self.events.search([("caldav_uid", "=", uid)]))
                renamed = self._series(uid, rule, title="Renamed", tz="Europe/Bratislava")
                outcome, levels, msgs = self._poll(ordinary + [self._resource([renamed])])
                rows = self.events.search([("caldav_uid", "=", uid)])
                self._report("P12", shape=name, before=before, after=len(rows), names=sorted(set(rows.mapped("name"))),
                             outcome=repr(outcome), levels=levels, msgs=msgs[:2])
                self.assertNotIn("ERROR", levels)
                self.assertEqual(
                    rows.recurrence_id.base_event_id.name,
                    "Renamed",
                )

    # P12b: remote moves one occurrence of a normal series (override) ----------
    def test_p12b_remote_override_on_normal_series(self):
        ordinary = self._ordinary(30)
        rule = {"freq": "weekly", "count": 4}
        self._poll(ordinary + [self._resource([self._series("ovr-series", rule, tz="Europe/Bratislava")])])
        master = self._series("ovr-series", rule, tz="Europe/Bratislava")
        moved = self._component("ovr-series", start=datetime(2030, 3, 18, 13), title="Moved one",
                                tz="Europe/Bratislava", position=datetime(2030, 3, 18, 9))
        outcome, levels, msgs = self._poll(ordinary + [self._resource([master, moved])])
        rows = self.events.search([("caldav_uid", "=", "ovr-series")], order="start")
        self._report("P12b", n=len(rows), names=rows.mapped("name"), starts=[str(s) for s in rows.mapped("start")],
                     outcome=repr(outcome), levels=levels, msgs=msgs[:2])
        self.assertNotIn("ERROR", levels)
        self.assertIn("Moved one", rows.mapped("name"))

    # P13: all-day single and all-day series ----------------------------------
    def test_p13_allday(self):
        ordinary = self._ordinary(30)
        single = self._component("allday-1", start=datetime(2030, 5, 6), allday=True, title="Holiday")
        series = self._series("allday-series", {"freq": "weekly", "count": 3}, start=datetime(2030, 5, 7), allday=True)
        self._poll(ordinary + [self._resource([single]), self._resource([series])])
        single2 = self._component("allday-1", start=datetime(2030, 5, 8), allday=True, title="Holiday moved")
        series2 = self._series("allday-series", {"freq": "weekly", "count": 3}, start=datetime(2030, 5, 7), allday=True, title="AD renamed")
        outcome, levels, msgs = self._poll(ordinary + [self._resource([single2]), self._resource([series2])])
        one = self.events.search([("caldav_uid", "=", "allday-1")])
        ser = self.events.search([("caldav_uid", "=", "allday-series")])
        self._report("P13", single=(one.name, str(one.start_date), one.allday), series=(len(ser), sorted(set(ser.mapped("name")))),
                     outcome=repr(outcome), levels=levels, msgs=msgs[:2])
        self.assertNotIn("ERROR", levels)
        self.assertEqual(one.name, "Holiday moved")

    # P16: attendee change on single event -------------------------------------
    def test_p16_attendee_added_remotely(self):
        ordinary = self._ordinary(30)
        other = self.env["res.partner"].create({"name": "Synthetic guest", "email": "guest@example.test"})
        self._poll(ordinary + [self._resource([self._component("att-1")])])
        comp = self._component("att-1")
        comp.add("attendee", "mailto:guest@example.test")
        outcome, levels, _ = self._poll(ordinary + [self._resource([comp])])
        event = self.events.search([("caldav_uid", "=", "att-1")])
        self._report("P16", guest=other in event.partner_ids, levels=levels, outcome=repr(outcome))
        self.assertIn(other, event.partner_ids)

    # P17: query counts and ledger size ---------------------------------------
    def test_p17_query_counts_and_ledger(self):
        resources = self._ordinary(150)
        resources.append(self._resource([self._series("perf-series", {"freq": "weekly", "count": 30})]))
        self.env.flush_all()
        q0 = self.env.cr.sql_log_count
        self._poll(resources)
        self.env.flush_all()
        q1 = self.env.cr.sql_log_count
        resources[0] = self._resource([self._component("ord-0", start=datetime(2030, 2, 1, 9), title="Changed")])
        self._poll(resources)
        self.env.flush_all()
        q2 = self.env.cr.sql_log_count
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-%s" % self._token}
        self.calendar.events.return_value = resources
        self.events._poll_user_caldav_server(self.user)
        q3 = self.env.cr.sql_log_count
        ledger = self.env["ir.config_parameter"].sudo().get_param("caldav_sync.safety.%s" % self.user.id, "")
        self._report("P17", full=q1 - q0, one_changed=q2 - q1, unchanged=q3 - q2, ledger_bytes=len(ledger))

    # P12c: single timed event moved remotely -----------------------------------
    def test_p12c_single_event_time_change(self):
        ordinary = self._ordinary(30)
        self._poll(ordinary + [self._resource([self._component("single-move", tz="Europe/Bratislava")])])
        moved = self._component("single-move", start=datetime(2030, 1, 15, 14), tz="Europe/Bratislava", title="Moved")
        outcome, levels, msgs = self._poll(ordinary + [self._resource([moved])])
        event = self.events.search([("caldav_uid", "=", "single-move")])
        self._report("P12c", title=event.name, start=str(event.start), outcome=repr(outcome), levels=levels)
        self.assertNotIn("ERROR", levels)
        self.assertEqual(event.name, "Moved")

    # P12d: whole series moved one hour remotely -------------------------------
    def test_p12d_series_time_change(self):
        ordinary = self._ordinary(30)
        rule = {"freq": "weekly", "count": 4}
        self._poll(ordinary + [self._resource([self._series("series-move", rule, tz="Europe/Bratislava")])])
        moved = self._series("series-move", rule, start=datetime(2030, 3, 4, 10), tz="Europe/Bratislava", title="Later")
        outcome, levels, msgs = self._poll(ordinary + [self._resource([moved])])
        rows = self.events.search([("caldav_uid", "=", "series-move")], order="start")
        self._report("P12d", n=len(rows), names=rows.mapped("name"), starts=[str(x) for x in rows.mapped("start")],
                     outcome=repr(outcome), levels=levels)
        self.assertNotIn("ERROR", levels)
