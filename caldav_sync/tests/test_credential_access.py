from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.service.model import call_kw
from odoo.tests import HttpCase, JsonRpcException, TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestCalDAVCredentialAccess(TransactionCase):
    def setUp(self):
        super().setUp()
        self.client_patch = patch("caldav.DAVClient")
        self.client = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.client.return_value.principal.return_value.calendars.return_value = []
        # Permit generic export so denials exercise the credential boundary.
        self.owner = new_test_user(self.env, login="caldav.boundary.owner",
                                   groups="base.group_user,base.group_allow_export")
        self.other = new_test_user(self.env, login="caldav.boundary.other",
                                   groups="base.group_user,base.group_allow_export")
        self.portal = new_test_user(self.env, login="caldav.boundary.portal",
                                    groups="base.group_portal,base.group_allow_export")
        self.admin = new_test_user(self.env, login="caldav.boundary.admin", groups="base.group_system")
        self.owner.sudo().write({
            "caldav_calendar_url": "https://calendar.example.invalid/",
            "caldav_username": "invented-calendar-account",
            "caldav_password": "invented-existing-secret",
        })

    def test_stored_password_is_not_readable_exportable_or_searchable(self):
        for actor in (self.owner, self.other, self.portal, self.admin):
            target = self.owner.with_user(actor)
            for method, args in (
                ("read", [target.ids, ["caldav_password"]]),
                ("export_data", [target.ids, ["caldav_password"]]),
                ("search_count", [[("caldav_password", "=", "invented-existing-secret")]]),
            ):
                with self.subTest(actor=actor.id, operation=method):
                    with self.assertRaises(AccessError):
                        call_kw(target, method, args, {})

    def test_owner_can_replace_password_without_reading_it_back(self):
        owner = self.owner.with_user(self.owner)
        call_kw(owner, "write", [owner.ids, {"caldav_new_password": "invented-replacement"}], {})
        self.assertEqual(self.owner.sudo().caldav_password, "invented-replacement")
        self.assertEqual(owner.read(["caldav_new_password", "caldav_clear_password"]), [{
            "id": owner.id, "caldav_new_password": False, "caldav_clear_password": False,
        }])
        self.assertNotIn("caldav_password", owner.SELF_READABLE_FIELDS)
        self.assertNotIn("caldav_password", owner.SELF_WRITEABLE_FIELDS)
        owner.write({"caldav_new_password": False})
        self.assertEqual(self.owner.sudo().caldav_password, "invented-replacement")
        self.client.reset_mock()
        owner._get_caldav_client()
        self.assertEqual(self.client.call_args.kwargs["password"], "invented-replacement")

    def test_cross_user_and_portal_password_writes_are_denied(self):
        for actor, record in ((self.other, self.owner), (self.portal, self.portal)):
            for field, value in (("caldav_new_password", "invented-forgery"), ("caldav_clear_password", True)):
                with self.subTest(actor=actor.id, field=field):
                    with self.assertRaises(AccessError):
                        call_kw(record.with_user(actor), "write", [record.ids, {field: value}], {})
        self.assertEqual(self.owner.sudo().caldav_password, "invented-existing-secret")

    def test_upstream_sudo_does_not_authorize_a_portal_or_adjacent_actor(self):
        for actor, record in ((self.other, self.owner), (self.portal, self.portal)):
            with self.subTest(actor=actor.id):
                with self.assertRaises(AccessError):
                    record.with_user(actor).sudo().write({"caldav_new_password": "invented-forgery"})

    def test_owner_can_clear_and_admin_can_replace_but_copy_has_no_secret(self):
        self.owner.with_user(self.owner).write({"caldav_clear_password": True})
        self.assertFalse(self.owner.sudo().caldav_password)
        self.owner.with_user(self.admin).write({"caldav_new_password": "invented-admin-replacement"})
        duplicate = self.owner.sudo().copy({"login": "caldav.boundary.copy"})
        self.assertFalse(duplicate.caldav_password)
        self.assertFalse(duplicate.caldav_calendar_option_id)

    def test_preferences_view_exposes_only_replacement_inputs(self):
        for xmlid in ("caldav_sync.view_users_form_inherit", "caldav_sync.view_users_form_simple_modif_inherit"):
            arch = self.env.ref(xmlid).arch_db
            self.assertIn('name="caldav_new_password"', arch)
            self.assertNotIn('name="caldav_password"', arch)

    def test_replacement_and_url_change_discover_calendars_once(self):
        self.client.reset_mock()
        self.owner.with_user(self.owner).write({
            "caldav_calendar_url": "https://calendar.example.invalid/replacement/",
            "caldav_new_password": "invented-replacement",
        })
        self.assertEqual(self.client.return_value.principal.return_value.calendars.call_count, 1)

    def test_replace_and_clear_together_are_rejected(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.owner.with_user(self.owner).write({
                "caldav_new_password": "invented-conflicting-replacement", "caldav_clear_password": True,
            })
        self.assertEqual(self.owner.sudo().caldav_password, "invented-existing-secret")


@tagged("post_install", "-at_install")
class TestCalDAVCredentialHTTPAccess(HttpCase):
    def test_credential_read_denied_over_http(self):
        owner = new_test_user(self.env, login="caldav.http.owner", password="invented-http-password",
                              groups="base.group_user")
        self.authenticate("caldav.http.owner", "invented-http-password")
        with self.assertRaises(JsonRpcException) as error:
            self.make_jsonrpc_request("/web/dataset/call_kw/res.users/read", {
                "model": "res.users", "method": "read", "args": [owner.ids, ["caldav_password"]], "kwargs": {},
            })
        self.assertIn("Access", str(error.exception))
