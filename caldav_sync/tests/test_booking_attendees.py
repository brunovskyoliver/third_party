from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import Command
from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon


@tagged("post_install", "-at_install")
class TestBookingAttendees(TransactionCase, CaldavTestCommon):
    """A booking's client stays an Odoo attendee but is not sent to CalDAV."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.worker = cls._generate_user(
                "synthetic_caldav_worker",
                caldav_username="synthetic",
                caldav_password="synthetic",
                caldav_url="https://calendar.example.test/calendar",
            )
            cls.colleague = cls._generate_user("synthetic_caldav_colleague")
        cls.client = cls.env["res.partner"].create({
            "name": "Synthetic Booking Client",
            "email": "synthetic.booking.client@example.invalid",
        })

    def setUp(self):
        super().setUp()
        if "appointment_type_id" not in self.env["calendar.event"]._fields:
            self.skipTest("appointment is not installed")

    def _event(self, booking=True, partners=None, extra=None):
        partners = partners or (self.worker.partner_id | self.client)
        values = {
            "name": "Synthetic consultation",
            "user_id": self.worker.id,
            "partner_ids": [Command.set(partners.ids)],
            "start": datetime.now() + timedelta(days=2),
            "stop": datetime.now() + timedelta(days=2, hours=1),
        }
        if booking:
            # Resource mode: no worker-hours fixture is needed for the booking to save.
            values["appointment_type_id"] = self.env["appointment.type"].create(
                {"name": "Synthetic CalDAV service", "schedule_based_on": "resources"}
            ).id
        values.update(extra or {})
        return self.env["calendar.event"].with_context(caldav_no_sync=True).create(values)

    @staticmethod
    def _attendee_text(event):
        return " ".join(
            "%s %s" % (attendee, attendee.params.get("cn", ""))
            for attendee in event._create_event_data()["attendee"]
        )

    def test_booking_client_is_left_out_of_caldav_attendees(self):
        event = self._event(partners=self.worker.partner_id | self.colleague.partner_id | self.client)
        self.assertIn(self.client, event.partner_ids, "the client stays an Odoo attendee")
        text = self._attendee_text(event)
        self.assertNotIn(self.client.email, text)
        self.assertNotIn(self.client.name, text)
        self.assertIn(self.colleague.email, text, "an internal colleague is still sent")

    def test_plain_meeting_keeps_external_attendees(self):
        event = self._event(booking=False)
        self.assertIn(self.client.email, self._attendee_text(event))

    def test_import_does_not_remove_the_left_out_client(self):
        event = self._event()
        # The server returns the event without the client it was never sent.
        returned = {"partner_ids": [(6, 0, self.worker.partner_id.ids)]}
        self.assertNotIn("partner_ids", event._get_value_changes(returned))
        added = {"partner_ids": [(6, 0, (self.worker.partner_id | self.colleague.partner_id).ids)]}
        changed = event._get_value_changes(added)
        self.assertEqual(
            set(changed["partner_ids"][0][2]),
            set((self.worker.partner_id | self.colleague.partner_id | self.client).ids),
            "a colleague added remotely is imported and the client is kept",
        )

    def test_booking_description_is_not_sent_to_caldav(self):
        note = "<p>Synthetic reception note 0900 000 321</p>"
        event = self._event(extra={"description": note})
        data = event._create_event_data()
        self.assertNotIn("description", data)
        self.assertNotIn("0900 000 321", " ".join(str(value) for value in data.values()))
        self.assertEqual(str(data["summary"]), "Synthetic consultation", "the title is still sent")
        self.assertIn("0900 000 321", event.description, "the note stays on the Odoo event")

    def test_plain_meeting_keeps_its_description(self):
        event = self._event(booking=False, extra={"description": "<p>Synthetic agenda</p>"})
        self.assertIn("Synthetic agenda", str(event._create_event_data()["description"]))

    def test_import_does_not_clear_the_unsent_description(self):
        event = self._event(extra={"description": "<p>Synthetic reception note</p>"})
        # The server returns the booking with no description, or with one typed there.
        for returned in (False, "<p>Synthetic text typed in the other calendar</p>"):
            self.assertNotIn("description", event._get_value_changes({"description": returned}))
        plain = self._event(booking=False, extra={"description": "<p>Synthetic agenda</p>"})
        self.assertEqual(
            plain._get_value_changes({"description": "<p>Synthetic new agenda</p>"}),
            {"description": "<p>Synthetic new agenda</p>"},
            "a plain meeting still imports a changed description",
        )
