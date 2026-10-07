from datetime import datetime, UTC
from unittest.mock import MagicMock, patch

import caldav
import icalendar
from caldav.elements import dav
from psycopg2.errors import SerializationFailure
from requests.exceptions import ReadTimeout

from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon


@tagged("post_install", "-at_install")
class TestCalDAVPollPerformance(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch("caldav.DAVClient"):
            cls.user = cls._generate_user(
                "caldav-performance", "synthetic", "synthetic", "https://calendar.example.test/"
            )

    def setUp(self):
        super().setUp()
        self.events = self.env["calendar.event"]
        self.calendar = MagicMock()
        self.calendar.get_properties.return_value = {}
        self.client = MagicMock()
        self.client.calendar.return_value = self.calendar
        self.client_patch = patch.object(type(self.user), "_get_caldav_client", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def _resource(self, uid, title="Synthetic event", attendees=()):
        component = icalendar.Event()
        component.add("uid", uid)
        component.add("summary", title)
        component.add("dtstart", datetime(2030, 1, 15, 9, tzinfo=UTC))
        component.add("dtend", datetime(2030, 1, 15, 10, tzinfo=UTC))
        component.add("dtstamp", datetime(2030, 1, 1, tzinfo=UTC))
        for email in attendees:
            component.add("attendee", "mailto:" + email)
        calendar = icalendar.Calendar()
        calendar.add_component(component)
        return caldav.Event(data=calendar.to_ical(), url="https://calendar.example.test/%s.ics" % uid)

    def _poll(self, resources):
        self.calendar.events.return_value = resources
        self.events._poll_user_caldav_server(self.user)

    def test_unchanged_resources_do_not_repeat_imports(self):
        resources = [self._resource("perf-%s" % i) for i in range(30)]
        self._poll(resources)
        with patch.object(type(self.events), "_sync_event_from_ical", wraps=self.events._sync_event_from_ical) as importer:
            queries_before = self.env.cr.sql_log_count
            self._poll(resources)
            self.assertLess(self.env.cr.sql_log_count - queries_before, 25)
        importer.assert_not_called()
        self.assertEqual(self.events.search_count([("caldav_uid", "in", ["perf-%s" % i for i in range(30)])]), 30)

    def test_only_changed_resource_is_reimported(self):
        resources = [self._resource("changed-%s" % i) for i in range(8)]
        self._poll(resources)
        resources[0] = self._resource("changed-0", "Changed remotely")
        with patch.object(type(self.events), "_sync_event_from_ical", wraps=self.events._sync_event_from_ical) as importer:
            self._poll(resources)
        self.assertEqual(importer.call_count, 1)
        self.assertEqual(self.events.search([("caldav_uid", "=", "changed-0")]).name, "Changed remotely")

    def test_collection_token_skips_unchanged_snapshot(self):
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-1"}
        resources = [self._resource("token-event")]
        self._poll(resources)
        self.calendar.events.reset_mock()
        self._poll(resources)
        self.calendar.events.assert_not_called()

    def test_local_change_invalidates_collection_cache(self):
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-1"}
        resources = [self._resource("local-change")]
        self._poll(resources)
        event = self.events.search([("caldav_uid", "=", "local-change")])
        event.with_context(caldav_no_sync=True, no_microsoft_sync=True).write({"name": "Edited locally"})
        self.calendar.events.reset_mock()
        self._poll(resources)
        self.calendar.events.assert_called_once()

    def test_collection_change_invalidates_snapshot(self):
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-1"}
        self._poll([self._resource("first-token-event")])
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-2"}
        self._poll([self._resource("first-token-event"), self._resource("new-token-event")])
        self.assertTrue(self.events.search([("caldav_uid", "=", "new-token-event")]))

    def test_deleted_local_record_is_not_returned_from_cache(self):
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-1"}
        resources = [self._resource("recreate-local")]
        self._poll(resources)
        self.events.search([("caldav_uid", "=", "recreate-local")]).with_context(
            caldav_no_sync=True, no_microsoft_sync=True,
        ).unlink()
        self._poll(resources)
        self.assertTrue(self.events.search([("caldav_uid", "=", "recreate-local")]))

    def test_partial_snapshot_keeps_orphans_and_caches_successes(self):
        resources = [self._resource("valid-resource"), self._resource("broken-resource")]
        self._poll(resources)
        orphan = self.events.search([("caldav_uid", "=", "broken-resource")])
        original = self.events._sync_event_from_ical

        def import_resource(component, user):
            uid = str(component.walk("VEVENT")[0]["uid"])
            if uid == "broken-resource":
                raise ValueError("Synthetic invalid event")
            return original(component, user)

        resources[1] = self._resource("broken-resource", "Invalid update")
        with patch.object(type(self.events), "_sync_event_from_ical", side_effect=import_resource) as importer:
            with self.assertLogs("odoo.addons.caldav_sync.models.calendar_event", level="ERROR"):
                self._poll(resources)
            self.assertEqual(importer.call_count, 1)
            self.assertTrue(orphan.exists())
            importer.reset_mock()
            with self.assertLogs("odoo.addons.caldav_sync.models.calendar_event", level="ERROR"):
                self._poll(resources)
            self.assertEqual(importer.call_count, 1)
            self.assertTrue(orphan.exists())

    def test_retry_signal_is_not_swallowed(self):
        with patch.object(type(self.events), "_sync_event_from_ical", side_effect=SerializationFailure("synthetic retry")):
            with self.assertRaises(SerializationFailure):
                self._poll([self._resource("retry-resource")])

    def test_complete_snapshot_removes_only_missing_ordinary_event(self):
        resources = [self._resource("kept"), self._resource("removed")]
        self._poll(resources)
        self._poll(resources[:1])
        self.assertTrue(self.events.search([("caldav_uid", "=", "kept")]))
        self.assertFalse(self.events.search([("caldav_uid", "=", "removed")]))

    def test_preserved_source_event_is_not_deleted(self):
        self._poll([self._resource("source-owned")])
        event = self.events.search([("caldav_uid", "=", "source-owned")])
        with patch.object(type(self.events), "_caldav_preserves_local_event", return_value=True):
            self._poll([])
        self.assertTrue(event.exists())

    def test_cache_identity_changes_with_account(self):
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-1"}
        resources = [self._resource("account-event")]
        self._poll(resources)
        self.user.write({"caldav_username": "another-synthetic-account"})
        self.calendar.events.reset_mock()
        self._poll(resources)
        self.calendar.events.assert_called_once()

    def test_concurrent_poll_does_not_fetch_remote_calendar(self):
        with self.env.registry.cursor() as other_cursor:
            other_cursor.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s), %s)",
                ("caldav_sync.poll", self.user.id),
            )
            self.assertTrue(other_cursor.fetchone()[0])
            self._poll([self._resource("concurrent-resource")])
            self.client.calendar.assert_not_called()

    def test_connection_failure_does_not_prevent_other_users(self):
        with patch("caldav.DAVClient"):
            other_user = self._generate_user(
                "caldav-other", "synthetic", "synthetic", "https://other.example.test/"
            )
        called = []

        def poll_user(user):
            called.append(user.id)
            if user == self.user:
                raise ReadTimeout("synthetic timeout")

        with patch.object(type(self.events), "_poll_user_caldav_server", side_effect=poll_user):
            with self.assertLogs("odoo.addons.caldav_sync.models.calendar_event", level="WARNING"):
                self.events.poll_caldav_server()
        self.assertIn(other_user.id, called)
        self.assertIn(self.user.id, called)

    def test_invalid_cache_rebuilds_from_remote_snapshot(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "caldav_sync.poll_cache.%s" % self.user.id, "not-json",
        )
        self._poll([self._resource("rebuild-cache")])
        self.assertTrue(self.events.search([("caldav_uid", "=", "rebuild-cache")]))

    def test_new_known_contact_invalidates_unchanged_collection(self):
        self.calendar.get_properties.return_value = {dav.SyncToken.tag: "token-1"}
        resources = [self._resource("new-contact", attendees=["new-invitee@example.test"])]
        self._poll(resources)
        partner = self.env["res.partner"].create({
            "name": "Synthetic newly known invitee", "email": "new-invitee@example.test",
        })
        self.calendar.events.reset_mock()
        self._poll(resources)
        self.calendar.events.assert_called_once()
        event = self.events.search([("caldav_uid", "=", "new-contact")])
        self.assertIn(partner, event.partner_ids)

    def test_known_contact_email_change_invalidates_unchanged_resource(self):
        partner = self.env["res.partner"].create({
            "name": "Synthetic invitee", "email": "old-invitee@example.test",
        })
        resources = [self._resource("changed-contact", attendees=[partner.email])]
        self._poll(resources)
        event = self.events.search([("caldav_uid", "=", "changed-contact")])
        self.assertIn(partner, event.partner_ids)
        partner.write({"email": "changed-invitee@example.test"})
        self._poll(resources)
        self.assertNotIn(partner, event.partner_ids)
