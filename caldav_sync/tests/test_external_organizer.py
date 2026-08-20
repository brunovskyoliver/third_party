from odoo.tests import TransactionCase
from unittest.mock import patch, MagicMock
from .common import CaldavTestCommon
from .test_calendar import _get_ics_path, _patch_caldav_with_events_from_ics


class TestExternalOrganizer(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls._generate_user(
            "test",
            caldav_username="test",
            caldav_password="test",
            caldav_url="https://example.com/calendar",
        )

    def test_external_organizer_event_sync(self):
        """Unknown external organizers must not become Odoo contacts."""
        organizer_email = "external.person@otherdomain.com"
        self.assertFalse(
            self.env["res.partner"].search([("email", "=", organizer_email)])
        )
        # Setup mock for CalDAV client with our test ICS file
        with _patch_caldav_with_events_from_ics(
            [_get_ics_path("test_external_organizer.ics")], self.user
        ):
            # Ensure caldav is enabled
            self.user._compute_is_caldav_enabled()
            # Sync events from the mock server
            self.env["calendar.event"].poll_caldav_server()

            # Find the synced event
            event = self.env["calendar.event"].search(
                [("caldav_uid", "=", "external-organizer-test-123")]
            )

            # Verify event was created
            self.assertTrue(event, "Event should be created")

            # An unknown organizer falls back to the syncing user; it must
            # not cause an external partner record to be created.
            self.assertEqual(
                event.user_id,
                self.user,
                "Unknown organizer should fall back to the syncing user",
            )

            self.assertFalse(
                self.env["res.partner"].search([("email", "=", organizer_email)]),
                "CalDAV must not create a partner for an unknown organizer",
            )

            # Unknown organizers are not represented by a calendar attendee.
            external_attendee = event.attendee_ids.filtered(
                lambda a: a.email == organizer_email
            )
            self.assertFalse(
                external_attendee,
                "Unknown organizer must not be materialized as an attendee",
            )

    def test_existing_external_organizer_is_linked_without_duplication(self):
        organizer_email = "external.person@otherdomain.com"
        organizer = self.env["res.partner"].create(
            {"name": "Existing external organizer", "email": organizer_email}
        )

        with _patch_caldav_with_events_from_ics(
            [_get_ics_path("test_external_organizer.ics")], self.user
        ):
            self.env["calendar.event"].poll_caldav_server()

        event = self.env["calendar.event"].search(
            [("caldav_uid", "=", "external-organizer-test-123")]
        )
        self.assertEqual(
            self.env["res.partner"].search_count([("email", "=", organizer_email)]),
            1,
        )
        self.assertIn(organizer, event.attendee_ids.partner_id)

    def test_no_organizer_external_event_sync(self):
        """Test that events without organizers are assigned to the calendar's user."""
        # Setup mock for CalDAV client with our test ICS file
        with _patch_caldav_with_events_from_ics(
            [_get_ics_path("test_external_no_organizer.ics")], self.user
        ):
            self.user._compute_is_caldav_enabled()
            # Make sure that the current user is not the admin user
            self.assertNotEqual(self.user.id, self.env.ref("base.user_admin").id)
            self.env["calendar.event"].poll_caldav_server()

            # Find the synced event
            event = self.env["calendar.event"].search(
                [("caldav_uid", "=", "external-organizer-test-123")]
            )

            # Verify event was created
            self.assertTrue(event, "Event should be created")

            # Verify user_id is set to the calendar's user
            self.assertEqual(
                event.user_id,
                self.user,
                "Event without organizer should have user_id set to calendar's user",
            )
