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
from odoo import http
from odoo.http import request

SWITCH_GROUP = 'login_as_any_user.group_switch_users'


class UserSwitch(http.Controller):
    """This is a controller to switch user and switch back to admin
        user_switch:
            this function is to check weather the user is admin or not
        switch_admin:
            function to switch back to admin
    """

    @http.route('/switch/user', type='jsonrpc', auth='public')
    def user_switch(self):
        """
            Summary:
                function to check weather the user is admin
            Return:
                weather the current user is admin or not
        """
        user = request.env.user
        previous_uid = request.session.get('previous_user')
        if previous_uid and previous_uid != user.id:
            # Inside a switched session the button switches back.
            return False
        return user._is_admin() or user.has_group(SWITCH_GROUP)

    @http.route('/switch/admin', type='jsonrpc', auth='public')
    def switch_admin(self):
        session = request.session
        pre_uid = session.get('previous_user')
        pre_user = request.env['res.users'].browse(pre_uid)

        current_user = request.env.user.sudo()
        if current_user.has_group("base.group_system"):
            request.session['previous_user'] = request.env.user.id

        request.update_env(user=request.env.user)

        if pre_user.exists() and (pre_user._is_admin() or pre_user.has_group(SWITCH_GROUP)):
            session.pop('previous_user', None)
            session.authenticate_without_password(
                request.env.cr.dbname,
                pre_user.login,
                request.env,
            )
            request.update_env(user=request.env.user)
            return {
                'type': 'ir.actions.act_url',
                'url': '/',
                'target': 'self'
            }
        return True

