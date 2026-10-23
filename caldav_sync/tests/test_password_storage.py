from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from .common import CaldavTestCommon

SECRET = "synthetic-caldav-secret"


@tagged("post_install", "-at_install")
class TestCaldavPasswordStorage(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.owner = cls._generate_user("caldav-owner")
        cls.colleague = cls._generate_user("caldav-colleague")
        cls.admin = cls._generate_user("caldav-admin")
        cls.admin.group_ids = [Command.link(cls.env.ref("base.group_system").id)]
        export_group = cls.env.ref("base.group_allow_export")
        (cls.colleague | cls.admin).group_ids = [Command.link(export_group.id)]

    def _stored_password(self, user):
        self.env.cr.execute(
            "SELECT caldav_password FROM res_users WHERE id = %s", (user.id,)
        )
        return self.env.cr.fetchone()[0]

    @patch("caldav.DAVClient")
    def _write_as_owner(self, values, mock_dav_client):
        self.owner.with_user(self.owner).write(values)
        return mock_dav_client

    def test_owner_sets_password_from_preferences_without_reading_it_back(self):
        self._write_as_owner({
            "caldav_calendar_url": "https://caldav.example.com/",
            "caldav_username": "owner",
            "caldav_password": SECRET,
        })

        self.assertEqual(self._stored_password(self.owner), SECRET)
        own = self.owner.with_user(self.owner)
        self.assertFalse(own.read(["caldav_password"])[0]["caldav_password"])
        self.assertEqual(own.caldav_username, "owner")

    def test_other_users_and_admins_cannot_read_export_or_search_it(self):
        self._write_as_owner({"caldav_password": SECRET})

        for actor in (self.colleague, self.admin):
            record = self.owner.with_user(actor)
            self.assertFalse(record.read(["caldav_password"])[0]["caldav_password"])
            exported = record.export_data(["caldav_password"])["datas"]
            self.assertNotIn(SECRET, repr(exported))
            searched = record.search_read(
                [("id", "=", self.owner.id)], ["caldav_password"]
            )
            self.assertNotIn(SECRET, repr(searched))
        with self.assertRaises(ValueError):  # non-searchable field
            self.env["res.users"].search([("caldav_password", "=", SECRET)])

    def test_other_users_and_admins_cannot_set_it(self):
        self._write_as_owner({"caldav_password": SECRET})

        with self.assertRaises(AccessError):
            self.owner.with_user(self.colleague).write({"caldav_password": "x"})
        with self.assertRaises(AccessError):
            self.owner.with_user(self.admin).write({"caldav_password": "x"})
        self.assertEqual(self._stored_password(self.owner), SECRET)

        # An administrator can only remove a credential, e.g. at offboarding.
        self.owner.with_user(self.admin).write({"caldav_password": False})
        self.assertIsNone(self._stored_password(self.owner))

    def test_copy_does_not_duplicate_the_password(self):
        self._write_as_owner({"caldav_password": SECRET})

        duplicate = self.owner.copy({"login": "caldav-owner-copy"})

        self.assertIsNone(self._stored_password(duplicate))

    @patch("caldav.DAVClient")
    def test_sync_authenticates_with_stored_and_legacy_values(self, mock_dav_client):
        # Rows written by the previous stored field keep their value in place.
        self.owner.write({
            "caldav_calendar_url": "https://caldav.example.com/",
            "caldav_username": "owner",
        })
        self.env.cr.execute(
            "UPDATE res_users SET caldav_password = %s WHERE id = %s",
            ("legacy-" + SECRET, self.owner.id),
        )
        self.owner.invalidate_recordset()

        self.owner._compute_is_caldav_enabled()
        self.assertTrue(self.owner.is_caldav_enabled)
        self.assertEqual(
            mock_dav_client.call_args.kwargs["password"], "legacy-" + SECRET
        )

        self.owner.with_user(self.owner).write({"caldav_password": SECRET})
        self.assertTrue(self.owner.is_caldav_enabled)
        self.owner._get_caldav_client()
        self.assertEqual(mock_dav_client.call_args.kwargs["password"], SECRET)
        self.assertEqual(mock_dav_client.call_args.kwargs["username"], "owner")

    @patch("caldav.DAVClient")
    def test_owner_change_triggers_revalidation(self, mock_dav_client):
        self.owner.write({
            "caldav_calendar_url": "https://caldav.example.com/",
            "caldav_username": "owner",
        })
        self.assertFalse(self.owner.is_caldav_enabled)

        self.owner.with_user(self.owner).write({"caldav_password": SECRET})

        self.assertTrue(self.owner.is_caldav_enabled)
        mock_dav_client.return_value.principal.assert_called()
