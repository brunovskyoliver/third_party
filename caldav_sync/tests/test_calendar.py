from collections.abc import Iterable
from odoo.tests import TransactionCase, tagged
from odoo import Command
from unittest.mock import patch, MagicMock, DEFAULT
import icalendar
from pathlib import Path
from .common import CaldavTestCommon
from contextlib import contextmanager
from datetime import date, datetime, UTC, timedelta
import caldav
from odoo.addons.microsoft_calendar.models.microsoft_sync import MicrosoftCalendarSync

WEEKDAY_MAP = {
    0: "SUN",
    1: "MON",
    2: "TUE",
    3: "WED",
    4: "THU",
    5: "FRI",
    6: "SAT",
}


def _get_ics_path(filename):
    return Path(__file__).parent / "data" / filename


@contextmanager
def _patch_caldav_with_events_from_ics(
    ics_paths, user, last_modified=None, futurize=True
):
    with patch("caldav.DAVClient") as MockDAVClient:
        mock_client = MockDAVClient.return_value
        mock_calendars = {}

        def calendar_side_effect(url):
            if url not in mock_calendars:
                mock_cal = MagicMock()
                mock_cal.events = MagicMock(return_value=[])
                mock_cal.event_by_uid = MagicMock()
                mock_calendars[url] = mock_cal
            return mock_calendars[url]

        mock_client.calendar = calendar_side_effect

        # Get or create the mock calendar for this user
        mock_calendar = calendar_side_effect(user.caldav_calendar_url)

        def event_by_uid_side_effect(uid):
            for event in mock_calendar.events():
                if str(event.icalendar_component.get("uid")) == uid:
                    return event
            return DEFAULT

        ical_events = []
        if ics_paths:
            if not isinstance(ics_paths, Iterable):
                ics_paths = [ics_paths] if ics_paths else []
            for ics_path in ics_paths:
                with ics_path.open("rb") as file:
                    ical_content = file.read()
                ical_events.append(icalendar.Calendar.from_ical(ical_content))
        if last_modified:
            for event in ical_events:
                for subcomponent in event.subcomponents:
                    if subcomponent.name == "VEVENT":
                        subcomponent["last-modified"] = icalendar.vDate(last_modified)
                        subcomponent["dtstamp"] = icalendar.vDate(last_modified)
        if futurize:
            for event in ical_events:
                for subcomponent in event.subcomponents:
                    if subcomponent.name == "VEVENT":
                        start = subcomponent.get("dtstart") and subcomponent.decoded(
                            "dtstart"
                        )
                        end = subcomponent.get("dtend") and subcomponent.decoded(
                            "dtend"
                        )
                        if isinstance(start, datetime) and isinstance(end, datetime):
                            duration = end - start
                        else:
                            duration = timedelta(hours=1)
                        subcomponent["dtstart"] = icalendar.vDDDTypes(datetime.now())
                        subcomponent["dtend"] = icalendar.vDDDTypes(
                            datetime.now() + duration
                        )

        base_events = [event for event in ical_events if not event.get("recurrence-id")]
        for base_event in base_events:
            child_events = [
                event
                for event in ical_events
                if event.get("recurrence-id")
                and event.get("uid") == base_event.get("uid")
            ]
            for child_event in child_events:
                base_event.add_component(child_event)
            mock_calendar.add_event(base_event)
        caldav_events = [caldav.Event(data=event) for event in base_events]
        mock_calendar.events.return_value = caldav_events
        mock_calendar.event_by_uid.side_effect = event_by_uid_side_effect
        user._compute_is_caldav_enabled()
        yield


