from time import monotonic
import uuid
import hashlib
import json

import icalendar.cal

from odoo import models, api, fields, _
from odoo.exceptions import AccessError, UserError
from odoo.addons.calendar.models.calendar_recurrence import MAX_RECURRENT_EVENT
import caldav
from caldav.lib.error import DAVError, NotFoundError
from caldav.elements import dav
from psycopg2.extensions import TransactionRollbackError
from requests.exceptions import RequestException
import logging
from datetime import date, datetime, time, timedelta
from icalendar import vCalAddress, vText, vDatetime, vRecur, Event, vDate
import re
from pytz import timezone, utc
from typing import List, Dict, Optional, Any, TYPE_CHECKING
from markdownify import markdownify as md
import markdown2 as md2

_logger = logging.getLogger(__name__)

CALDAV_SYNC_SOURCE = "caldav"
NO_MICROSOFT_SYNC_CONTEXT = "no_microsoft_sync"
MICROSOFT_DELETE_MODE_PARAM = "caldav_sync.microsoft_delete_mode"
MICROSOFT_DELETE_MODE_LOG_ONLY = "log_only"
MICROSOFT_SYNC_DIRECTION_PARAM = "caldav_sync.microsoft_sync_direction"
MICROSOFT_SYNC_DIRECTION_IMPORT_ONLY = "import_only"
# Keep v2: failed detached resources are not cached. No global forced reimport.
CALDAV_POLL_CACHE_VERSION = 2
CALDAV_IMPORT_CONTEXT = {
    "calendar_sync_source": CALDAV_SYNC_SOURCE,
    NO_MICROSOFT_SYNC_CONTEXT: True,
}


class CalDAVPollStopped(Exception):
    """A separately committed stop request aborts the current account."""


class CalDAVPartialSyncError(Exception):
    """An imported subset is useful, but must not be reported as a success."""


class CalDAVResourceReview(CalDAVPartialSyncError):
    """A supported refusal, reported once as WARNING rather than a code error."""

    def __init__(self, uid, event_ids, kind="recurrence"):
        super().__init__("CalDAV resource requires review")
        self.uid = uid
        self.event_ids = event_ids
        self.kind = kind


CalDAVRecurrenceReview = CalDAVResourceReview


if TYPE_CHECKING:
    from odoo.addons.base.models.res_users import Users as User
    from odoo.addons.base.models.res_partner import Partner
    from odoo.addons.calendar.models.calendar_event import Meeting as OdooCalendarEvent
else:
    User = models.Model
    Partner = models.Model
    OdooCalendarEvent = models.Model


def _microsoft_sync_is_blocked(recordset):
    return bool(
        recordset.env.context.get(NO_MICROSOFT_SYNC_CONTEXT)
        or recordset.env.context.get("calendar_sync_source") == CALDAV_SYNC_SOURCE
    )


def _microsoft_outbound_is_blocked(recordset):
    return (
        recordset.env.context.get("microsoft_import_only")
        or recordset.env["ir.config_parameter"].sudo().get_param(
            MICROSOFT_SYNC_DIRECTION_PARAM, "bidirectional"
        ).strip().lower()
        == MICROSOFT_SYNC_DIRECTION_IMPORT_ONLY
    )


def _microsoft_delete_block_reason(recordset):
    if _microsoft_sync_is_blocked(recordset):
        return "source guard"
    delete_mode = recordset.env["ir.config_parameter"].sudo().get_param(
        MICROSOFT_DELETE_MODE_PARAM, "enabled"
    )
    if delete_mode.strip().lower() == MICROSOFT_DELETE_MODE_LOG_ONLY:
        return "temporary log-only mode"
    return None


def _log_microsoft_request(
    recordset, action, event_id=None, blocked=False, reason=None
):
    message = (
        "Blocked Microsoft Calendar %s for %s IDs %s (Microsoft event ID %s)%s"
        if blocked
        else "Microsoft Calendar %s requested for %s IDs %s (Microsoft event ID %s)%s"
    )
    log_method = _logger.warning if blocked else _logger.info
    reason_suffix = " (%s)" % reason if reason else ""
    log_method(
        message,
        action,
        recordset._name,
        recordset.ids,
        event_id or "<new>",
        reason_suffix,
    )

WEEKDAY_MAP = {
    0: "MO",
    1: "TU",
    2: "WE",
    3: "TH",
    4: "FR",
    5: "SA",
    6: "SU",
}


def _parse_rrule_string(rrule_str: str) -> Dict[str, Any]:
    """Parse a string representing an RRULE into a dictionary of its parts.

    Takes a string like "RRULE:FREQ=WEEKLY;UNTIL=20221231T000000Z;BYDAY=MO"
    and returns a dictionary with proper types for vRecur.
    """
    from icalendar import vDDDTypes, vWeekday, vFrequency

    def parse_value(key: str, value: str) -> Any:
        if key == "UNTIL":
            # Convert to datetime and wrap in vDDDTypes
            if "T" in value:
                dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
            else:
                dt = datetime.strptime(value, "%Y%m%d")
            return vDDDTypes(dt)
        elif key in ("WKST", "BYDAY", "BYWEEKDAY"):
            # Convert to vWeekday
            return [vWeekday(day) for day in value.split(",")]
        elif key == "FREQ":
            # vFrequency will handle the conversion
            return vFrequency(value)
        elif key in (
            "COUNT",
            "INTERVAL",
            "BYSECOND",
            "BYMINUTE",
            "BYHOUR",
            "BYWEEKNO",
            "BYMONTHDAY",
            "BYYEARDAY",
            "BYMONTH",
            "BYSETPOS",
        ):
            # Convert to int or list of ints
            if "," in value:
                return [int(v) for v in value.split(",")]
            return int(value)
        return value

    if not rrule_str.startswith("RRULE:"):
        return {}

    params = rrule_str[6:]  # Remove 'RRULE:'
    result = {}
    for param in params.split(";"):
        if "=" in param:
            key, value = param.split("=", 1)
            key = key.upper()
            result[key] = parse_value(key, value)

    return result


def _is_yearly_ical_event(ical_event) -> bool:
    """Yearly series (birthdays, name days) are not synced into Odoo (owner, 2026-10-21)."""
    for component in ical_event.walk():
        if component.name != "VEVENT":
            continue
        rrule = component.get("rrule")
        freqs = rrule.get("FREQ", []) if rrule else []
        if isinstance(freqs, str):
            freqs = [freqs]
        if "YEARLY" in [str(freq).upper() for freq in freqs]:
            return True
    return False


def _extract_vcal_email(vcal_address):
    email_regex = re.compile(r"[a-z0-9.\-+_]+@[a-z0-9.\-+_]+\.[a-z]+")
    res = email_regex.search(str(vcal_address))
    return res.group(0).lower().strip() if res else ""


