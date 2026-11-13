CalDAV Synchronization
======================

Bemade Inc.

Copyright (C) 2023-June Bemade Inc. (https://www.bemade.org).
Author: Marc Durepos (Contact : marc@bemade.org)

This program is under the terms of the GNU Lesser General Public License (LGPL-3)
For details, visit https://www.gnu.org/licenses/lgpl-3.0.en.html

Overview
--------

The CalDAV Synchronization module for Odoo allows users to synchronize their
calendar events with CalDAV servers. This enables seamless integration of Odoo
calendar with external applications like Apple Calendar or Thunderbird.

Features
--------

- Synchronize Odoo calendar events with CalDAV servers.
- Create, update, and delete events in Odoo and reflect changes on the CalDAV
  server.
- Poll CalDAV server for changes and update Odoo calendar accordingly.

Configuration
-------------

1. Install the module in Odoo.
2. Go to the User settings in Odoo.
3. Enter the CalDAV calendar URL, username, and password on the user settings.

Usage
-----

1. Create a calendar event in Odoo and it will be synchronized with the CalDAV
   calendar.
2. Update the event in Odoo and the changes will reflect on the CalDAV server.
3. Delete the event in Odoo and it will be removed from the CalDAV server.
4. Changes made to the calendar on the CalDAV server (other email apps) will be
   polled and updated in Odoo.

Technical Details
-----------------

* The module extends the `calendar.event` model to add CalDAV synchronization
  functionality.
* It uses the `icalendar` library to format events and the `caldav` library to
  interact with CalDAV servers.
* Polling for changes on the CalDAV server can be triggered manually by
  triggering the scheduled action in Odoo.

Change Log
----------

0.8.0
^^^^^

* Disable sending of notification emails when events are created or updated
  in Odoo during a CalDAV server synchronization.
* General code cleanup with improved type hints.

0.7.0
^^^^^

* Stopped the import of past events when synchronizing from the CalDAV server.
  This should help with performance, timeouts and avoid importing events that
  are not relevant to the user.

0.6.0
^^^^^

* Fixed an issue where synchronizing events created duplicate events on every sync.
* Completely revamped and synchronization of recurring events in both directions.

  * Making a recurring event in Odoo correctly creates the recurring event on the server.
  * Modifying the base event of a recurrence with "all events" or "future events" in
    Odoo reflects correctly on the server.
  * Modifying a non-base event correctly updates on the server in all 3 modes (this
    event only, all events, future events).
  * Modifying a base recurring event on the CalDAV server correctly updates the events
    on Odoo after a synchronization.
  * Deleting a whole recurring sequence from Odoo correctly deletes the sequence from
    the CalDAV server.
  * Deleting a single event or a whole recurring sequence on the CalDAV server
    correctly synchronizes to Odoo after a synchronization.

* CalDAV (iCalendar) UIDs are now correctly shared among events of a same recurrence in
  Odoo. This corrects a number of issues around updating and deleting events from both
  the Odoo and CalDAV server side.

Issues & Requests
-----------------

Please submit issues on Bemade's Gitlab at https://git.bemade.org/bemade/bemade-addons
or via our website at https://www.bemade.org.

License
-------

This program is under the terms of the GNU Lesser General Public License (LGPL-3)
For details, visit https://www.gnu.org/licenses/lgpl-3.0.en.html

TENENET safety patch, 2026-11-13
------------------------------

This customer patch lives in the separate ``third_party`` submodule. The
parent must pin its reviewed commit. Activation requires ``-u caldav_sync``
for the admin server action and the normal service restart; no new fields,
tables or TENENET addon upgrade. The poll cache stays at version 2.

Pure detached UID groups match singleton occurrences by normalized UTC
RECURRENCE-ID, then an unambiguous start in the series timezone. A persisted
hashed alias keeps that fallback stable after DTSTART moves again. IDs,
organizer, attendees and stored recurrence positions stay intact. Mixed
linked/plain plus detached UIDs fail closed and prevent that user's cleanup.
TENENET mirrors are checked before ambiguity handling.

Incomplete imports report partial health and cannot clean up. Cleanup retains
LIVE cb7707a candidate/remote-confirmation semantics: a missing linked base
expands its whole active chain only after UID lookup returns NotFound. All
selected occurrences are archived together, never unlinked. Reappearing remote
positions reactivate their original IDs and restore the recurrence base.
``caldav_no_sync`` prevents outbound deletion on the archive path.

The deliberate fewer-removals exceptions are source/Microsoft protection,
proven sync origin, and refusal of destructive recurrence rewrites. Legacy
rows without importer creation proof stay visible and log WARNING with skipped
IDs; organizer ownership or cache membership is not proof of sync origin.
An event imported by B cannot be cleaned up by organizer A's poll.

The persisted rolling 24-hour breaker counts UID resources, not occurrence
rows. Defaults are minimum small batch 5, minimum daily allowance 20, ratio
0.10 of resource population. Parameters are ``caldav_sync.cleanup_small_batch``,
``caldav_sync.cleanup_24h_min`` and ``caldav_sync.cleanup_24h_ratio``. Exceeding
the cumulative allowance archives nothing for that user, logs WARNING and
leaves an administrator TODO. Settings administrators may approve exactly the
pending set once through Users > Action > Schváliť odstránenie; account, IDs,
UIDs and row versions are checked again on the next complete poll. Restore
never refunds the budget. Invalid ledger/settings fail closed.

The separately committed ``caldav_sync.stop_requested=true`` flag stops a
running poll at its next checkpoint and rolls back its current account. Prior
account commits remain; the release runbook restores explicit logged IDs.

Regression suites cover the six detached shapes, repeated DST overrides,
normal recurrence, small deletions, confirmed whole-series deletions and false
omissions over multiple polls, exact-set admin approval, cumulative caps,
origin protection and TENENET source-owned mirrors. The pre-existing local
"this and following" RecursionError remains a separate ticket.