@tagged("post_install", "-at_install")
class TestCalendarEvent(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.users"].search([])._compute_is_caldav_enabled()
        cls.user_1_url = "https://mycaldav.test.com/test1calendar"
        cls.user_1 = cls._generate_user(
            "test1",
            caldav_username="user1",
            caldav_password="pass1",
            caldav_url=cls.user_1_url,
        )
        cls.user_2_url = "https://mycaldav.test.com/test2calendar"
        cls.user_2 = cls._generate_user(
            "test2",
            caldav_username="user2",
            caldav_password="pass2",
            caldav_url=cls.user_2_url,
        )
        cls.user_3_url = "https://mycaldav.test.com/test3calendar"
        cls.user_3 = cls._generate_user(
            "test3",
            caldav_username="user3",
            caldav_password="pass3",
            caldav_url=cls.user_3_url,
        )
        # Imported invitees must already exist; the bridge does not create contacts.
        cls.env["res.partner"].create([
            {"name": "Synthetic partner 1", "email": "partner1@example.com"},
            {"name": "Synthetic partner 2", "email": "partner2@example.com"},
        ])

    def test_attendee_without_email_never_matches_contacts_without_email(self):
        # Prod 2026-10-08: an empty address matched the first of ~11k contacts
        # without email (a Socio client) on 149 staff events.
        no_email = self.env["res.partner"].create({"name": "AAA synthetic contact without email"})
        event = icalendar.Event()
        event.add("attendee", "mailto:partner1@example.com")
        event.add("attendee", "CN=No address")
        event.add("organizer", "CN=No address either")
        partners = self.env["calendar.event"]._get_attendee_partners(event, self.user_1.partner_id.email)
        self.assertNotIn(no_email, partners)
        self.assertIn(self.user_1.partner_id, partners)
        self.assertEqual(partners.filtered(lambda partner: partner.email == "partner1@example.com").mapped("name"),
                         ["Synthetic partner 1"])
        self.assertFalse(self.env["calendar.event"]._get_organizer_partner(event))
        self.assertFalse(self.env["calendar.event"]._get_attendee_partners(icalendar.Event(), ""))

    def test_basic_event_from_server_create(self):
        user = self.user_1
        ics_path = _get_ics_path("basic.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            current_events = self.env["calendar.event"].search([])
            self.env["calendar.event"].poll_caldav_server()
            events_after_sync = self.env["calendar.event"].search([])
            new_events = events_after_sync - current_events
            self.assertEqual(len(new_events), 1)

    def test_basic_past_event_from_server_create(self):
        user = self.user_1
        ics_path = _get_ics_path("basic.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user, futurize=False):
            current_events = self.env["calendar.event"].search([])
            self.env["calendar.event"].poll_caldav_server()
            events_after_sync = self.env["calendar.event"].search([])
            new_events = events_after_sync - current_events
            self.assertEqual(len(new_events), 1)

    def test_basic_event_from_server_update(self):
        user = self.user_1
        ics_path = _get_ics_path("basic.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()

        # Verify the event was created correctly
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        self.assertEqual(event.name, "Test")
        orig_start = event.start
        orig_stop = event.stop

        # Now update the event with the updated ICS data
        ics_path = _get_ics_path("basic_updated.ics")
        with _patch_caldav_with_events_from_ics(
            ics_path,
            user,
            last_modified=(datetime.now(UTC)),
        ):
            # Clear any caches to ensure fresh data
            self.env["calendar.event"].invalidate_model()
            self.env["calendar.event"].poll_caldav_server()

        # Refresh the event from the database to get updated values
        event.invalidate_recordset()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])

        # Verify the event was updated correctly
        self.assertEqual(event.name, "Test Updated")
        # This next one is just lazy avoiding the HTML stripping
        self.assertIn("Some note ...", event.description)
        self.assertGreater(event.start, orig_start)
        self.assertGreater(event.stop, orig_stop)

    def test_basic_event_from_server_delete(self):
        user = self.user_1
        ics_path = _get_ics_path("basic.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        # Passing None to ics_path means no events returned from server
        with _patch_caldav_with_events_from_ics(None, user):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        self.assertFalse(event)

    def test_microsoft_event_is_not_removed_by_caldav_orphan_cleanup(self):
        user = self.user_1
        event = self._create_microsoft_event(user, "microsoft-event-uid")

        with _patch_caldav_with_events_from_ics(None, user):
            self.env["calendar.event"].poll_caldav_server()

        self.assertTrue(event.exists())

    def test_caldav_import_does_not_insert_to_microsoft(self):
        user = self.user_1
        with patch.object(MicrosoftCalendarSync, "_microsoft_insert") as microsoft_insert:
            with patch.object(
                type(user), "_get_microsoft_sync_status", return_value="sync_active"
            ):
                event = (
                    self.env["calendar.event"]
                    .with_user(user)
                    .with_context(
                        caldav_no_sync=True,
                        caldav_keep_ids=True,
                        calendar_sync_source="caldav",
                        no_microsoft_sync=True,
                    )
                    .create(
                        {
                            "name": "CalDAV event",
                            "user_id": user.id,
                            "caldav_uid": "caldav-created-event",
                            "need_sync_m": True,
                            "start": datetime.now() + timedelta(days=2),
                            "stop": datetime.now() + timedelta(days=2, hours=1),
                        }
                    )
                )

        self.assertTrue(event.exists())
        self.assertFalse(event.need_sync_m)
        microsoft_insert.assert_not_called()

    def test_caldav_write_does_not_patch_microsoft(self):
        user = self.user_1
        event = self._create_microsoft_event(user, "microsoft-patch-event")
        with patch.object(MicrosoftCalendarSync, "_microsoft_patch") as microsoft_patch:
            with patch.object(
                type(user), "_get_microsoft_sync_status", return_value="sync_active"
            ):
                event.with_context(
                    caldav_no_sync=True,
                    calendar_sync_source="caldav",
                    no_microsoft_sync=True,
                ).write({"name": "Changed by CalDAV"})

        self.assertEqual(event.name, "Changed by CalDAV")
        self.assertFalse(event.need_sync_m)
        microsoft_patch.assert_not_called()

    def test_caldav_archive_does_not_delete_from_microsoft(self):
        user = self.user_1
        event = self._create_microsoft_event(user, "microsoft-archive-event")
        with patch.object(MicrosoftCalendarSync, "_microsoft_delete") as microsoft_delete:
            with patch.object(
                type(user), "_get_microsoft_sync_status", return_value="sync_active"
            ):
                event.with_context(
                    caldav_no_sync=True,
                    calendar_sync_source="caldav",
                    no_microsoft_sync=True,
                ).write({"active": False})

        self.assertFalse(event.active)
        microsoft_delete.assert_not_called()

    def test_microsoft_delete_log_only_mode(self):
        user = self.user_1
        parameter = self.env["ir.config_parameter"].sudo()
        parameter.set_param("caldav_sync.microsoft_delete_mode", "log_only")
        event = self._create_microsoft_event(user, "microsoft-log-only-event")

        with patch.object(MicrosoftCalendarSync, "_microsoft_delete") as microsoft_delete:
            with patch.object(
                type(user), "_get_microsoft_sync_status", return_value="sync_active"
            ):
                with self.assertLogs(
                    "odoo.addons.caldav_sync.models.calendar_event", level="WARNING"
                ) as logs:
                    event.write({"active": False})

        self.assertFalse(event.active)
        microsoft_delete.assert_not_called()
        self.assertIn("temporary log-only mode", "\n".join(logs.output))

    def test_caldav_unlink_does_not_delete_from_microsoft(self):
        user = self.user_1
        event = self._create_microsoft_event(user, "microsoft-unlink-event")
        with patch.object(MicrosoftCalendarSync, "_microsoft_delete") as microsoft_delete:
            with patch.object(
                type(user), "_get_microsoft_sync_status", return_value="sync_active"
            ):
                event.with_context(
                    caldav_no_sync=True,
                    calendar_sync_source="caldav",
                    no_microsoft_sync=True,
                ).unlink()

        self.assertFalse(event.exists())
        microsoft_delete.assert_not_called()

    def test_microsoft_import_does_not_get_a_caldav_uid(self):
        user = self.user_1
        event = (
            self.env["calendar.event"]
            .with_user(user)
            .with_context(caldav_no_sync=True)
            .create(
                {
                    "name": "Microsoft event without CalDAV UID",
                    "user_id": user.id,
                    "microsoft_id": "microsoft-event-id",
                    "ms_universal_event_id": "microsoft-universal-id",
                    "start": datetime.now() + timedelta(days=2),
                    "stop": datetime.now() + timedelta(days=2, hours=1),
                }
            )
        )

        self.assertFalse(event.caldav_uid)

    def test_caldav_does_not_update_legacy_microsoft_uid(self):
        user = self.user_1
        event = self._create_microsoft_event(
            user, "5C753EFA-DA9C-479A-81CE-5A5421518EAE"
        )

        with _patch_caldav_with_events_from_ics(_get_ics_path("basic.ics"), user):
            self.env["calendar.event"].poll_caldav_server()

        self.assertEqual(event.name, "Microsoft event")
        self.assertEqual(
            len(
                self.env["calendar.event"].search(
                    [("caldav_uid", "=", event.caldav_uid)]
                )
            ),
            1,
        )

    def test_caldav_orphan_cleanup_skips_incomplete_snapshot(self):
        user = self.user_1
        event = (
            self.env["calendar.event"]
            .with_user(user)
            .with_context(caldav_no_sync=True, caldav_keep_ids=True)
            .create(
                {
                    "name": "CalDAV event",
                    "user_id": user.id,
                    "caldav_uid": "incomplete-snapshot-event",
                    "start": datetime.now() + timedelta(days=2),
                    "stop": datetime.now() + timedelta(days=2, hours=1),
                }
            )
        )

        with patch("caldav.DAVClient") as MockDAVClient:
            mock_calendar = MagicMock()
            mock_calendar.events.return_value = [
                MagicMock(data="synthetic-invalid-event", url=None, icalendar_instance=object())
            ]
            MockDAVClient.return_value.calendar.return_value = mock_calendar
            user._compute_is_caldav_enabled()
            self.env["calendar.event"]._poll_user_caldav_server(user)

        self.assertTrue(event.exists())

    def test_yearly_series_is_not_synced_and_existing_copy_is_removed(self):
        user = self.user_1
        old_copy = (
            self.env["calendar.event"]
            .with_user(user)
            .with_context(caldav_no_sync=True, caldav_keep_ids=True, no_microsoft_sync=True, calendar_sync_source="caldav")
            .create(
                {
                    "name": "Synthetic yearly",
                    "user_id": user.id,
                    "caldav_uid": "synthetic-yearly-uid",
                    "start": datetime.now() + timedelta(days=3),
                    "stop": datetime.now() + timedelta(days=3, hours=1),
                }
            )
        )
        yearly = icalendar.Calendar()
        component = icalendar.Event()
        component.add("uid", "synthetic-yearly-uid")
        component.add("summary", "Synthetic yearly")
        component.add("dtstart", datetime(2026, 11, 2, 9, 0))
        component.add("dtend", datetime(2026, 11, 2, 10, 0))
        component.add("rrule", {"freq": "yearly"})
        yearly.add_component(component)

        with patch("caldav.DAVClient") as MockDAVClient:
            mock_calendar = MagicMock()
            mock_calendar.events.return_value = [
                MagicMock(
                    data=yearly.to_ical(),
                    url="yearly.ics",
                    icalendar_instance=icalendar.Calendar.from_ical(yearly.to_ical()),
                )
            ]
            MockDAVClient.return_value.calendar.return_value = mock_calendar
            user._compute_is_caldav_enabled()
            self.env["calendar.event"]._poll_user_caldav_server(user)
            mock_calendar.event_by_uid.return_value.delete.assert_not_called()

        self.assertFalse(old_copy.exists())
        self.assertFalse(
            self.env["calendar.event"].search([("caldav_uid", "=", "synthetic-yearly-uid")])
        )

    def test_existing_recurrence_uid_is_reused_after_base_uid_drift(self):
        user = self.user_1
        ics_path = _get_ics_path("test_recurring.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()

        uid = "F777C16F-2F14-4326-9681-69C6043DB8C0"
        recurrence = self.env["calendar.recurrence"].search(
            [("caldav_uid", "=", uid)], limit=1
        )
        self.assertTrue(recurrence)
        recurrence.base_event_id.with_context(caldav_no_sync=True).write(
            {"caldav_uid": False}
        )

        with self.assertLogs(
            "odoo.addons.caldav_sync.models.calendar_event", level="INFO"
        ) as logs:
            with _patch_caldav_with_events_from_ics(ics_path, user):
                self.env["calendar.event"].poll_caldav_server()

        log_output = "\n".join(logs.output)
        self.assertNotIn("calendar_recurrence_caldav_uid_unique", log_output)
        self.assertNotIn("orphan cleanup will be skipped", log_output)
        self.assertEqual(
            len(
                self.env["calendar.recurrence"].search(
                    [("caldav_uid", "=", uid)]
                )
            ),
            1,
        )

    def test_matches_caldav_start_accepts_date_only_event(self):
        user = self.user_1
        event = (
            self.env["calendar.event"]
            .with_user(user)
            .with_context(caldav_no_sync=True)
            .create(
                {
                    "name": "All-day event",
                    "user_id": user.id,
                    "start": datetime(2026, 1, 15, 9),
                    "stop": datetime(2026, 1, 16, 9),
                }
            )
        )
        component = icalendar.Event()
        component.add("dtstart", date(2026, 1, 15))
        caldav_event = MagicMock(icalendar_component=component)

        self.assertTrue(event._matches_caldav_start(caldav_event))

    def test_recurring_from_server_create(self):
        user = self.user_1
        ics_path = _get_ics_path("test_recurring.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        events = self.env["calendar.event"].search(
            [("partner_id", "=", user.partner_id.id)]
        )
        self.assertEqual(len(events), 10)

    def test_multiple_attendees_event_from_server_create(self):
        user = self.user_1
        ics_path = _get_ics_path("test_multi_attendee.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        self.assertEqual(len(event.attendee_ids), 3)
        self.assertIn(user.partner_id, event.attendee_ids.partner_id)

    def test_multiple_attendees_event_from_server_update(self):
        user = self.user_1
        ics_path = _get_ics_path("test_multi_attendee.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        ics_path = _get_ics_path("test_multi_attendee_update.ics")
        with _patch_caldav_with_events_from_ics(
            ics_path, user, last_modified=datetime.now(UTC)
        ):
            self.env["calendar.event"].poll_caldav_server()
        self.assertEqual(len(event.attendee_ids), 2)
        self.assertIn(user.partner_id, event.attendee_ids.partner_id)

    def test_multiple_attendees_event_from_server_delete(self):
        user = self.user_1
        ics_path = _get_ics_path("test_multi_attendee.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user):
            self.env["calendar.event"].poll_caldav_server()
        # Passing None as ics_path means no events returned from server
        with _patch_caldav_with_events_from_ics(None, user):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search([("user_id", "=", user.id)])
        self.assertFalse(event)

    def test_multiple_user_attendees_event_from_server_create(self):
        """Test event has:
        Organizer: user1 (test1@example.com)
        Attendees: user2 and user3 (test2@example.com, test3@example.com)
        """
        user1 = self.user_1
        user2 = self.user_2
        user3 = self.user_3
        ics_path = _get_ics_path("test_multi_user.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user1):
            self.env["calendar.event"].poll_caldav_server()
        with _patch_caldav_with_events_from_ics(ics_path, user2):
            self.env["calendar.event"].poll_caldav_server()
        with _patch_caldav_with_events_from_ics(ics_path, user3):
            self.env["calendar.event"].poll_caldav_server()
        event = self.env["calendar.event"].search(
            [("caldav_uid", "=", "2495546B-5C9A-4632-AAD3-A179EF83CF20")]
        )
        self.assertEqual(len(event), 1)
        # Make sure the event wasn't duplicated all over the place
        other_user_events = self.env["calendar.event"].search(
            [("user_id", "in", [user2.id, user3.id])]
        )
        self.assertFalse(other_user_events)
        self.assertIn(user2.partner_id, event.partner_ids)
        self.assertIn(user3.partner_id, event.partner_ids)

    def test_multiple_user_attendees_event_from_server_update(self):
        """Test event has (as in above test):
        Organizer: user1 (test1@example.com)
        Attendees: user2 and user3 (test2@example.com, test3@example.com)
        """
        user1 = self.user_1
        user2 = self.user_2
        user3 = self.user_3
        ics_path = _get_ics_path("test_multi_user.ics")
        with _patch_caldav_with_events_from_ics(ics_path, user1):
            self.env["calendar.event"].poll_caldav_server()
        with _patch_caldav_with_events_from_ics(ics_path, user2):
            self.env["calendar.event"].poll_caldav_server()
        with _patch_caldav_with_events_from_ics(ics_path, user3):
            self.env["calendar.event"].poll_caldav_server()
        notification_method = "odoo.addons.calendar.models.calendar_attendee.CalendarAttendee._notify_attendees"
        # Now update it to remove one attendee
        # Shuffle the user polling order just to test more robustly
        ics_path = _get_ics_path("test_multi_user_update.ics")
        with _patch_caldav_with_events_from_ics(
            ics_path, user2, last_modified=datetime.now(UTC)
        ), patch(notification_method) as mock_notification_method:
            self.env["calendar.event"].poll_caldav_server()
            mock_notification_method.assert_not_called()
        with _patch_caldav_with_events_from_ics(
            ics_path, user3, last_modified=datetime.now(UTC)
        ), patch(notification_method) as mock_notification_method:
            self.env["calendar.event"].poll_caldav_server()
            mock_notification_method.assert_not_called()
        with _patch_caldav_with_events_from_ics(
            ics_path, user1, last_modified=datetime.now(UTC)
        ), patch(notification_method) as mock_notification_method:
            self.env["calendar.event"].poll_caldav_server()
            mock_notification_method.assert_not_called()
        event = self.env["calendar.event"].search(
            [("caldav_uid", "=", "2495546B-5C9A-4632-AAD3-A179EF83CF20")]
        )
        self.assertIn(user3.partner_id, event.partner_ids)
        self.assertNotIn(user2.partner_id, event.partner_ids)
        self.assertEqual(len(event.attendee_ids), 2)

    def _create_multi_user_test_event(self):
        return (
            self.env["calendar.event"]
            .with_user(self.user_1)
            .create(
                {
                    "name": "Test event",
                    "partner_ids": [
                        Command.set(
                            [
                                self.user_2.partner_id.id,
                                self.user_3.partner_id.id,
                                self.user_1.partner_id.id,
                            ]
                        ),
                        Command.create(
                            {
                                "name": "Test partner",
                                "email": "testpartner@example.com",
                            }
                        ),
                    ],
                    "start": datetime.now() + timedelta(days=2),
                    "stop": datetime.now() + timedelta(days=2, hours=1),
                }
            )
        )

    def test_event_data_contains_dtstamp(self):
        event = self.env["calendar.event"].with_context(caldav_no_sync=True).with_user(self.user_1).create({
            "name": "Test event",
            "start": datetime.now() + timedelta(days=2),
            "stop": datetime.now() + timedelta(days=2, hours=1),
        })
        self.assertIn("dtstamp", event._create_event_data())

    def test_get_outdated_accepts_naive_dtstamp(self):
        event = self.env["calendar.event"].with_context(caldav_no_sync=True).with_user(self.user_1).create({
            "name": "Existing event",
            "start": datetime.now() + timedelta(days=2),
            "stop": datetime.now() + timedelta(days=2, hours=1),
        })
        component = MagicMock()
        component.get.return_value.dt = datetime(2000, 1, 1, 0, 0, 0)

        self.assertTrue(
            self.env["calendar.event"]._get_outdated(
                component, event, self.env["calendar.event"]
            )
        )

    def test_missing_remote_event_is_recreated(self):
        with self._patch_all_3_users_davclients() as (_, mock_calendar):
            event = (
                self.env["calendar.event"]
                .with_user(self.user_1)
                .with_context(caldav_no_sync=True, caldav_keep_ids=True)
                .create(
                    {
                        "name": "Missing remote event",
                        "user_id": self.user_1.id,
                        "caldav_uid": "missing-remote-event",
                        "start": datetime.now() + timedelta(days=2),
                        "stop": datetime.now() + timedelta(days=2, hours=1),
                    }
                )
            )
            mock_calendar.events.return_value = []

            self.assertTrue(event._is_caldav_enabled())
            self.assertIn(self.user_1, event.caldav_user_ids)

            event.with_context(caldav_no_sync=False).write(
                {"name": "Recreated remote event"}
            )

            mock_calendar.add_event.assert_called_once()

    def test_recurrency_values_accept_date_only_until(self):
        component = icalendar.Event()
        component.add("dtstart", datetime(2026, 1, 1, tzinfo=UTC))
        component.add("rrule", {"freq": "daily", "until": date(2026, 1, 3)})

        values = self.env["calendar.event"]._get_recurrency_values_from_ical_event(
            component
        )

        self.assertEqual(values["end_type"], "end_date")

    @contextmanager
    def _patch_all_3_users_davclients(self):
        with patch("caldav.DAVClient") as MockDAVClient:
            (self.user_1 | self.user_2 | self.user_3)._compute_is_caldav_enabled()

            mock_client = MockDAVClient.return_value
            mock_calendar = MagicMock()
            mock_event_by_uid = MagicMock()
            mock_client.calendar.return_value = mock_calendar
            mock_calendar.events.return_value = []
            mock_calendar.event_by_uid.return_value = mock_event_by_uid
            yield mock_client, mock_calendar

    def _create_microsoft_event(self, user, caldav_uid):
        return (
            self.env["calendar.event"]
            .with_user(user)
            .with_context(caldav_no_sync=True, caldav_keep_ids=True)
            .create(
                {
                    "name": "Microsoft event",
                    "user_id": user.id,
                    "caldav_uid": caldav_uid,
                    "microsoft_id": "microsoft-event-id",
                    "ms_universal_event_id": "microsoft-universal-id",
                    "microsoft_recurrence_master_id": "microsoft-master-id",
                    "need_sync_m": True,
                    "start": datetime.now() + timedelta(days=2),
                    "stop": datetime.now() + timedelta(days=2, hours=1),
                }
            )
        )