class CalendarEvent(models.Model):
    _inherit = "calendar.event"

    # CalDAV UID is either unique per event or the same for all events in a recurring
    # sequence. This field and caldav_recurrence_id are not calculated fields since
    # the recalculation timing is hard to control and we want to make sure we sync
    # some modifications before the UID changes with recurrence_id and other changes on
    # the record.
    caldav_uid = fields.Char(string="CalDAV UID", store=True, index=True)
    # Recurrence ID in iCalendar is the date or datetime the event would have
    # been at if it followed the sequence. It is set by calendar.recurrence
    # when applying a recurrence.
    caldav_recurrence_id = fields.Datetime(
        string="CalDAV Recurrence ID",
    )
    caldav_user_ids = fields.Many2many(
        comodel_name="res.users",
        compute="_compute_caldav_users",
    )
    is_base_event = fields.Boolean(compute="_compute_is_base_event")
    differs_from_base_event = fields.Boolean(compute="_compute_differs_from_base_event")

    ###################################
    #### Field Computation Methods ####
    ###################################

    def _recompute_caldav_ids(self):
        for event in self:
            if event.recurrence_id:
                event.caldav_uid = event.recurrence_id.caldav_uid
                time = event.recurrence_id.dtstart.time()
                date = event.start.date()
                event.caldav_recurrence_id = datetime(
                    date.year,
                    date.month,
                    date.day,
                    time.hour,
                    time.minute,
                    time.second,
                )
            else:
                if not self.env.context.get("caldav_keep_ids"):
                    event.caldav_uid = uuid.uuid4()
                event.caldav_recurrence_id = False

    @api.depends("name", "description", "partner_ids", "location", "videocall_location")
    def _compute_differs_from_base_event(self):
        base_events = self.filtered("is_base_event")
        non_base_events = self - base_events
        for event in base_events:
            event.differs_from_base_event = False
        fields_to_check = [
            "name",
            "description",
            "partner_ids",
            "location",
            "videocall_location",
        ]
        for event in non_base_events:
            base_event = event.recurrence_id.base_event_id
            event.differs_from_base_event = (
                any(
                    [
                        getattr(event, field) != getattr(base_event, field)
                        for field in fields_to_check
                    ]
                )
                or not event.follow_recurrence
            )

    @api.depends("recurrency", "recurrence_id", "recurrence_id.base_event_id")
    def _compute_is_base_event(self):
        for rec in self:
            rec.is_base_event = (
                not rec.recurrence_id or rec.recurrence_id.base_event_id == rec
            )

    @api.depends("user_id", "partner_ids", "partner_ids.user_id")
    def _compute_caldav_users(self):
        for rec in self:
            rec.caldav_user_ids = (rec.user_id | rec.partner_ids.user_ids).filtered(
                "is_caldav_enabled"
            )

    def _is_caldav_enabled(self):
        return self.user_id.is_caldav_enabled

    def _get_ical_recurrence_id(self) -> datetime:
        """Get the recurrence-id to use for identifying this event
        specifically in an iCalendar instance.

        :return: The timezone-aware datetime that uniquely identifies this
        event in the recurrence chain.
        """
        event_tz = timezone(self.event_tz or self.env.user.tz or "UTC")
        recurrence_id = utc.localize(self.caldav_recurrence_id).astimezone(event_tz)
        return recurrence_id

    #################################################################
    #### Local Change Methods - Sending Updates to CalDAV Server ####
    #################################################################

    @api.model_create_multi
    def create(self, vals_list):
        ctx = {"caldav_no_sync": True}
        if self.env.context.get(NO_MICROSOFT_SYNC_CONTEXT) or _microsoft_outbound_is_blocked(
            self
        ):
            vals_list = [dict(vals, need_sync_m=False) for vals in vals_list]
        events = super(CalendarEvent, self.with_context(**ctx)).create(vals_list)
        # Microsoft imports use caldav_no_sync to prevent a synchronous CalDAV
        # write. They are not CalDAV events, so do not assign them a synthetic
        # UID that a later CalDAV poll could mistake for a remote event.
        # CalDAV imports also use caldav_keep_ids and still need their supplied
        # UID and recurrence identifiers normalized.
        if not self.env.context.get("caldav_no_sync") or self.env.context.get(
            "caldav_keep_ids"
        ):
            events.with_context(**ctx)._recompute_caldav_ids()
        if not self.env.context.get("caldav_no_sync"):
            events._to_sync()._sync_create_to_caldav()
        return events

    def _microsoft_insert(self, values, timeout=None):
        if _microsoft_sync_is_blocked(self) or _microsoft_outbound_is_blocked(self):
            reason = (
                "source guard"
                if _microsoft_sync_is_blocked(self)
                else "import-only mode"
            )
            _log_microsoft_request(self, "insert", blocked=True, reason=reason)
            return False
        _log_microsoft_request(self, "insert")
        if timeout is None:
            return super()._microsoft_insert(values)
        return super()._microsoft_insert(values, timeout=timeout)

    def _microsoft_patch(self, user_id, event_id, values, timeout=None):
        if _microsoft_sync_is_blocked(self) or _microsoft_outbound_is_blocked(self):
            reason = (
                "source guard"
                if _microsoft_sync_is_blocked(self)
                else "import-only mode"
            )
            _log_microsoft_request(self, "patch", event_id, blocked=True, reason=reason)
            return False
        _log_microsoft_request(self, "patch", event_id)
        if timeout is None:
            return super()._microsoft_patch(user_id, event_id, values)
        return super()._microsoft_patch(
            user_id, event_id, values, timeout=timeout
        )

    def _microsoft_delete(self, user_id, event_id, timeout=None):
        reason = _microsoft_delete_block_reason(self)
        if not reason and _microsoft_outbound_is_blocked(self):
            reason = "import-only mode"
        if reason:
            _log_microsoft_request(
                self, "delete", event_id, blocked=True, reason=reason
            )
            return False
        _log_microsoft_request(self, "delete", event_id)
        if timeout is None:
            return super()._microsoft_delete(user_id, event_id)
        return super()._microsoft_delete(user_id, event_id, timeout=timeout)

    def _sync_odoo2microsoft(self):
        if _microsoft_outbound_is_blocked(self):
            return self
        return super()._sync_odoo2microsoft()

    def _sync_create_to_caldav(self):
        for event in self:
            for user in event.caldav_user_ids:
                client = user._get_caldav_client()
                calendar = client.calendar(url=user.caldav_calendar_url)
                try:
                    caldav_events = event._create_in_icalendar(calendar)
                    for caldav_event in caldav_events:
                        caldav_uid = (
                            caldav_event.vobject_instance.vevent.uid.value
                        )  # pyright: ignore[reportAttributeAccessIssue]
                        event.with_context(caldav_no_sync=True).write(
                            {"caldav_uid": caldav_uid}
                        )
                        created = event.recurrence_id.calendar_event_ids if event.recurrence_id else event
                        self._caldav_record_created(user, created)
                except Exception as e:
                    _logger.error(
                        f"Failed to sync event to CalDAV server: {e}", exc_info=True
                    )

    def write(self, vals):
        vals = dict(vals)
        if self.env.context.get(NO_MICROSOFT_SYNC_CONTEXT) or _microsoft_outbound_is_blocked(
            self
        ):
            vals["need_sync_m"] = False
        res = super(CalendarEvent, self.with_context(caldav_no_sync=True)).write(vals)
        # Events sometimes get archived in Odoo in the process of updating recurrence
        # In that case, we will delete them from the CalDAV server before recreating
        if (
            "active" in vals and not vals.get("active")
            and not self.env.context.get("caldav_no_sync")
        ):
            for event in self:
                event._sync_unlink_to_caldav()
            return res
        to_sync = self._to_sync()
        if to_sync and not self.env.context.get("caldav_no_sync"):
            for rec in to_sync:
                rec._sync_write_to_caldav()
        return res

    def _sync_write_to_caldav(self):
        ical_event_data = self._create_event_data()
        for user in self.caldav_user_ids:
            client = user._get_caldav_client()
            calendar = client.calendar(url=user.caldav_calendar_url)

            base_event = self._get_caldav_base_event_by_uid(calendar, self.caldav_uid)
            if not base_event:
                _logger.warning(
                    "CalDAV event for %s is missing on the server; recreating it.",
                    self,
                )
                event_to_recreate = (
                    self.recurrence_id.base_event_id
                    if self.recurrence_id and not self.is_base_event
                    else self
                )
                base_event = event_to_recreate._update_base_caldav_event(
                    calendar,
                    None,
                    event_to_recreate._create_event_data(),
                )
                if not base_event:
                    _logger.warning(
                        "Unable to recreate CalDAV event for %s on the server.",
                        self,
                    )
                    return
                if not self.recurrence_id or self.is_base_event:
                    return
            if self.recurrence_id:
                tz = timezone(self.event_tz or self.env.user.tz)
                start = utc.localize(self.start).astimezone(tz)
                # We have an event in the recurrence chain that differs from the base
                # We need to add an iCalendar VEVENT with recurrence-id or update it
                if self.is_base_event:
                    # We have a normal base event, so we just update it or create it
                    self._update_base_caldav_event(
                        calendar, base_event, ical_event_data
                    )
                elif (
                    self.differs_from_base_event
                    or start != self._get_ical_recurrence_id()
                ):
                    # We have an event that differs from the base event in some way
                    # So we need to update or create its matching subcomponent, identified
                    # by its recurrence-id
                    if not base_event:
                        _logger.warning(
                            f"Failed to find base event for {self} on CalDAV server."
                        )
                        return
                    index = self._get_subcomponent_index_for_recurrence(base_event)
                    if index:
                        ical_event = base_event.icalendar_instance.subcomponents[index]
                        self._update_ical_event_values(ical_event, ical_event_data)
                    else:
                        base_event.icalendar_instance.add_component(
                            Event(**ical_event_data)
                        )
                    base_event.save()
            else:
                self._update_base_caldav_event(calendar, base_event, ical_event_data)

    def _update_base_caldav_event(
        self,
        calendar: caldav.Calendar,
        event: caldav.Event,
        ical_event_data: dict,
    ):
        if event:
            self._update_ical_event_values(
                event.icalendar_component, ical_event_data
            )  # pyright: ignore[reportAttributeAccessIssue]
            event.save()
            return event
        else:
            return calendar.add_event(**ical_event_data)

    @api.model
    def _update_ical_event_values(
        self, ical_event: icalendar.cal.Event, event_data: dict
    ):
        sequence = (seq := ical_event.get("sequence") or 0) + 1
        event_data.update(sequence=sequence)
        for key, value in event_data.items():
            ical_event[key] = value

    def _get_caldav_base_event_by_uid(
        self, calendar: caldav.Calendar, uid: str
    ) -> Optional[caldav.Event]:
        for event in calendar.events():
            component = (
                event.icalendar_component
            )  # pyright: ignore[reportAttributeAccessIssue]
            event_uid = self._extract_component_text(component, "uid")
            if event_uid == uid and not component.get("recurrence-id"):
                return event

    def unlink(self):
        if not self.env.context.get("caldav_no_sync"):
            for rec in self._to_sync():
                try:
                    rec._sync_unlink_to_caldav()
                except Exception as e:
                    _logger.error(f"Failed to delete event from CalDAV server: {e}")
        return super(CalendarEvent, self).unlink()

    def _get_subcomponent_index_for_recurrence(
        self, caldav_event: caldav.Event
    ) -> Optional[int]:
        ical_instance = caldav_event.icalendar_instance
        for index, component in enumerate(ical_instance.subcomponents):
            if (
                component.get("name") == "VEVENT"
                and (rec_id := component.get("recurrence-id"))
                and rec_id.dt == self._get_ical_recurrence_id()
            ):
                return index

    def _sync_unlink_to_caldav(self):
        delete_all = self.env.context.get("caldav_delete_all", False)
        if self.caldav_uid:
            for user in self.caldav_user_ids:
                client = user._get_caldav_client()
                calendar = client.calendar(url=user.caldav_calendar_url)
                try:
                    caldav_event = calendar.event_by_uid(self.caldav_uid)
                    if not delete_all and self.recurrence_id and not self.is_base_event:
                        index = self._get_subcomponent_index_for_recurrence(
                            caldav_event
                        )
                        if index:
                            caldav_event.icalendar_instance.subcomponents.pop(index)
                        caldav_event.save()
                    else:
                        # In some cases, like when events are detached from a recurrence,
                        # they can be base events but have an old caldav_uid from when
                        # they were recurring. That's why we make sure that the start
                        # of the event matches.
                        if delete_all or self._matches_caldav_start(caldav_event):
                            caldav_event.delete()
                except NotFoundError:
                    # No worries - it just didn't exist so nothing to sync
                    pass
                except Exception as e:
                    _logger.error(f"Failed to remove event from CalDAV server: {e}")

    def _matches_caldav_start(self, caldav_event: caldav.Event) -> bool:
        event_start = caldav_event.icalendar_component.get(
            "dtstart"
        ).dt  # pyright: ignore[reportAttributeAccessIssue]
        if isinstance(event_start, date) and not isinstance(event_start, datetime):
            return self.start.date() == event_start
        if event_start.tzinfo is None:
            event_start = utc.localize(event_start)
        tz = event_start.tzinfo
        self_start = utc.localize(self.start).astimezone(tz)
        return self_start == event_start

    def _update_future_events(self, values, time_values, recurrence_values):
        """When self makes a change updating future events in the recurrence, we need
        only to make sure that self is properly synchronized after the change. We also
        need to make sure that the recurrence rule on the old base event is re-synced.
        The superclass method already archives all the future events and re-creates
        them, so we implicitly delete them from the CalDAV server before writing the
        new base event (self).

        We do, however, need to get a new caldav_uid and caldav_recurrence_id for all
        the events in the new chain.
        """
        ctx = {"keep_caldav_ids": False}
        old_base = self.recurrence_id.base_event_id
        super().with_context(**ctx)._update_future_events(
            values, time_values, recurrence_values
        )
        events_to_refresh = self.recurrence_id._get_events_from(self.start)
        # Future events are archived but not this one. We need to remove the old version
        # from the calendar if it existed.
        self._sync_unlink_to_caldav()
        events_to_refresh.with_context(**ctx)._recompute_caldav_ids()
        self._sync_write_to_caldav()
        old_base._sync_write_to_caldav()

    def _break_recurrence(self, future=True):
        recurrence = self.recurrence_id
        detached_events = super()._break_recurrence(future) | self
        # detached_events are already removed from the CalDAV server in
        # calendar.recurrence._detach_events. We now need to reinitialize them.
        for event in detached_events:
            event._sync_write_to_caldav()
        if future:
            # When future=True, the base event needs to re-sync its recurrence values
            recurrence.base_event_id._sync_write_to_caldav()
        return detached_events - self

    def _rewrite_recurrence(self, values, time_values, recurrence_values):
        """
        When _rewrite_recurrence is called, it archives all the events in the
        recurrence but only after messing with the recurrence_id. This breaks our check
        on is_base_event, so we preemptively sync the deletion here.

        Because this method is usually called with caldav_no_sync=True in the context
        to avoid looping, we manually call _sync_write_to_caldav once the operation is
        completed.
        """
        base = self.recurrence_id.base_event_id
        owner, uid = base.user_id, base.caldav_uid
        proof = None
        if owner and uid and not base._is_microsoft_owned() and not base._caldav_preserves_local_event():
            proof = self._caldav_import_safety_state(owner)
            if proof is not None and not self._caldav_origin_matches(
                base, proof, self._caldav_account_identity(owner),
            ):
                proof = None
        for event in self.recurrence_id.calendar_event_ids:
            event.with_context(caldav_delete_all=True)._sync_unlink_to_caldav()
        # A proven local rewrite preserves the existing resource UID. Never
        # transfer another account's proof or adopt detached historical rows.
        target = self.with_context(caldav_keep_ids=True) if proof is not None else self
        res = super(CalendarEvent, target)._rewrite_recurrence(values, time_values, recurrence_values)
        for event in self.recurrence_id.calendar_event_ids:
            event._recompute_caldav_ids()
        if proof is not None:
            regenerated = self.recurrence_id.calendar_event_ids.filtered(
                lambda event: event.user_id == owner and event.caldav_uid == uid
                and event.recurrence_id and not event._is_microsoft_owned()
                and not event._caldav_preserves_local_event()
            )
            self._caldav_record_created(owner, regenerated)
        self._sync_write_to_caldav()
        return res

    def _to_sync(self):
        """Determine which records in self we need to synchronize with the
        CalDAV server. In essence, we only synchronize base events and those
        differing from the base event in a recurring chain."""
        return self.filtered(
            lambda event: event.id
            and event._is_caldav_enabled()
            and not event._is_microsoft_owned()
            and (event.is_base_event or event.differs_from_base_event)
        )

    def _is_microsoft_owned(self):
        self.ensure_one()
        return any(
            (
                self.microsoft_id,
                self.ms_universal_event_id,
                self.microsoft_recurrence_master_id,
            )
        )

    @api.model
    def _caldav_owned_event_domain(self):
        return [
            ("microsoft_id", "=", False),
            ("ms_universal_event_id", "=", False),
            ("microsoft_recurrence_master_id", "=", False),
        ]

    def _create_in_icalendar(self, calendar: caldav.Calendar) -> List[caldav.Event]:
        """Create an event matching self in the provided calendar.

        :param calendar: The calendar in which to create the event.
        :return: The list of created events, usually one.
        """
        ical_event_data = self._create_event_data()
        try:
            caldav_event = calendar.save_event(**ical_event_data)
        except AttributeError as error:
            missing_recurrence_parent = (
                self.recurrence_id
                and not self.is_base_event
                and "recurrence-id" in ical_event_data
                and str(error) == "'NoneType' object has no attribute 'icalendar_instance'"
            )
            if not missing_recurrence_parent:
                raise

            base_event = self.recurrence_id.base_event_id
            if not base_event or base_event == self:
                raise

            _logger.warning(
                "CalDAV recurrence parent is missing; recreating it before syncing "
                "event %s (UID %s)",
                self.id,
                self.caldav_uid,
            )
            base_caldav_event = calendar.save_event(**base_event._create_event_data())
            if not base_caldav_event or not base_caldav_event.icalendar_instance:
                _logger.warning(
                    "Unable to recreate CalDAV recurrence parent for event %s (UID %s)",
                    self.id,
                    self.caldav_uid,
                )
                return []

            # caldav.save_event() treats a VEVENT with RECURRENCE-ID as an
            # update to its parent.  The parent may not be discoverable by UID
            # immediately after creation, so append the exception directly to
            # the newly-created parent instead of issuing a second save_event().
            base_caldav_event.icalendar_instance.add_component(
                Event(**ical_event_data)
            )
            base_caldav_event.save()
            caldav_event = base_caldav_event
        if self.recurrence_id and self.is_base_event and not self.follow_recurrence:
            ical_event_data = self._create_event_data()
            second_caldav_event = calendar.save_event(**ical_event_data)
            return [caldav_event, second_caldav_event]
        return [caldav_event]

    def _create_event_data(self) -> Dict:
        """Create a dictionary of iCalendar compatible event data."""
        event_data = {}
        self._add_event_dates(event_data)
        self._add_event_header_info(event_data)
        self._add_event_attendees(event_data)
        if self.is_base_event and self.recurrence_id:
            self._add_event_recurrence(event_data)
        elif self.recurrence_id:
            self._add_event_recurrence_id(event_data)
        return event_data

    def _add_event_header_info(self, event_data: Dict) -> None:
        """Add event header info to a dict containing event data.

        IMPORTANT: the VEVENT sequence is set to 0. It should be changed if this method
        is used to update an existing event!

        :param event_data: The dictionary of event data to be updated with header info.
        """
        event_data["uid"] = vText(self.caldav_uid)
        if self.name:
            event_data["summary"] = vText(self.name)
        # TODO: Consider using X-ALT-DESC to stick HTML into the iCal event desc.
        # A booking's description (the reception note) stays in Odoo, as with Outlook.
        if (
            not self._caldav_is_appointment_booking()
            and self.description
            and self._html_to_text(self.description)
        ):
            event_data["description"] = vText(self._html_to_text(self.description))
        if self.location:
            event_data["location"] = vText(self.location)
        if self.videocall_location:
            event_data["conference"] = self.videocall_location
        event_data["sequence"] = 0

    def _add_event_dates(self, event_data: Dict) -> None:
        """Add pertinent dates to event data, based on self."""
        # Determine timezone: prefer event_tz, then user tz, finally UTC
        # Note: All datetimes in Odoo are stored in UTC, so defaulting to UTC is correct.
        #       UTC times are sent in from the appointments app when installed, without
        #       timezone information. This was breaking the sync process due to a call to
        #       upper() on boolean value False.
        tz = self.event_tz or self.env.user.tz or "UTC"
        event_tz = timezone(tz)
        event_data["last-modified"] = vDatetime(
            utc.localize(self.write_date).astimezone(event_tz)
        )
        event_data["created"] = vDatetime(
            utc.localize(self.create_date).astimezone(event_tz)
        )
        event_data["dtstamp"] = vDatetime(utc.localize(self.create_date).astimezone(event_tz))
        event_data["dtstart"] = vDatetime(utc.localize(self.start).astimezone(event_tz))
        event_data["dtend"] = vDatetime(utc.localize(self.stop).astimezone(event_tz))

    def _add_event_recurrence_id(self, event_data: Dict) -> None:
        """Add the recurrence-id parameter to event data if self is linked
        to a calendar.recurrence record."""
        if self.recurrence_id:
            event_data["recurrence-id"] = vDatetime(self._get_ical_recurrence_id())

    def _add_event_recurrence(self, event_data: Dict) -> None:
        """Add the recurrence rule (rrule) to event data if self is
        recurrent. This should only be called for base events."""
        if self.recurrence_id:
            rrule = str(self.recurrence_id._get_rrule())
            rrule_dict = _parse_rrule_string(rrule)
            event_data["rrule"] = vRecur(**rrule_dict)

    def _caldav_is_appointment_booking(self):
        """True for an appointment booking. ``appointment`` is optional for this addon."""
        self.ensure_one()
        return "appointment_type_id" in self._fields and bool(self.appointment_type_id)

    def _caldav_booking_client_partners(self):
        """Attendees of an appointment booking who are not internal users.

        A booking's client stays an Odoo attendee (their change and cancel
        links depend on it) but is never sent to the CalDAV server. The
        ``appointment`` module is optional for this addon.
        """
        self.ensure_one()
        if not self._caldav_is_appointment_booking():
            return self.env["res.partner"]
        return self.partner_ids.filtered(
            lambda partner: not any(
                not user.share
                for user in partner.sudo().with_context(active_test=False).user_ids
            )
        )

    def _add_event_attendees(self, event_data: Dict) -> None:
        """Add the attendee information to the "organizer" and "attendee"
        keys of the event data."""
        attendee_lines = []
        for partner in self.partner_ids - self._caldav_booking_client_partners():
            if partner == self.user_id.partner_id:
                continue
            attendee = vCalAddress(f"MAILTO:{partner.email}")
            attendee.params["cn"] = vText(partner.name)
            attendee_record = self.env["calendar.attendee"].search(
                [("event_id", "=", self.id), ("partner_id", "=", partner.id)],
                limit=1,
            )
            if attendee_record:
                attendee.params["partstat"] = vText(
                    self._map_attendee_status(attendee_record.state)
                )
            attendee_lines.append(attendee)
        organizer = vCalAddress(f"MAILTO:{self.user_id.email}")
        organizer.params["cn"] = self.user_id.name
        event_data["organizer"] = organizer
        event_data["attendee"] = attendee_lines

    @api.model
    def _html_to_text(self, html):
        return md(html, heading_style="ATX")

    @api.model
    def _map_attendee_status(self, state: str) -> str:
        """Map the state of an Odoo event attendee to its iCalendar
        equivalent.

        :param state: The state of the Odoo event attendee.
        :return: The equivalent iCalendar attendee state."""
        mapping = {
            "needsAction": "NEEDS-ACTION",
            "accepted": "ACCEPTED",
            "declined": "DECLINED",
            "tentative": "TENTATIVE",
        }
        return mapping.get(state, "NEEDS-ACTION")

    ###################################################################
    #### Methods for synchronizing changes from the server to Odoo ####
    ###################################################################

    @api.model
    def poll_caldav_server(self) -> None:
        """Poll each user's CalDAV calendar server for changes and
        synchronize them with their Odoo calendar.

        Accounts in a connection-failure backoff are skipped (outbound pushes
        are not: skipping them would let the next successful poll delete the
        unpushed events as orphans). A run stopped by the time budget leaves a
        cursor so the next scheduled run starts with the accounts it did not
        reach. As a cron, each account's outcome is committed on its own and
        reported with remaining=0, so Odoo does not re-run the job at once."""
        now = fields.Datetime.now()
        enabled = self.env["res.users"].search([("is_caldav_enabled", "=", True)], order="id")
        users = enabled.filtered(lambda user: user._caldav_retry_due(now))
        parameters = self.env["ir.config_parameter"].sudo()
        cursor_key = "caldav_sync.poll_cursor"
        try:
            cursor = int(parameters.get_param(cursor_key, 0) or 0)
        except (TypeError, ValueError):
            cursor = 0
        if cursor:
            users = users.filtered(lambda user: user.id >= cursor) + users.filtered(
                lambda user: user.id < cursor
            )
        budget = self._caldav_poll_budget()
        deadline = monotonic() + budget
        in_cron = bool(self.env.context.get("ir_cron_progress_id"))
        cron = self.env["ir.cron"]
        next_cursor = 0
        for index, user in enumerate(users):
            if monotonic() >= deadline:
                next_cursor = user.id
                _logger.info(
                    "CalDAV poll budget of %s s used; %s account(s) wait for the next run",
                    budget, len(users) - index,
                )
                break
            error = None
            try:
                with self.env.cr.savepoint():
                    polled = self.with_context(dont_notify=True)._poll_user_caldav_server(user)
                    if isinstance(polled, CalDAVPartialSyncError):
                        error, backoff = polled, False
            except CalDAVPollStopped:
                _logger.warning("CalDAV poll stopped; current account rolled back, no further accounts started")
                break
            except TransactionRollbackError:
                raise
            except (DAVError, RequestException) as exc:
                error, backoff = exc, True
                _logger.warning(
                    "CalDAV connection failed for user ID %s (%s); other calendars will continue",
                    user.id, type(exc).__name__,
                )
            except Exception as exc:
                # A code or data problem, not an unreachable server: record it
                # but keep polling on schedule.
                error, backoff = exc, False
                _logger.exception(
                    "CalDAV poll failed for user ID %s; other calendars will continue",
                    user.id,
                )
            if error is not None or polled is not False:
                user._caldav_record_poll(error, backoff=error is not None and backoff)
            if in_cron:
                # Commit this account. The job's own budget bounds the run;
                # remaining=0 stops Odoo from looping it back to back.
                cron._commit_progress(1, remaining=0)
        if next_cursor != cursor:
            parameters.set_param(cursor_key, next_cursor or False)

    @api.model
    def _caldav_poll_budget(self):
        """Seconds one poll run may start new accounts (system parameter)."""
        value = self.env["ir.config_parameter"].sudo().get_param(
            "caldav_sync.poll_time_budget", "240"
        )
        try:
            return max(float(value), 1.0)
        except (TypeError, ValueError):
            return 240.0

    def _caldav_preserves_local_event(self):
        """Let source-owned calendar mirrors retain their source's authority."""
        self.ensure_one()
        preserve = getattr(self, "_calendar_sync_preserves_local_event", None)
        return bool(preserve and preserve())

    @api.model
    def _caldav_local_versions(self, user, known_ids):
        domain = self._caldav_owned_event_domain() + [
            ("caldav_uid", "!=", False),
            "|", ("user_id", "=", user.id), ("id", "in", sorted(known_ids)),
        ]
        # write_date alone can stay equal for multiple writes in one transaction.
        # Hash import-relevant values in one ORM batch; never persist their text.
        field_names = [
            "write_date", "name", "start", "stop", "start_date", "stop_date",
            "allday", "description", "location", "videocall_location",
            "partner_ids", "user_id", "recurrence_id", "caldav_uid",
            "caldav_recurrence_id", "rrule_type", "interval", "end_type",
            "count", "until", "mon", "tue", "wed", "thu", "fri", "sat",
            "sun", "month_by", "day", "weekday", "byday",
        ]
        field_names = [name for name in field_names if name in self._fields]
        return {
            str(row["id"]): hashlib.sha256(json.dumps(
                row, default=str, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            for row in self.search_read(domain, field_names, load=None)
        }

    @api.model
    def _caldav_collection_token(self, calendar):
        try:
            token = calendar.get_properties([dav.SyncToken()]).get(dav.SyncToken.tag)
        except (DAVError, NotImplementedError):
            # Older servers can still use the resource-content cache below.
            return None
        return token if isinstance(token, str) and token else None

    @api.model
    def _caldav_local_fingerprint(self, user, known_ids):
        """One SQL digest that changes whenever a row behind _caldav_local_versions may.

        It covers a superset of those records (archived ones too) and uses the
        row versions (xmin) of each event and its recurrence plus the attendee
        links, so any committed write changes it. A changed fingerprint only
        means "compare the exact versions"; it never skips work by itself.
        """
        # Pending writes (and stored computes they trigger) must be in the rows.
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT md5(COALESCE(string_agg(
                       e.id::text || ':' || e.xmin::text || ':'
                       || COALESCE(r.xmin::text, '') || ':' || COALESCE(p.ids, ''),
                       ',' ORDER BY e.id), ''))
              FROM calendar_event e
         LEFT JOIN calendar_recurrence r ON r.id = e.recurrence_id
         LEFT JOIN LATERAL (
                    SELECT string_agg(rel.res_partner_id::text, '.'
                                      ORDER BY rel.res_partner_id) AS ids
                      FROM calendar_event_res_partner_rel rel
                     WHERE rel.calendar_event_id = e.id
                   ) p ON TRUE
             WHERE e.caldav_uid IS NOT NULL
               AND COALESCE(e.microsoft_id, '') = ''
               AND COALESCE(e.ms_universal_event_id, '') = ''
               AND COALESCE(e.microsoft_recurrence_master_id, '') = ''
               AND (e.user_id = %s OR e.id = ANY(%s))
            """,
            (user.id, sorted(known_ids)),
        )
        return self.env.cr.fetchone()[0]

    @api.model
    def _caldav_email_key(self, email):
        """Short, non-reversible key of an address exactly as it is matched."""
        # Must equal left(md5(email), 16) in _caldav_contact_version (UTF-8 DB).
        return hashlib.md5(email.encode()).hexdigest()[:16]

    @api.model
    def _caldav_ical_email_keys(self, ical_event, user):
        """Keys of every address an import of ical_event looks contacts up by."""
        emails = {user.partner_id.email or ""}
        for component in ical_event.walk():
            if component.name != "VEVENT":
                continue
            emails.update(self._get_ical_attendee_emails(component))
            organizer = component.get("organizer")
            if organizer:
                emails.add(_extract_vcal_email(organizer))
        return {self._caldav_email_key(email) for email in emails if email}

    @api.model
    def _caldav_contact_version(self, email_keys):
        """Digest of the contacts an import can link: partners whose email is
        one of the calendar's attendee/organizer addresses (email_keys).

        A new, archived or re-addressed contact with such an address, or a
        change to its users, invalidates an otherwise unchanged import; edits
        to contacts this calendar never mentions do not.
        """
        self.env.flush_all()
        self.env.cr.execute(
            """
            WITH p AS (
                SELECT id, email, active, user_id
                  FROM res_partner
                 WHERE email IS NOT NULL AND left(md5(email), 16) = ANY(%s)
            )
            SELECT md5(
                COALESCE((SELECT string_agg(
                              p.id::text || ':' || p.email || ':' || p.active::text
                              || ':' || COALESCE(p.user_id::text, ''),
                              ',' ORDER BY p.id) FROM p), '')
                || '|' ||
                COALESCE((SELECT string_agg(
                              u.id::text || ':' || u.partner_id::text || ':' || u.active::text,
                              ',' ORDER BY u.id)
                            FROM res_users u WHERE u.partner_id IN (SELECT id FROM p)), '')
            )
            """,
            (sorted(email_keys),),
        )
        return self.env.cr.fetchone()[0]

    @api.model
    def _poll_user_caldav_server(self, user) -> None:
        """Poll a single user's CalDAV calendar on the server for changes
        and synchronize them to Odoo.

        :param user: The res.user record for whom to synchronize events.
        """
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s), %s)",
            ("caldav_sync.poll", user.id),
        )
        if not self.env.cr.fetchone()[0]:
            # Another worker is polling this account; it records the outcome.
            return False
        self._caldav_check_stop()
        _logger.info("Polling CalDAV server for user ID %s", user.id)
        calendar = user._get_caldav_client().calendar(url=user.caldav_calendar_url)
        parameters = self.env["ir.config_parameter"].sudo()
        cache_key = "caldav_sync.poll_cache.%s" % user.id
        identity = self._caldav_account_identity(user)
        cached_text = parameters.get_param(cache_key, "")
        try:
            cached = json.loads(cached_text) if cached_text else {}
        except (ValueError, TypeError):
            cached = {}
        if (
            not isinstance(cached, dict) or cached.get("version") != CALDAV_POLL_CACHE_VERSION
            or cached.get("identity") != identity
        ):
            cached = {}
        objects = cached.get("objects", {})
        if not isinstance(objects, dict):
            objects = {}
        known_ids = {
            event_id for entry in objects.values() if isinstance(entry, dict)
            for event_id in entry.get("ids", []) if isinstance(event_id, int)
        }
        email_keys = {
            key for key in cached.get("email_keys", []) if isinstance(key, str)
        }
        # Read before the snapshot so a change during fetching is seen next poll.
        fingerprint = self._caldav_local_fingerprint(user, known_ids)
        contact_version = self._caldav_contact_version(email_keys)
        contacts_unchanged = bool(cached) and contact_version == cached.get("contact_version")
        token = self._caldav_collection_token(calendar)
        remote_unchanged = token and token == cached.get("token") and contacts_unchanged
        if remote_unchanged and fingerprint == cached.get("fingerprint"):
            _logger.info("CalDAV calendar unchanged for user ID %s", user.id)
            return
        versions = self._caldav_local_versions(user, known_ids)
        if remote_unchanged and versions == cached.get("versions"):
            # Rows were rewritten without an import-relevant change.
            self._save_caldav_poll_cache(
                user, parameters, cache_key, cached_text, identity,
                token, objects, email_keys,
            )
            _logger.info("CalDAV calendar unchanged for user ID %s", user.id)
            return
        # Freeze the denominator before imports, so newly imported rows cannot
        # enlarge the deletion allowance during this run.
        local_before = self.search([
            ("user_id", "=", user.id), ("caldav_uid", "!=", False),
        ] + self._caldav_owned_event_domain())
        cleanup_total = len(local_before)
        resource_baseline = max(len(objects), len(set(local_before.mapped("caldav_uid"))))
        # Restore checks used to issue one search per resource. Load the
        # inactive CalDAV rows once and pass the UID-indexed view through the
        # import batch; this is also the only state needed for reactivation.
        inactive_before = self.with_context(active_test=False).search([
            ("user_id", "=", user.id), ("active", "=", False),
            ("caldav_uid", "!=", False),
        ] + self._caldav_owned_event_domain())
        inactive_by_uid = {}
        for event in inactive_before:
            inactive_by_uid.setdefault(event.caldav_uid, self.browse())
            inactive_by_uid[event.caldav_uid] |= event
        events = list(calendar.events())
        self = self.with_context(
            caldav_review_seen=set(),
            caldav_review_notices={"pending": bool(cached.get("review_notices", True))},
        )
        synced_ids = set()
        updated_objects = {}
        processed = skipped = 0
        yearly_uids = set()
        sync_complete = True
        unexpected_failure = False
        blocked_uids = set()
        origin_batch = []
        for resource_index, caldav_event in enumerate(events):
            if resource_index % 25 == 0:
                self._caldav_check_stop()
            try:
                data = caldav_event.data
                raw = data if isinstance(data, bytes) else data.encode("utf-8")
                if b"FREQ=YEARLY" in raw.upper():
                    ical_event = caldav_event.icalendar_instance
                    if _is_yearly_ical_event(ical_event):
                        yearly_uids.update(
                            str(component.get("uid"))
                            for component in ical_event.walk()
                            if component.name == "VEVENT"
                        )
                        continue
                digest = hashlib.sha256(
                    data if isinstance(data, bytes) else data.encode("utf-8")
                ).hexdigest()
                resource_key = hashlib.sha256(
                    str(caldav_event.url or digest).encode()
                ).hexdigest()
                previous = objects.get(resource_key, {})
                if (
                    contacts_unchanged and previous.get("digest") == digest
                    and previous.get("ids")
                    and all(
                        str(event_id) in versions
                        and versions[str(event_id)] == previous.get("versions", {}).get(str(event_id))
                        for event_id in previous["ids"]
                    )
                ):
                    synced_ids.update(previous["ids"])
                    self.env.context["caldav_review_seen"].update(previous.get("review_keys", []))
                    updated_objects[resource_key] = previous
                    skipped += 1
                    continue
                # A malformed event must not poison the transaction or make the
                # rest of the snapshot look like a deletion request.
                resource_review_keys = set()
                with self.env.cr.savepoint():
                    ical_event = caldav_event.icalendar_instance
                    imported = self.with_context(caldav_review_resource_keys=resource_review_keys)._sync_event_from_ical(
                        ical_event, user, origin_batch=origin_batch,
                        restore_rows_by_uid=inactive_by_uid,
                    )
                email_keys |= self._caldav_ical_email_keys(ical_event, user)
                synced_ids.update(imported.ids)
                if imported:
                    updated_objects[resource_key] = {
                        "digest": digest, "ids": imported.ids,
                        "review_keys": sorted(resource_review_keys),
                    }
                    # A changed recurrence can affect a later cached resource too.
                    for event_id in imported.ids:
                        versions.pop(str(event_id), None)
                processed += 1
            except TransactionRollbackError:
                # The whole transaction must retry; never cache a rolled-back import.
                raise
            except CalDAVResourceReview as error:
                blocked_uids.add(error.uid)
                self._caldav_review_notice(
                    user, error.kind, resource_key, digest, error.event_ids,
                )
                if error.kind == "mixed":
                    # A detached UID taking over a linked/plain resource is a
                    # user-level snapshot ambiguity. Keep imported rows, but
                    # fail closed for cleanup of every resource in this poll.
                    sync_complete = False
            except Exception:
                sync_complete = False
                unexpected_failure = True
                _logger.exception(
                    "Failed to import a CalDAV event for user ID %s; "
                    "orphan cleanup will be skipped",
                    user.id,
                )

        self._caldav_flush_origin_batch(origin_batch)
        if not sync_complete:
            (_logger.warning if unexpected_failure else _logger.info)(
                "Skipping CalDAV orphan cleanup for user ID %s because the "
                "remote snapshot was not imported completely",
                user.id,
            )
            self._save_caldav_poll_cache(
                user, parameters, cache_key, cached_text, identity,
                None, updated_objects, email_keys,
            )
            return CalDAVPartialSyncError("Incomplete CalDAV snapshot")

        if not resource_baseline or len(events) >= resource_baseline * 0.50:
            self._caldav_backfill_origin(user, synced_ids)
        cleanup_error = self._caldav_cleanup_snapshot(
            user, synced_ids, yearly_uids, len(events), cleanup_total,
            resource_baseline=resource_baseline, calendar=calendar,
            blocked_uids=blocked_uids,
        )

        if not resource_baseline or len(events) >= resource_baseline * 0.50:
            self._caldav_clear_resolved_notices(user)
        self._save_caldav_poll_cache(
            user, parameters, cache_key, cached_text, identity,
            None if cleanup_error else token, updated_objects, email_keys,
        )
        if cleanup_error:
            return cleanup_error
        _logger.info(
            "CalDAV snapshot for user ID %s: %s processed, %s unchanged",
            user.id, processed, skipped,
        )

    @api.model
    def _save_caldav_poll_cache(
        self, user, parameters, cache_key, cached_text, identity, token, objects,
        email_keys,
    ):
        known_ids = {event_id for entry in objects.values() for event_id in entry["ids"]}
        versions = self._caldav_local_versions(user, known_ids)
        for entry in objects.values():
            entry["versions"] = {
                str(event_id): versions.get(str(event_id)) for event_id in entry["ids"]
            }
        notice_state = self.env.context.get("caldav_review_notices")
        if notice_state is None:
            has_notices = bool(json.loads(cached_text or "{}").get("review_notices", True))
        else:
            has_notices = bool(notice_state["pending"] or self.env.context.get("caldav_review_seen"))
        value = json.dumps({
            "review_notices": has_notices,
            "version": CALDAV_POLL_CACHE_VERSION, "identity": identity, "token": token,
            "contact_version": self._caldav_contact_version(email_keys),
            "email_keys": sorted(email_keys),
            "fingerprint": self._caldav_local_fingerprint(user, known_ids),
            "objects": objects, "versions": versions,
        }, sort_keys=True, separators=(",", ":"))
        if value != cached_text:
            parameters.set_param(cache_key, value)

    @api.model
    def _caldav_account_identity(self, user):
        return hashlib.sha256(json.dumps([
            user.id, user.caldav_calendar_url, user.caldav_username,
        ]).encode()).hexdigest()

    @api.model
    def _caldav_safety_state(self, user):
        """Proof of creation and archive history, independent of the poll cache.

        Never infer origin from organizer ownership or a resource-cache hit.
        Legacy rows have no creation proof and are ineligible for cleanup.
        Account changes invalidate origin matches but never reset the 24 h cap.
        Corrupt state fails closed rather than granting a fresh budget.
        """
        key = "caldav_sync.safety.%s" % user.id
        text = self.env["ir.config_parameter"].sudo().get_param(key, "")
        try:
            state = json.loads(text) if text else {"created": {}, "archives": []}
            if not isinstance(state, dict) or not isinstance(state["created"], dict):
                raise ValueError()
            if not isinstance(state["archives"], list):
                raise ValueError()
            if not isinstance(state.get("positions", {}), dict):
                raise ValueError()
            for entry in state["archives"]:
                fields.Datetime.to_datetime(entry["at"])
                if not isinstance(entry["uids"], list) or any(not isinstance(uid, str) for uid in entry["uids"]):
                    raise ValueError()
                if not entry["at"] or not isinstance(entry["ids"], list) or any(
                    type(event_id) is not int for event_id in entry["ids"]
                ):
                    raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CalDAVPartialSyncError("Invalid CalDAV safety ledger") from None
        return self._caldav_prune_safety_state(state)

    @api.model
    def _caldav_prune_safety_state(self, state):
        """Bound nonessential history; never refund the rolling 24 h budget."""
        state.setdefault("positions", {})
        cutoff = fields.Datetime.now() - timedelta(days=90)
        ids = {int(key) for key in state["created"] if str(key).isdigit()}
        ids.update(entry.get("id") for entry in state.get("positions", {}).values()
                   if isinstance(entry, dict) and type(entry.get("id")) is int)
        rows = self.with_context(active_test=False).search_read(
            [("id", "in", sorted(ids))], ["active", "write_date"], load=None,
        ) if ids else []
        retained_ids = {
            row["id"] for row in rows
            if row["active"] or not row["write_date"] or fields.Datetime.to_datetime(row["write_date"]) >= cutoff
        }

        def retained(entry, event_id):
            # Origin and detached-position mappings remain valid for the
            # lifetime of a visible row. Age of the proof is not expiry.
            return event_id in retained_ids and isinstance(entry, dict)

        state["created"] = {key: entry for key, entry in state["created"].items()
                            if str(key).isdigit() and retained(entry, int(key))}
        state["positions"] = {key: entry for key, entry in state.get("positions", {}).items()
                              if isinstance(entry, dict) and retained(entry, entry.get("id"))}
        state["archives"] = [entry for entry in state["archives"]
                             if fields.Datetime.to_datetime(entry["at"]) >= cutoff]
        return state

    @api.model
    def _caldav_save_safety_state(self, user, state):
        self.env["ir.config_parameter"].sudo().set_param(
            "caldav_sync.safety.%s" % user.id,
            json.dumps(self._caldav_prune_safety_state(state), sort_keys=True, separators=(",", ":")),
        )

    @api.model
    def _caldav_origin_matches(self, event, state, identity):
        if event.recurrency and not event.recurrence_id:
            return False
        origin = state["created"].get(str(event.id), {})
        return origin.get("account") == identity and origin.get("uid") == hashlib.sha256(event.caldav_uid.encode()).hexdigest()

    @api.model
    def _caldav_import_safety_state(self, user):
        # Unreadable deletion evidence must not hide a newly imported meeting.
        # Keep the corrupt value intact; only archiving is blocked until repair.
        try:
            return self._caldav_safety_state(user)
        except CalDAVPartialSyncError:
            self._caldav_review_notice(user, "ledger", "ledger", "invalid", [])
            return None

    @api.model
    def _caldav_warning_once(self, user, kind, resource_key, fingerprint):
        # One parameter per condition/resource, replaced when its content
        # changes. No raw UID or meeting content is stored.
        notice_key = "caldav_sync.review.%s.%s" % (
            user.id, hashlib.sha256((kind + resource_key).encode()).hexdigest(),
        )
        for name in ("caldav_review_seen", "caldav_review_resource_keys"):
            seen = self.env.context.get(name)
            if seen is not None:
                seen.add(notice_key)
        parameters = self.env["ir.config_parameter"].sudo()
        value = hashlib.sha256(fingerprint.encode()).hexdigest()
        if parameters.get_param(notice_key) == value:
            return False
        parameters.set_param(notice_key, value)
        return True

    @api.model
    def _caldav_clear_resolved_notices(self, user):
        seen = self.env.context.get("caldav_review_seen")
        if seen is None:
            return
        notice_state = self.env.context.get("caldav_review_notices")
        # Once a complete poll has reconciled the notice set, an unchanged
        # resource-only poll with no notices needs no extra database scan.
        # Legacy caches default to pending and receive one full reconciliation.
        if notice_state is not None and not notice_state["pending"] and not seen:
            return
        notices = self.env["ir.config_parameter"].sudo().search([
            ("key", "=like", "caldav_sync.review.%s.%%" % user.id),
        ])
        notices.filtered(lambda notice: notice.key not in seen).unlink()
        if notice_state is not None:
            notice_state["pending"] = bool(seen)

    @api.model
    def _caldav_review_notice(self, user, kind, resource_key, fingerprint, event_ids):
        """Warn once per unchanged review condition and leave an admin TODO."""
        if not self._caldav_warning_once(user, kind, resource_key, fingerprint):
            return
        _logger.warning(
            "CalDAV review required for user ID %s (%s), event IDs %s; no rows archived",
            user.id, kind, sorted(event_ids),
        )
        activities = self.env["mail.activity"].sudo().with_context(mail_activity_quick_update=True)
        summary = "CalDAV: vyžaduje kontrolu (%s)" % kind
        if not activities.search_count([
            ("res_model", "=", "res.partner"), ("res_id", "=", user.partner_id.id),
            ("summary", "=", summary),
        ]):
            activities.create({
                "res_model_id": self.env["ir.model"]._get_id("res.partner"),
                "res_id": user.partner_id.id, "user_id": self.env.ref("base.user_admin").id,
                "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
                "summary": summary,
                "note": _(
                    "CalDAV používateľa ID %(user)s vyžaduje kontrolu typu %(kind)s. "
                    "Udalosti zostali viditeľné. ID: %(ids)s",
                    user=user.id, kind=kind, ids=", ".join(map(str, sorted(event_ids))) or "žiadne",
                ),
            })

    @api.model
    def _caldav_record_created(self, user, events, origin_batch=None):
        """Called only for rows created by this import, in its savepoint."""
        events = events.filtered(lambda event: not (event.recurrency and not event.recurrence_id))
        if origin_batch is not None:
            origin_batch.append((user.id, events.ids))
            return
        state = self._caldav_import_safety_state(user)
        if state is None:
            return
        identity = self._caldav_account_identity(user)
        for event in events:
            state["created"][str(event.id)] = {
                "account": identity,
                "uid": hashlib.sha256(event.caldav_uid.encode()).hexdigest(),
                "at": fields.Datetime.to_string(fields.Datetime.now()),
            }
        self._caldav_save_safety_state(user, state)

    @api.model
    def _caldav_flush_origin_batch(self, pending):
        if not pending:
            return
        grouped = {}
        for user_id, ids in pending:
            grouped.setdefault(user_id, set()).update(ids)
        for user_id, ids in grouped.items():
            user = self.env["res.users"].browse(user_id)
            state = self._caldav_import_safety_state(user)
            if state is None:
                continue
            identity = self._caldav_account_identity(user)
            now = fields.Datetime.to_string(fields.Datetime.now())
            for event in self.with_context(active_test=False).browse(sorted(ids)).exists():
                if event.caldav_uid and not (event.recurrency and not event.recurrence_id):
                    state["created"][str(event.id)] = {
                        "account": identity,
                        "uid": hashlib.sha256(event.caldav_uid.encode()).hexdigest(),
                        "at": now,
                    }
            self._caldav_save_safety_state(user, state)

    @api.model
    def _caldav_backfill_origin(self, user, synced_ids):
        marker = "caldav_sync.origin_backfill.%s" % user.id
        parameters = self.env["ir.config_parameter"].sudo()
        if parameters.get_param(marker):
            return
        state = self._caldav_import_safety_state(user)
        if state is None:
            return
        known = set()
        for parameter in parameters.search([("key", "like", "caldav_sync.safety.%")]):
            try:
                other = json.loads(parameter.value)
            except (ValueError, TypeError):
                continue
            if isinstance(other, dict) and isinstance(other.get("created"), dict):
                known.update(int(event_id) for event_id in other["created"] if str(event_id).isdigit())
        identity = self._caldav_account_identity(user)
        now = fields.Datetime.to_string(fields.Datetime.now())
        rows = self.with_context(active_test=False).browse(sorted(synced_ids)).exists().filtered(
            lambda event: event.active and event.user_id == user and event.caldav_uid
            and event.id not in known and not event._caldav_preserves_local_event()
            and not (event.recurrency and not event.recurrence_id)
        )
        for event in rows:
            state["created"][str(event.id)] = {
                "account": identity,
                "uid": hashlib.sha256(event.caldav_uid.encode()).hexdigest(),
                "at": now,
            }
        self._caldav_save_safety_state(user, state)
        parameters.set_param(marker, now)

    @api.model
    def _caldav_check_stop(self):
        # A poll transaction uses repeatable-read. Its own cursor cannot see a
        # new emergency flag, so read it through a fresh independent snapshot.
        with self.env.registry.cursor() as stop_cursor:
            stop_cursor.execute(
                "SELECT value FROM ir_config_parameter WHERE key = %s",
                ("caldav_sync.stop_requested",),
            )
            row = stop_cursor.fetchone()
        if row and str(row[0]).lower() in ("1", "true", "yes", "on"):
            raise CalDAVPollStopped()

    @api.model
    def _caldav_cleanup_limits(self, resource_count):
        parameters = self.env["ir.config_parameter"].sudo()
        try:
            small = int(parameters.get_param("caldav_sync.cleanup_small_batch", "5"))
            minimum = int(parameters.get_param("caldav_sync.cleanup_24h_min", "20"))
            ratio = float(parameters.get_param("caldav_sync.cleanup_24h_ratio", "0.10"))
            if small < 1 or minimum < small or not 0 <= ratio <= 1:
                raise ValueError()
        except (TypeError, ValueError):
            raise CalDAVPartialSyncError("Invalid CalDAV circuit-breaker settings") from None
        # Resources, never occurrence rows. Small calendars get at least the
        # small-batch allowance, subject to the cumulative rolling limit.
        return max(small, minimum, int(resource_count * ratio))

    @api.model
    def _caldav_cleanup_meeting_limit(self):
        try:
            limit = int(self.env["ir.config_parameter"].sudo().get_param(
                "caldav_sync.cleanup_24h_meetings", "100",
            ))
            if limit < 1:
                raise ValueError()
            return limit
        except (TypeError, ValueError):
            raise CalDAVPartialSyncError("Invalid CalDAV meeting-row limit") from None

    @api.model
    def _caldav_cleanup_plan(self, user, synced_ids, yearly_uids, calendar, blocked_uids=None):
        """LIVE cb7707a candidate selection and recurrence confirmation.

        Compute the whole set before any archive. A listing omission alone must
        not remove a recurrence: event_by_uid must explicitly return NotFound.
        Unlike LIVE, unioning sets also avoids unlinking differing rows twice.
        """
        blocked_uids = blocked_uids or set()
        orphans = self.search([
            ("caldav_uid", "!=", False), ("id", "not in", sorted(synced_ids)),
            ("user_id", "=", user.id),
        ] + self._caldav_owned_event_domain())._to_sync().filtered(
            lambda event: event.caldav_uid not in blocked_uids
            and not event._caldav_preserves_local_event()
        )
        base_orphans = orphans.filtered(lambda event: event.recurrence_id and event.is_base_event)
        candidates = orphans - base_orphans
        for base in base_orphans:
            if calendar is None:
                raise CalDAVPartialSyncError("No remote recurrence confirmation available")
            try:
                calendar.event_by_uid(base.caldav_uid)
            except NotFoundError:
                candidates |= base.recurrence_id.calendar_event_ids
        if yearly_uids:
            candidates |= self.search([
                ("caldav_uid", "in", sorted(yearly_uids)), ("user_id", "=", user.id),
            ] + self._caldav_owned_event_domain())
        # Preserve the existing source-ownership protections also after a whole
        # chain is expanded. These filters can only reduce LIVE's removals.
        candidates = candidates.filtered(
            lambda event: event.active and event.user_id == user and not event._is_microsoft_owned()
            and not event._caldav_preserves_local_event()
        )
        detached = candidates.filtered(lambda event: event.recurrency and not event.recurrence_id)
        if detached:
            self._caldav_review_notice(
                user, "detached", "detached", str(sorted(detached.ids)), sorted(detached.ids),
            )
        # Historical detached occurrences have no authoritative remote chain.
        # Their retirement requires a separate owner decision, even if an old
        # ledger or an admin approval once claimed them as sync-owned.
        return candidates - detached

    @api.model
    def _caldav_cleanup_snapshot(
        self, user, synced_ids, yearly_uids, remote_count, total=None,
        resource_baseline=0, calendar=None, blocked_uids=None,
    ):
        """Plan LIVE removals, apply origin guard and a 24 h resource breaker."""
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), %s)",
            ("caldav_sync.poll", user.id),
        )
        self._caldav_check_stop()
        if resource_baseline and (
            remote_count <= resource_baseline * 0.10
            or remote_count < resource_baseline * 0.50
        ):
            self._caldav_review_notice(
                user, "snapshot", "snapshot", str((resource_baseline, remote_count)), [],
            )
            return CalDAVPartialSyncError("Empty or substantially reduced CalDAV snapshot")
        candidates = self._caldav_cleanup_plan(
            user, synced_ids, yearly_uids, calendar, blocked_uids,
        )
        try:
            state = self._caldav_safety_state(user)
            identity = self._caldav_account_identity(user)
            resource_count = max(resource_baseline, remote_count, len(set(self.search([
                ("user_id", "=", user.id), ("caldav_uid", "!=", False),
            ] + self._caldav_owned_event_domain()).mapped("caldav_uid"))))
            limit = self._caldav_cleanup_limits(resource_count)
            meeting_limit = self._caldav_cleanup_meeting_limit()
        except CalDAVPartialSyncError:
            _logger.warning("CalDAV cleanup blocked for user ID %s: invalid safety ledger or settings", user.id)
            self._caldav_cleanup_notice(user, len(candidates), 0)
            return CalDAVPartialSyncError("Invalid CalDAV safety ledger or settings")
        unproven = candidates.filtered(lambda event: not self._caldav_origin_matches(event, state, identity))
        if unproven:
            self._caldav_review_notice(
                user, "origin", "origin", str(sorted(unproven.ids)), sorted(unproven.ids),
            )
            candidates -= unproven
        if not candidates:
            state.pop("pending", None)
            state.pop("approval", None)
            self._caldav_save_safety_state(user, state)
            return
        # Prove the exact pending set, including each row's current version. An
        # edit, ownership change, different account or different candidate IDs
        # invalidates an approval. No raw remote UID is persisted.
        uids = sorted({hashlib.sha256(event.caldav_uid.encode()).hexdigest() for event in candidates})
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT id, md5(to_jsonb(e)::text) FROM calendar_event e WHERE id = ANY(%s) ORDER BY id",
            (sorted(candidates.ids),),
        )
        signature = hashlib.sha256(json.dumps([
            identity, uids, self.env.cr.fetchall(),
        ], sort_keys=True, default=str).encode()).hexdigest()
        now = fields.Datetime.now()
        recent = [entry for entry in state["archives"]
                  if fields.Datetime.to_datetime(entry["at"]) > now - timedelta(hours=24)]
        used = sum(len(entry["uids"]) for entry in recent)
        meetings_used = sum(len(entry["ids"]) for entry in recent)
        approved = state.get("approval") == signature
        if (used + len(uids) > limit or meetings_used + len(candidates) > meeting_limit) and not approved:
            state["pending"] = {
                "signature": signature, "ids": sorted(candidates.ids), "uids": uids,
                "at": fields.Datetime.to_string(now), "resource_count": resource_count,
                "used": used, "limit": limit,
                "meetings_used": meetings_used, "meeting_limit": meeting_limit,
            }
            state.pop("approval", None)
            self._caldav_save_safety_state(user, state)
            if self._caldav_warning_once(user, "budget", "budget", signature):
                _logger.warning(
                    "CalDAV circuit breaker user ID %s: pending %s resources / %s rows, "
                    "24h resources %s/%s, meetings %s/%s; event IDs %s. No rows archived",
                    user.id, len(uids), len(candidates), used, limit,
                    meetings_used, meeting_limit, sorted(candidates.ids),
                )
            self._caldav_cleanup_notice(user, len(uids), limit, len(candidates), meeting_limit)
            return CalDAVPartialSyncError("CalDAV circuit breaker requires approval")
        self._caldav_check_stop()
        state["archives"] = recent + [{
            "at": fields.Datetime.to_string(now), "ids": sorted(candidates.ids), "uids": uids,
        }]
        state.pop("pending", None)
        state.pop("approval", None)
        self._caldav_save_safety_state(user, state)
        candidates.with_context(
            **CALDAV_IMPORT_CONTEXT, caldav_no_sync=True, caldav_keep_ids=True,
            dont_notify=True, skip_attendee_notification=True,
        ).with_user(user).write({"active": False})
        _logger.warning(
            "CalDAV archived user ID %s event IDs %s at %s; resources %s; "
            "rolling 24h used %s/%s; one-time approval %s",
            user.id, sorted(candidates.ids), fields.Datetime.to_string(now),
            len(uids), used + len(uids), limit, approved,
        )
        return

    @api.model
    def _caldav_approve_cleanup(self, users):
        """Admin server action: release only the pending set on the next poll."""
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Odstránenie môže schváliť iba správca nastavení."))
        if users._name != "res.users" or len(users) != 1:
            raise UserError(_("Vyberte presne jedného používateľa."))
        user = users.sudo()
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), %s)",
            ("caldav_sync.poll", user.id),
        )
        state = self._caldav_safety_state(user)
        pending = state.get("pending")
        if not pending or not pending.get("signature"):
            raise UserError(_("Používateľ nemá čakajúcu požiadavku na odstránenie."))
        state["approval"] = pending["signature"]
        self._caldav_save_safety_state(user, state)
        _logger.warning(
            "CalDAV administrator ID %s approved exact pending set for user ID %s, event IDs %s",
            self.env.uid, user.id, pending["ids"],
        )
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("Schváliť odstránenie"),
            "message": _("Schválená je iba táto čakajúca množina. Ďalšia úplná synchronizácia ju znovu overí."),
            "type": "warning", "sticky": True,
        }}

    @api.model
    def _caldav_cleanup_notice(self, user, count, limit, meetings=0, meeting_limit=0):
        """A durable, deduplicated TODO visible to the settings administrator.

        Contains only record IDs and counts. No attendee details or credentials.
        It remains until an administrator acknowledges it, even after recovery.
        """
        activities = self.env["mail.activity"].sudo().with_context(mail_activity_quick_update=True)
        summary = "CalDAV: zablokované odstraňovanie udalostí"
        if activities.search_count([
            ("res_model", "=", "res.partner"), ("res_id", "=", user.partner_id.id),
            ("summary", "=", summary),
        ]):
            return
        admin = self.env.ref("base.user_admin")
        activities.create({
            "res_model_id": self.env["ir.model"]._get_id("res.partner"),
            "res_id": user.partner_id.id, "user_id": admin.id,
            "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
            "summary": summary,
            "note": _(
                "Synchronizácia používateľa ID %(user)s navrhla archivovať %(count)s "
                "vzdialených zdrojov (24-hodinový limit %(limit)s). Nič sa neodstránilo. "
                "Počet stretnutí %(meetings)s (24-hodinový limit %(meeting_limit)s). "
                "Skontrolujte vzdialený kalendár a stav caldav_sync.health.%(user)s. "
                "V Nastavenia > Používatelia vyberte používateľa ID %(user)s a akciu "
                "Schváliť odstránenie. Schválenie platí iba pre presnú čakajúcu množinu.",
                user=user.id, count=count, limit=limit, meetings=meetings, meeting_limit=meeting_limit,
            ),
        })

    @api.model
    def _sync_event_from_ical(
        self, ical_event: icalendar.cal.Event, user: User, origin_batch=None,
        restore_rows_by_uid=None,
    ):
        """Given an iCalendar event, compare the event with any existing
        Odoo event that it matches and synchronize the changes. If no event
        exists, create it regardless of its date.

        :param ical_event: The iCalendar event to synchronize with Odoo.
        :param user: The res.user record for whom to synchronize the event.
        :return: The calendar.event records that were synchronized.
        """
        synced_events = self.env["calendar.event"]
        event_components = [
            component for component in ical_event.walk() if component.name == "VEVENT"
        ]
        for component in event_components:
            uid = str(component.get("uid"))
            recurrence_id = (
                component.get("recurrence-id").dt
                if "recurrence-id" in component
                else None
            )

            # Include archived historical rows before choosing the importer.
            # Restoring them through the normal linked-series path would
            # restore only the RRULE subset and then roll it back as ambiguous.
            detached = self.with_context(active_test=False).search([
                ("caldav_uid", "=", uid), ("user_id", "=", user.id),
                ("recurrency", "=", True), ("recurrence_id", "=", False),
            ] + self._caldav_owned_event_domain())
            if detached and not detached.filtered("active"):
                # A deliberate local series rewrite leaves inactive, unlinked
                # old occurrences behind. Its existing linked chain is authoritative;
                # those archived leftovers must neither take over nor restore.
                linked_series = self.with_context(active_test=False).search([
                    ("caldav_uid", "=", uid), ("user_id", "=", user.id),
                    ("recurrence_id", "!=", False),
                ] + self._caldav_owned_event_domain(), limit=1)
                if linked_series:
                    detached = self.browse()
            if detached:
                same_uid = self.with_context(active_test=False).search([
                    ("caldav_uid", "=", uid), ("user_id", "=", user.id),
                ] + self._caldav_owned_event_domain())
                if any(event._caldav_preserves_local_event() for event in same_uid):
                    synced_events |= same_uid
                    continue
                linked = self.with_context(active_test=False).search_count([
                    ("caldav_uid", "=", uid), ("user_id", "=", user.id),
                    ("recurrence_id", "!=", False),
                ] + self._caldav_owned_event_domain())
                known_chain = self.env["calendar.recurrence"].with_context(active_test=False).search_count([
                    ("caldav_uid", "=", uid),
                ])
                if same_uid - detached or linked or known_chain:
                    raise CalDAVResourceReview(uid, same_uid.ids, "mixed")
                inactive = detached.filtered(lambda event: not event.active)
                if inactive:
                    inactive.with_context(
                        **CALDAV_IMPORT_CONTEXT, caldav_no_sync=True, caldav_keep_ids=True,
                        dont_notify=True, skip_attendee_notification=True,
                    ).write({"active": True})
                    _logger.warning(
                        "CalDAV restored user ID %s event IDs %s", user.id, sorted(inactive.ids),
                    )
                synced_events |= self._sync_detached_caldav_component(component, user, detached)
                continue
            restore_rows = (
                restore_rows_by_uid.get(uid, self.browse())
                if restore_rows_by_uid is not None else None
            )
            self._caldav_restore_remote_occurrences(component, user, restore_rows)
            existing_instance = self._get_existing_instance(uid, recurrence_id)
            # Ambiguous data must stop this resource, never choose an arbitrary
            # record or send a multi-record set to singleton comparison code.
            if existing_instance and any(
                event._caldav_preserves_local_event() for event in existing_instance
            ):
                synced_events |= existing_instance
                continue
            if len(existing_instance) > 1:
                raise ValueError("Ambiguous CalDAV occurrence")
            known_recurrence = self.env["calendar.recurrence"].with_context(
                active_test=False
            ).search([("caldav_uid", "=", uid)], limit=1)
            microsoft_events = self.env["calendar.event"].with_context(
                active_test=False
            ).search([("caldav_uid", "=", uid)]).filtered(
                lambda event: event._is_microsoft_owned()
            )
            recurrence_is_microsoft_owned = known_recurrence and (
                known_recurrence.microsoft_id
                or known_recurrence.ms_universal_event_id
                or known_recurrence.calendar_event_ids.filtered(
                    lambda event: event._is_microsoft_owned()
                )
            )
            if not existing_instance and (
                microsoft_events or recurrence_is_microsoft_owned
            ):
                _logger.warning(
                    "Ignoring CalDAV event %s because its UID belongs to "
                    "a Microsoft-owned event",
                    uid,
                )
                continue
            if not existing_instance and known_recurrence:
                # The recurrence UID is authoritative even when an older
                # record lost its base-event UID. Reuse the existing series so
                # this snapshot cannot create a duplicate recurrence.
                synced_events |= known_recurrence.calendar_event_ids
                _logger.warning(
                    "Reusing existing CalDAV recurrence %s instead of "
                    "creating a duplicate",
                    uid,
                )
                continue
            outdated = self._get_outdated(component, existing_instance, synced_events)
            owned = (
                existing_instance and existing_instance.partner_id == user.partner_id
            )
            # Pass for_creation=True only when creating a new event
            values = self._get_values_from_ical_component(
                component, user, for_creation=not existing_instance
            )
            recurrency_vals = self._get_recurrency_values_from_ical_event(component)
            if not existing_instance:
                # just scrap the recurrency vals, they're not useful
                if not recurrency_vals.get("follow_recurrence"):
                    recurrency_vals = {}

                new_event = self.with_context(
                    **CALDAV_IMPORT_CONTEXT,
                    caldav_no_sync=True,
                    caldav_keep_ids=True,
                ).create(values | recurrency_vals)
                self.env["calendar.event"].flush_model()
                created = new_event.recurrence_id.calendar_event_ids if new_event.recurrency else new_event
                self._caldav_record_created(user, created, origin_batch=origin_batch)
                synced_events |= created
                continue
            elif outdated or not owned:
                pass
            else:
                recurrence_changes = existing_instance._get_recurrence_changes(recurrency_vals)
                # Existing series edits can invoke Odoo's break/rewrite paths,
                # unlinking or replacing real occurrences outside cleanup caps.
                # A sync error is preferable. Individual exceptions may still
                # move safely through the explicit self_only path below.
                unsafe_changes = set(recurrence_changes) - {
                    "follow_recurrence", "recurrence_update", "until", "count",
                }
                if unsafe_changes or (
                    existing_instance.recurrence_id and not recurrence_id
                    and not component.get("rrule")
                ):
                    raise CalDAVResourceReview(uid, existing_instance.ids, "recurrence")
                if (
                    "count" in recurrence_changes
                    or ("until" in recurrence_changes and fields.Date.to_date(recurrency_vals.get("until"))
                        != fields.Date.to_date(existing_instance.until))
                ):
                    # Keep the original occurrence set. Report the retained
                    # bounds instead of silently ignoring a remote shortening
                    # or extension, without freezing other account resources.
                    self._caldav_review_notice(
                        user, "recurrence", uid,
                        hashlib.sha256(component.to_ical()).hexdigest(),
                        existing_instance.recurrence_id.calendar_event_ids.ids,
                    )
                changed_vals = existing_instance._get_value_changes(values)
                if component.get("recurrence-id") and "follow_recurrence" in recurrence_changes:
                    changed_vals["follow_recurrence"] = recurrence_changes["follow_recurrence"]
                if changed_vals:
                    existing_instance.with_context(
                        **CALDAV_IMPORT_CONTEXT,
                        caldav_no_sync=True,
                        caldav_keep_ids=True,
                    ).write(changed_vals)

            if existing_instance.recurrency and existing_instance.is_base_event:
                synced_events |= existing_instance.recurrence_id.calendar_event_ids
            else:
                synced_events |= existing_instance
        return synced_events

    @api.model
    def _caldav_restore_remote_occurrences(self, component, user, rows=None):
        """Restore exact remote positions, never regenerate a recurrence.

        Remote presence can recover rows hidden by an earlier sync. For a
        master, only positions present in RRULE/RDATE and absent from EXDATE are
        eligible. An unknown or ambiguous UID is left for the normal fail-closed
        importer. Source mirrors and Microsoft records retain their authority.
        """
        from dateutil.rrule import rrulestr

        uid = str(component.get("uid"))
        if rows is None:
            rows = self.with_context(active_test=False).search([
                ("caldav_uid", "=", uid), ("user_id", "=", user.id),
            ] + self._caldav_owned_event_domain())
        inactive = rows.filtered(
            lambda event: not event.active and not event._caldav_preserves_local_event()
            and not (event.recurrency and not event.recurrence_id)
        )
        if not inactive:
            return
        def utc_position(value):
            if isinstance(value, datetime):
                return value.astimezone(utc).replace(tzinfo=None) if value.tzinfo else value
            return datetime.combine(value, time.min)
        recurrence = component.get("recurrence-id")
        start = component.get("dtstart")
        if not start:
            raise CalDAVPartialSyncError("Remote CalDAV event lacks DTSTART")
        position = utc_position(recurrence.dt if recurrence else start.dt)
        rule = None
        if not recurrence and component.get("rrule"):
            rule_start = start.dt
            if not isinstance(rule_start, datetime):
                rule_start = datetime.combine(rule_start, time.min)
            # dateutil must receive a timezone with DST transitions; pytz's
            # localized DTSTART tzinfo has a fixed offset when rrule iterates.
            from dateutil.tz import gettz
            zone_name = getattr(rule_start.tzinfo, "zone", None)
            if zone_name:
                rule_start = rule_start.astimezone(gettz(zone_name))
            rule = rrulestr(component["rrule"].to_ical().decode(), dtstart=rule_start)
        def dates(name):
            properties = component.get(name, [])
            if not isinstance(properties, list):
                properties = [properties]
            return {utc_position(item.dt) for prop in properties for item in prop.dts}
        rdates, exdates = dates("rdate"), dates("exdate")
        restored = self.browse()
        for event in inactive:
            event_position = event.caldav_recurrence_id or event.start
            present = event_position == position or event_position in rdates
            if rule and event_position not in exdates:
                if rule._dtstart.tzinfo:
                    candidate = utc.localize(event_position).astimezone(rule._dtstart.tzinfo)
                    # _recompute_caldav_ids stores the initial UTC clock on
                    # each occurrence date, including after DST. Reconstruct
                    # that original wall-time position from the master's
                    # initial offset, rather than trusting the current start
                    # (which can itself use the legacy fixed UTC clock).
                    first_utc = rule._dtstart.astimezone(utc).replace(tzinfo=None)
                    legacy = rule._dtstart.replace(tzinfo=None) + (event_position - first_utc)
                    legacy = legacy.replace(tzinfo=rule._dtstart.tzinfo)
                    normalized = legacy.astimezone(utc).replace(tzinfo=None)
                    if normalized in exdates:
                        continue
                    present = present or rule.before(legacy, inc=True) == legacy
                else:
                    candidate = event_position
                present = present or rule.before(candidate, inc=True) == candidate
            if not (event.recurrency or event.recurrence_id):
                # A standalone UID may move remotely; require a single row.
                present = len(rows) == 1 and not recurrence and not rule
            if present and event_position not in exdates:
                restored |= event
        if restored:
            restored.with_context(
                **CALDAV_IMPORT_CONTEXT, caldav_no_sync=True, caldav_keep_ids=True,
                dont_notify=True, skip_attendee_notification=True,
            ).write({"active": True})
            for chain in restored.recurrence_id:
                active = chain.calendar_event_ids.filtered("active").sorted("start")
                # Archiving the old base promotes the next occurrence. When
                # the master reappears, put its original position back at the
                # base before importing master values; otherwise they would
                # move that promoted occurrence onto the first meeting.
                master = active.filtered(
                    lambda event: (event.caldav_recurrence_id or event.start) == position
                ) if not recurrence else self.browse()
                if len(master) > 1:
                    raise CalDAVPartialSyncError("Ambiguous restored CalDAV base")
                if master or not chain.base_event_id or not chain.base_event_id.active:
                    chain.with_context(**CALDAV_IMPORT_CONTEXT, caldav_no_sync=True).write({
                        "base_event_id": (master or active[:1]).id, "need_sync_m": False,
                    })
            _logger.warning("CalDAV restored user ID %s event IDs %s", user.id, sorted(restored.ids))

    @api.model
    def _sync_detached_caldav_component(self, component, user, detached):
        """Reconcile detached rows without recreating a recurrence or its IDs.

        RECURRENCE-ID identifies the original position even after DTSTART moves.
        A master can acknowledge the existing group, but may update only its
        exact DTSTART position. Never project a master's time onto every row.
        Unrepresented historical occurrences remain protected from cleanup.
        """
        position = component.get("recurrence-id") or component.get("dtstart")
        position = position.dt
        if isinstance(position, datetime):
            if position.tzinfo:
                position = position.astimezone(utc).replace(tzinfo=None)
        else:
            position = datetime.combine(position, time.min)
        matched = detached.filtered(lambda event: event.caldav_recurrence_id == position)
        alias_key = hashlib.sha256(json.dumps([
            self._caldav_account_identity(user), str(component.get("uid")), str(position),
        ]).encode()).hexdigest()
        state = None
        if not matched and component.get("recurrence-id"):
            state = self._caldav_import_safety_state(user)
            alias = (state or {}).get("positions", {}).get(alias_key)
            if isinstance(alias, dict):
                matched = detached.filtered(
                    lambda event: event.id == alias.get("id")
                    and hashlib.sha256(str(event.caldav_recurrence_id).encode()).hexdigest() == alias.get("stored")
                )
        if not matched:
            # Old _recompute_caldav_ids fixed the first occurrence's UTC clock
            # across DST. Actual starts retain the series timezone correctly.
            remote_position = component.get("recurrence-id") or component.get("dtstart")
            fallback = self.browse()
            for event in detached:
                zone = timezone(event.event_tz or user.tz or "UTC")
                original = remote_position.dt
                if isinstance(original, datetime):
                    local = (original.astimezone(zone) if original.tzinfo
                             else zone.localize(original, is_dst=None))
                    event_local = utc.localize(event.start).astimezone(zone)
                    if event_local.replace(tzinfo=None) == local.replace(tzinfo=None):
                        fallback |= event
                elif utc.localize(event.start).astimezone(zone).date() == original:
                    fallback |= event
            matched = fallback
            if len(matched) == 1 and component.get("recurrence-id"):
                # Remember only an unambiguous observed mapping. DTSTART may
                # move again, while both incoming and legacy recurrence IDs
                # must stay stable. Account/UID/positions are hashed; a local
                # identity/position change invalidates this mapping.
                if state is not None:
                    state.setdefault("positions", {})[alias_key] = {
                        "id": matched.id,
                        "stored": hashlib.sha256(str(matched.caldav_recurrence_id).encode()).hexdigest(),
                    }
                    self._caldav_save_safety_state(user, state)
        if len(matched) > 1 or (not matched and component.get("recurrence-id")):
            raise ValueError("Ambiguous or unknown detached CalDAV recurrence position")
        if matched and not matched._caldav_preserves_local_event() and (
            matched.partner_id == user.partner_id
            and not self._get_outdated(component, matched, self.browse())
        ):
            values = self._get_values_from_ical_component(component, user)
            # Preserve attendance and source identity during this conservative
            # repair. Never write recurrence values: they can replace rows.
            values.pop("partner_ids", None)
            changed = matched._get_value_changes(values)
            if changed:
                matched.with_context(
                    **CALDAV_IMPORT_CONTEXT, caldav_no_sync=True,
                    caldav_keep_ids=True,
                ).write({**changed, "recurrence_update": "self_only"})
        return detached

    @api.model
    def _get_existing_instance(self, uid, recurrence_id: Optional[datetime]):
        """Find the Odoo calendar.event record matching uid and,
        if set, recurrence_id.
        """
        owned_domain = self._caldav_owned_event_domain()
        if recurrence_id:
            if isinstance(recurrence_id, datetime):
                if recurrence_id.tzinfo:
                    recurrence_id = recurrence_id.astimezone(utc).replace(
                        tzinfo=None
                    )
                else:
                    recurrence_id = utc.localize(recurrence_id).replace(
                        tzinfo=None
                    )
            else:
                recurrence_id = datetime.combine(recurrence_id, time.min)
            instance = self.env["calendar.event"].search(
                [
                    ("caldav_uid", "=", uid),
                    ("caldav_recurrence_id", "=", recurrence_id),
                ] + owned_domain
            )
        else:
            instance = self.env["calendar.event"].search(
                [
                    ("caldav_uid", "=", uid),
                    ("recurrency", "=", False),
                ] + owned_domain
            )
            if instance:
                return instance
            else:
                recurrence = self.env["calendar.recurrence"].search(
                    [("base_event_id.caldav_uid", "=", uid)], limit=1
                )
                if not recurrence:
                    recurrence = self.env["calendar.recurrence"].search(
                        [("caldav_uid", "=", uid)], limit=1
                    )
                if (
                    recurrence
                    and recurrence.base_event_id
                    and not recurrence.microsoft_id
                    and not recurrence.ms_universal_event_id
                    and not recurrence.calendar_event_ids.filtered(
                        lambda event: event._is_microsoft_owned()
                    )
                ):
                    return recurrence.base_event_id

        if len(instance) == 1:
            return instance
        if len(instance) > 1:
            instance = instance.recurrence_id.base_event_id

        return instance or self.env["calendar.event"].search(
            [
                ("caldav_uid", "=", uid),
                ("recurrence_id", "=", False),
            ] + owned_domain
        )

    @api.model
    def _get_recurrency_values_from_ical_event(
        self, component: icalendar.cal.Component
    ) -> Dict:
        """Match the fields from calendar.event (recurring fields) to the fields specified in RRULE at
        https://icalendar.org/iCalendar-RFC-5545/3-8-5-3-recurrence-rule.html"""
        rrule = [item[1] for item in component.property_items() if item[0] == "RRULE"]
        rrule = rrule[0] if rrule else None

        if component.get("recurrence-id"):
            # When a component has "recurrence-id" set, it is a single event
            # in a series of events. Recurrence-id is the time the event should
            # normally occur at if it follows the recurrence of the series.
            follows_recurrence = (
                component.get("recurrence-id").dt == component.get("dtstart").dt
            )
            if not follows_recurrence:
                return {
                    "follow_recurrence": False,
                    "recurrence_update": "self_only",
                }
            if not rrule:
                # This event is following the base event's recurrence rule
                return {
                    "follow_recurrence": True,
                    "recurrence_update": "self_only",
                }

        if not rrule or not isinstance(rrule, vRecur):
            return {}
        rrule_str = rrule.to_ical() and rrule.to_ical().decode("utf-8")
        if rrule_str:
            recurrence_start = component.get("dtstart").dt
            until = rrule.get("until")
            until = until[0] if isinstance(until, list) else until
            date_only_until = isinstance(until, date) and not isinstance(
                until, datetime
            )
            if date_only_until:
                recurrence_start = datetime.combine(
                    recurrence_start.date()
                    if isinstance(recurrence_start, datetime)
                    else recurrence_start,
                    time.min,
                )
            elif isinstance(recurrence_start, date) and not isinstance(
                recurrence_start, datetime
            ):
                recurrence_start = datetime.combine(recurrence_start, time.min)
            if not date_only_until and recurrence_start.tzinfo is None:
                recurrence_start = utc.localize(recurrence_start)
            elif not date_only_until:
                recurrence_start = recurrence_start.astimezone(utc)
            rrule_params = self.env["calendar.recurrence"]._rrule_parse(
                "RRULE:" + rrule_str, recurrence_start
            )
        else:
            _logger.warning(f"Could not convert RRULE to string: {rrule}")
            return {}

        vals = {
            "recurrency": True,
            "follow_recurrence": True,
            "recurrence_update": "future_events",
            **rrule_params,
        }
        # Convert None to False since fields from Odoo that are not filled come back False
        vals = {
            key: value if value is not None else False for key, value in vals.items()
        }

        # Forever doesn't exist in Odoo. The calendar.recurrence model changes 'forever'
        # into 'count' with MAX_RECURRENT_EVENT as the 'count' parameter
        if vals.get("end_type") == "forever":
            vals.update(end_type="count")
            if not vals.get("count"):
                vals.update(count=MAX_RECURRENT_EVENT)
        until = vals.get("until")
        if until and (isinstance(until, vDatetime) or isinstance(until, vDate)):
            until_day = until.dt if isinstance(until, vDatetime) else until.dt
            vals.update(until=until_day)
            vals.pop("count", None)
        return vals

    def _get_recurrence_changes(self, recurrency_vals: Dict) -> Dict:
        """Compare a set of recurrency values with those already present on
        this event.

        :param recurrency_vals: The recurrency values from an iCalendar event
        to compare to self.

        :return: A dictionary containing only the values that are different
        from those in self."""
        if not recurrency_vals and not self.recurrence_id:
            return {}
        if not recurrency_vals and self.recurrence_id:
            return {"recurrence_update": "all_events", "recurrency": False}
        if recurrency_vals and not self.recurrence_id:
            return recurrency_vals
        changed_fields = {
            key: recurrency_vals[key]
            for key in recurrency_vals.keys()
            if hasattr(self, key) and recurrency_vals[key] != getattr(self, key)
        }
        if len(changed_fields) == 1 and "recurrence_update" in changed_fields:
            return {}
        return changed_fields

    def _get_value_changes(self, values: Dict) -> Dict:
        """Compare the values from an iCalendar event to those already
        present on self.

        :param values: The values from an iCalendar event
        :return: A dictionary containing only the values that are different
        from those in self."""
        changed_vals = {}
        # Don't update partner_ids if no change
        if "partner_ids" in values:
            partner_ids = values["partner_ids"][0][2]  # this is a SET command
            # Booking clients are left out of the outgoing attendee list, so the
            # server never returns them; their absence is not a removal.
            kept = [
                id for id in self._caldav_booking_client_partners().ids
                if id not in partner_ids
            ]
            if kept:
                partner_ids = list(partner_ids) + kept
                values["partner_ids"] = [(6, 0, partner_ids)]
            added_partner_ids = set(
                [id for id in partner_ids if id not in self.partner_ids.ids]
            )
            removed_partner_ids = set(
                [id for id in self.partner_ids.ids if id not in partner_ids]
            )
            if not (added_partner_ids or removed_partner_ids):
                values.pop("partner_ids")  # They break the equality check later
            else:
                changed_vals["partner_ids"] = values["partner_ids"]

        # A booking's description is never sent, so the server's (empty) one is
        # not a change: the note kept in Odoo must survive the next poll.
        if "description" in values and self._caldav_is_appointment_booking():
            values.pop("description")

        # Get just the list of values that have changed, leave the others alone
        for key, val in values.items():
            curr_val = getattr(self, key)
            # Can't deal with x2many fields, need ID from a record
            if isinstance(val, list):
                continue
            if curr_val and isinstance(curr_val, models.Model):
                if len(curr_val) > 1:
                    continue
                curr_val = curr_val.id
            if curr_val != val:
                changed_vals.update({key: val})
        return changed_vals

    @api.model
    def _get_outdated(
        self,
        component: icalendar.cal.Component,
        existing_instance: OdooCalendarEvent,
        synced_events: OdooCalendarEvent,
    ) -> bool:
        """Check whether a component from the CalDAV server (typically an
        event) is outdated when compared to its existing Odoo calendar.event
        instance.

        :param component: The iCalendar component to check.
        :param existing_instance: The calendar.event record to
        compare with.
        :param synced_events: The calendar.event record(s) that have already
        been synchronized.
        :return: True if the iCalendar component is outdated, False otherwise.
        """
        outdated = False
        last_modified = component.get("dtstamp") and component.get("dtstamp").dt
        if (
            existing_instance
            and last_modified
            and isinstance(last_modified, datetime)
            and existing_instance not in synced_events
        ):
            if last_modified.tzinfo is None:
                last_modified = utc.localize(last_modified)
            else:
                last_modified = last_modified.astimezone(utc)
            existing_write_date = existing_instance.write_date
            if existing_write_date.tzinfo is None:
                existing_write_date = utc.localize(existing_write_date)
            else:
                existing_write_date = existing_write_date.astimezone(utc)
            if last_modified < existing_write_date:
                outdated = True
        return outdated

    @api.model
    def _get_values_from_ical_component(
        self, component: icalendar.cal.Component, user: User, for_creation: bool = False
    ) -> Dict:
        """Get the dictionary representing calendar.event field values from
        an iCalendar event.

        :param component: The iCalendar component from which to extract values.
        :param user: The res.users record the event will belong to.
        :param for_creation: Whether these values are for creating a new event (True)
                            or updating an existing one (False).
        :return: The dictionary of values to construct a calendar.event."""
        start = component.get("dtstart") and component.decoded("dtstart")
        if isinstance(start, datetime):
            start = start.astimezone(utc).replace(tzinfo=None)
        end = component.get("dtend") and component.decoded("dtend")
        if isinstance(end, datetime):
            end = end.astimezone(utc).replace(tzinfo=None)

        # Get attendees regardless of creation/update
        attendee_ids = self._get_attendee_partners(component, user.partner_id.email)

        # Basic values that apply to both creation and updates
        values = {
            "name": str(component.get("summary")),
            "start": start,
            "stop": end,
            "description": self._text_to_html(
                self._extract_component_text(component, "description")
            ),
            "location": self._extract_component_text(component, "location"),
            "videocall_location": self._extract_component_text(component, "conference"),
            "caldav_uid": str(component.get("uid")),
            "partner_ids": [(6, 0, attendee_ids.ids)],
        }

        # Only set user_id and partner_id during creation
        if for_creation:
            organizer_partner = self._get_organizer_partner(component)
            if organizer_partner:
                # Get the Odoo user ID associated with the organizer partner
                organizer = (
                    organizer_partner.user_ids[0].id
                    if organizer_partner.user_ids
                    else False
                )
                values.update(
                    {
                        "partner_id": organizer_partner.id,
                        "user_id": organizer,
                    }
                )
            else:
                # For new events without an organizer, use the current user
                values.update(
                    {
                        "partner_id": user.partner_id.id,
                        "user_id": user.id,
                    }
                )

        return values

    @api.model
    def _get_attendee_partners(
        self, component: icalendar.cal.Component, current_user_email: str
    ) -> Partner:
        """Get the res.partner records who are attendees for a given
        iCalendar event.

        :param component: The iCalendar component from which to extract
        attendees.
        :param current_user_email: The email for the res.users record the
        matching Odoo event belongs to.
        :return: The res.partner records who are attendees for the event."""
        attendee_emails = self._get_ical_attendee_emails(component)
        # Add organizer to attendees if present
        organizer = component.get("organizer")
        if organizer:
            organizer_email = _extract_vcal_email(organizer)
            if organizer_email not in attendee_emails:
                attendee_emails.append(organizer_email)
        # Add current user if not already in attendees
        if current_user_email not in attendee_emails:
            attendee_emails.append(current_user_email)
        # An attendee or organizer without a parsable address yields "", and
        # ("email", "in", [""]) also matches every contact without an email
        # (mostly Socio clients), attaching a random one to staff events.
        attendee_emails = [email for email in attendee_emails if email]
        existing_partners = self.env["res.partner"].search(
            [("email", "in", attendee_emails)]
        ) if attendee_emails else self.env["res.partner"]
        final_attendees = {}
        # CalDAV invitees are external data.  Only link contacts that already
        # exist in Odoo; importing an event must never turn every attendee or
        # organizer address into a persistent CRM contact.
        all_partners = existing_partners
        # We do this because partners may have identical emails and we only want one
        # attending partner per email. Otherwise invitations get sent out multiple times
        # and nobody likes that.
        # Prioritize users as attendees
        for partner in all_partners.filtered(lambda partner: bool(partner.user_id)):
            if partner.email not in final_attendees:
                final_attendees[partner.email] = partner.id

        for partner in all_partners.filtered(lambda partner: not partner.user_id):
            if partner.email not in final_attendees:
                final_attendees[partner.email] = partner.id

        return all_partners.filtered(
            lambda partner: partner.id in final_attendees.values()
        )

    @api.model
    def _get_organizer_partner(self, component: icalendar.cal.Component) -> Partner:
        """Get the partner matching the organizer on an iCalendar event.

        :param component: The iCalendar component from which to extract the
        organizer.
        :return: The res.partner record matching the event organizer."""

        organizer = component.get("organizer")
        if organizer:
            email = _extract_vcal_email(organizer)
            _logger.info("Organizer email: %s", email)
            if not email:
                return self.env["res.partner"]
            partner = self.env["res.partner"].search([("email", "=", email)], limit=1)
            _logger.info("Found partner: %s", partner)
            return partner
        else:
            return self.env["res.partner"]

    @api.model
    def _get_ical_attendee_emails(
        self, component: icalendar.cal.Component
    ) -> List[str]:
        """Get the email addresses for attendees from an iCalendar event.

        :param component: The iCalendar event from which to extract emails.
        :return: A list of attendee emails
        """
        attendees = component.get("attendee", [])
        if not isinstance(attendees, list):
            attendees = [attendees]
        attendee_emails = [_extract_vcal_email(attendee) for attendee in attendees]
        return attendee_emails

    @api.model
    def _extract_component_text(
        self, component: icalendar.cal.Component, subcomponent_name: str
    ) -> str:
        """Extract the text from an iCalendar subcomponent. Convenience
        method to deal with empty subcomponents.

        :param component: The iCalendar component from which to extract text.
        :param subcomponent_name: The name of the subcomponent to extract.
        :return: The extracted subcomponent text or an empty string.
        """
        val = component.get(subcomponent_name)
        text = str(val) if val else ""
        return text

    @api.model
    def _text_to_html(self, text):
        return md2.markdown(text)
