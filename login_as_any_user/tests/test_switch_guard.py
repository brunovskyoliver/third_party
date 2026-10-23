from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSwitchGuard(TransactionCase):
    """A switcher without user management must never become an administrator."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        base = cls.env.ref("base.group_user")
        cls.switcher = Users.create({
            "name": "Synthetic switcher", "login": "synthetic-switcher",
            "group_ids": [Command.set([base.id, cls.env.ref("login_as_any_user.group_switch_users").id])],
        })
        cls.colleague = Users.create({
            "name": "Synthetic colleague", "login": "synthetic-colleague",
            "group_ids": [Command.set([base.id])],
        })
        cls.settings_admin = Users.create({
            "name": "Synthetic settings admin", "login": "synthetic-settings-admin",
            "group_ids": [Command.set([base.id, cls.env.ref("base.group_system").id])],
        })
        cls.plain = Users.create({
            "name": "Synthetic plain", "login": "synthetic-plain",
            "group_ids": [Command.set([base.id])],
        })

    def _wizard(self, actor, target):
        return self.env["user.selection"].with_user(actor).new({"user_id": target.id})

    def test_switcher_may_target_ordinary_users_only(self):
        self._wizard(self.switcher, self.colleague)._check_switch_allowed()
        for target in (self.settings_admin, self.env.ref("base.user_admin")):
            with self.assertRaises(AccessError):
                self._wizard(self.switcher, target)._check_switch_allowed()

    def test_picker_hides_administrators_from_switcher(self):
        domain = self.env["user.selection"].with_user(self.switcher)._target_user_domain()
        targets = self.env["res.users"].search(domain)
        self.assertIn(self.colleague, targets)
        self.assertNotIn(self.settings_admin, targets)
        self.assertNotIn(self.switcher, targets)

    def test_settings_admin_keeps_full_switching(self):
        self._wizard(self.settings_admin, self.switcher)._check_switch_allowed()

    def test_user_without_switch_group_is_refused(self):
        with self.assertRaises(AccessError):
            self._wizard(self.plain, self.colleague)._check_switch_allowed()
