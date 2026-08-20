from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.microsoft_calendar.utils.microsoft_calendar import (
    MicrosoftCalendarService,
)
from odoo.addons.microsoft_calendar.utils.microsoft_event import MicrosoftEvent

from .common import CaldavTestCommon


@tagged("post_install", "-at_install")
class TestMicrosoftImportOwnership(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sync_user = cls._generate_user("microsoft-sync-user")
        cls.sync_user.microsoft_calendar_token = "test-token"
        cls.sync_user.microsoft_calendar_token_validity = fields.Datetime.now() + timedelta(
            hours=1
        )
        cls.sync_user.microsoft_synchronization_stopped = False
        cls.external_organizer = cls._generate_user("external-organizer")

    def _outlook_event(self, *, event_id, organizer_email, is_organizer):
        return {
            "type": "singleInstance",
            "seriesMasterId": None,
            "id": event_id,
            "iCalUId": f"{event_id}-ical",
            "subject": "Brusel - guide",
            "body": {"content": "", "contentType": "text"},
            "start": {
                "dateTime": "2026-09-01T06:00:00.0000000",
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": "2026-09-01T20:00:00.0000000",
                "timeZone": "UTC",
            },
            "attendees": [],
            "isAllDay": False,
            "isCancelled": False,
            "sensitivity": "normal",
            "showAs": "busy",
            "isOnlineMeeting": False,
            "onlineMeetingUrl": None,
            "isOrganizer": is_organizer,
            "isReminderOn": False,
            "location": {"displayName": "Brusel"},
            "reminderMinutesBeforeStart": 0,
            "organizer": {
                "emailAddress": {
                    "address": organizer_email,
                    "name": organizer_email,
                }
            },
        }

    def _sync_event(self, event):
        with patch.object(
            MicrosoftCalendarService,
            "get_events",
            return_value=(MicrosoftEvent([event]), None),
        ):
            self.sync_user.sudo()._sync_microsoft_calendar()
        return self.env["calendar.event"].search(
            [("microsoft_id", "=", event["id"])]
        )

    def test_imported_outlook_organizer_event_belongs_to_sync_user(self):
        event = self._sync_event(
            self._outlook_event(
                event_id="brusel-guide-owner",
                organizer_email=self.sync_user.email,
                is_organizer=True,
            )
        )

        self.assertEqual(event.user_id, self.sync_user)
        self.assertNotEqual(event.user_id, self.env.ref("base.user_root"))
        self.assertIn(self.sync_user.partner_id, event.partner_ids)
        self.assertIn(self.sync_user.partner_id, event.attendee_ids.partner_id)
        self.assertTrue(
            self.env["calendar.event"]
            .with_user(self.sync_user)
            .search(
                [
                    ("id", "=", event.id),
                    ("partner_ids.user_ids", "in", [self.sync_user.id]),
                ]
            )
        )

    def test_imported_external_organizer_event_keeps_external_owner(self):
        event = self._sync_event(
            self._outlook_event(
                event_id="brusel-guide-external",
                organizer_email=self.external_organizer.email,
                is_organizer=False,
            )
        )

        self.assertEqual(event.user_id, self.external_organizer)
        self.assertNotEqual(event.user_id, self.sync_user)
        self.assertIn(self.sync_user.partner_id, event.partner_ids)
        self.assertIn(self.sync_user.partner_id, event.attendee_ids.partner_id)
        self.assertTrue(
            self.env["calendar.event"]
            .with_user(self.sync_user)
            .search(
                [
                    ("id", "=", event.id),
                    ("partner_ids.user_ids", "in", [self.sync_user.id]),
                ]
            )
        )

    def test_import_only_mode_blocks_all_odoo_to_microsoft_requests(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "caldav_sync.microsoft_sync_direction", "import_only"
        )
        event = self.env["calendar.event"].with_user(self.sync_user).create(
            {
                "name": "Import-only event",
                "user_id": self.sync_user.id,
                "start": fields.Datetime.now() + timedelta(days=1),
                "stop": fields.Datetime.now() + timedelta(days=1, hours=1),
                "microsoft_id": "existing-microsoft-id",
            }
        )

        with patch.object(MicrosoftCalendarService, "insert") as insert, patch.object(
            MicrosoftCalendarService, "patch"
        ) as patch_event, patch.object(MicrosoftCalendarService, "delete") as delete:
            event._sync_odoo2microsoft()
            event.write({"name": "Changed locally"})
            event._microsoft_delete(self.sync_user, event.microsoft_id)

        self.assertFalse(event.need_sync_m)
        insert.assert_not_called()
        patch_event.assert_not_called()
        delete.assert_not_called()
