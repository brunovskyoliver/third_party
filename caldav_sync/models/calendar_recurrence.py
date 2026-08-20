from odoo import models, fields, api
import uuid
import logging
from .calendar_event import (
    _log_microsoft_request,
    _microsoft_delete_block_reason,
    _microsoft_sync_is_blocked,
)

_logger = logging.getLogger(__name__)


class RecurrenceRule(models.Model):
    _inherit = "calendar.recurrence"

    caldav_uid = fields.Char(
        readonly=True,
        copy=False,
    )

    _caldav_uid_unique = models.Constraint(
        'UNIQUE (caldav_uid)',
        "The caldav_uid must be unique.",
    )

    def _microsoft_insert(self, values, timeout=None):
        if _microsoft_sync_is_blocked(self):
            _log_microsoft_request(self, "insert", blocked=True)
            return False
        _log_microsoft_request(self, "insert")
        if timeout is None:
            return super()._microsoft_insert(values)
        return super()._microsoft_insert(values, timeout=timeout)

    def _microsoft_patch(self, user_id, event_id, values, timeout=None):
        if _microsoft_sync_is_blocked(self):
            _log_microsoft_request(self, "patch", event_id, blocked=True)
            return False
        _log_microsoft_request(self, "patch", event_id)
        if timeout is None:
            return super()._microsoft_patch(user_id, event_id, values)
        return super()._microsoft_patch(
            user_id, event_id, values, timeout=timeout
        )

    def _microsoft_delete(self, user_id, event_id, timeout=None):
        reason = _microsoft_delete_block_reason(self)
        if reason:
            _log_microsoft_request(
                self, "delete", event_id, blocked=True, reason=reason
            )
            return False
        _log_microsoft_request(self, "delete", event_id)
        if timeout is None:
            return super()._microsoft_delete(user_id, event_id)
        return super()._microsoft_delete(user_id, event_id, timeout=timeout)

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get("no_microsoft_sync"):
            vals_list = [dict(vals, need_sync_m=False) for vals in vals_list]
        if not self._context.get("caldav_keep_ids"):
            for vals in vals_list:
                vals["caldav_uid"] = str(uuid.uuid4())
        else:
            for vals in vals_list:
                base_event = self.env["calendar.event"].browse(vals["base_event_id"])
                vals.update(caldav_uid=base_event.caldav_uid)
        return super().create(vals_list)

    @api.model
    def _detach_events(self, events):
        """When events are detached from a recurrence, their CalDAV UID and recurrence-id
        are no longer going to be valid, so we remove them from the server. They may then
        be re-written to the server with their new IDs later, but we don't care about
        that here."""
        detached_events = super()._detach_events(events)
        for event in detached_events:
            event._sync_unlink_to_caldav()
        return detached_events
