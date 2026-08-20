from odoo import fields, models


class CaldavCalendarOption(models.Model):
    _name = "caldav.calendar.option"
    _description = "CalDAV Calendar Option"
    _order = "name, id"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade"
    )
    name = fields.Char(required=True)
    url = fields.Char(required=True)
