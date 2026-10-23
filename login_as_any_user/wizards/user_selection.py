# -*- coding: utf-8 -*-
###############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2024-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author: Cybrosys Techno Solutions(odoo@cybrosys.com)
#
#    You can modify it under the terms of the GNU LESSER
#    GENERAL PUBLIC LICENSE (LGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU LESSER GENERAL PUBLIC LICENSE (LGPL v3) for more details.
#
#    You should have received a copy of the GNU LESSER GENERAL PUBLIC LICENSE
#    (LGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.http import request

SWITCH_GROUP = 'login_as_any_user.group_switch_users'


class UserSelection(models.Model):
    """
        class for a wizard for users selection
        _onchange_user_id:
            function to get corresponding user group
        action_switch:
            function for switching the user
    """
    _name = 'user.selection'
    _description = 'User Selection'

    user_id = fields.Many2one('res.users', string="User",
                              required=True,
                              help="Select the user here",
                              domain=lambda self: self._target_user_domain())
    access_ids = fields.One2many('res.groups',
                                 'user_id',
                                 help="User groups for the selection",
                                 string="Group", readonly=True)

    @api.model
    def _target_user_domain(self):
        domain = [('id', '!=', self.env.user.id)]
        if not self.env.user._is_admin():
            # Switchers without user-management rights must not become an administrator.
            admins = self.env['res.users'].sudo().search([('share', '=', False)]).filtered(
                lambda user: user._is_superuser() or user._is_admin())
            domain.append(('id', 'not in', admins.ids))
        return domain

    def _check_switch_allowed(self):
        actor = self.env.user
        if not (actor._is_admin() or actor.has_group(SWITCH_GROUP)):
            raise AccessError(_("Na prepínanie používateľov nemáte oprávnenie."))
        target = self.user_id.sudo()
        if not actor._is_admin() and (target._is_superuser() or target._is_admin()):
            raise AccessError(_("Ako správca systému sa môže prihlásiť iba správca systému."))

    @api.onchange('user_id')
    def _onchange_user_id(self):
        """
            Summary:
                change function to get users access group
        """
        self.access_ids = self.user_id.group_ids

    def action_switch(self):
        """
        Summary:
            function for switching the user
        Return:
            Main login page after logged in
        """
        self.ensure_one()
        self._check_switch_allowed()
        session = request.session
        session.update({
            'previous_user': self.env.user.id,
        })
        session.authenticate_without_password(self.env.cr.dbname,
                                              self.user_id.login, self.env)
        return {
            'type': 'ir.actions.act_url',
            'url': '/',
            'target': 'self'
        }
