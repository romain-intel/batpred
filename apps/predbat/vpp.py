# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Generic Virtual Power Plant (VPP) event handling.

Utilities and grid operators run demand-response programmes where, for a announced window, they
take over the battery and dispatch it themselves - PG&E in California via Tesla, and many others.
Predbat already has one of these wired in (Axle), but that integration is specific to Axle's API.
This module covers the general case where the only thing available is a schedule, because the
programme has no API a home user can poll.

Two sources are supported and either may be used alone:

- vpp_calendar - a Home Assistant calendar entity holding the event windows. Calendar entities carry
  the current-or-next event in their attributes (start_time/end_time/message) and read "on" while an
  event is running, which is all that is needed here. Anyone can populate one by hand from the emails
  the programme sends, which is the point: no API required.
- vpp_active - a live "an event is running right now" signal, for programmes that expose one. Tesla's
  grid_services_active is the example this was written against. It gives no advance notice, so on its
  own it can only be reacted to, never planned for.

While an event is active Predbat stops writing to the inverter entirely (read-only), because during
a dispatch the operator owns the battery and any command Predbat sends is either overridden or fights
it. This mirrors what fetch_config_options() already does for Axle.
"""

from datetime import datetime

from utils import str2time


def _event_window(base, entity_id):
    """Read the current-or-next event window out of a calendar entity's attributes.

    Home Assistant publishes start_time/end_time on the calendar entity itself, so no service call or
    extra API surface is needed. Only one event is visible this way - the one running or the one
    coming next - which is enough to know whether to stand down now and when the next stand-down is
    due, but not enough to plan around a whole week of events.

    Returns:
    - tuple: (start datetime, end datetime, message) or (None, None, None) if unreadable
    """
    start_raw = base.get_state_wrapper(entity_id=entity_id, attribute="start_time", default=None)
    end_raw = base.get_state_wrapper(entity_id=entity_id, attribute="end_time", default=None)
    message = base.get_state_wrapper(entity_id=entity_id, attribute="message", default="")
    if not start_raw or not end_raw:
        return None, None, None
    start = _parse_event_time(base, start_raw)
    end = _parse_event_time(base, end_raw)
    if start is None or end is None:
        base.log("Warn: VPP calendar {} has unparsable event times {} - {}".format(entity_id, start_raw, end_raw))
        return None, None, None
    return start, end, message


def _parse_event_time(base, raw):
    """Parse a calendar timestamp, which may or may not carry a timezone.

    Home Assistant writes calendar start_time/end_time as naive local time ("2026-08-22 17:00:00")
    for timed events and as a bare date for all-day ones, while some integrations write a full ISO
    offset instead. str2time only accepts offset-aware strings and raises on the rest, so the naive
    forms are localised here against the configured timezone rather than being rejected - getting
    this wrong by a timezone would stand Predbat down at the wrong hour.

    Returns:
    - datetime: timezone-aware, or None if nothing could be made of it
    """
    text = str(raw).strip()
    if not text:
        return None
    try:
        return str2time(text)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return base.local_tz.localize(naive) if hasattr(base.local_tz, "localize") else naive.replace(tzinfo=base.local_tz)
    return None


def fetch_vpp_event(base):
    """Work out the VPP state right now, from whichever sources are configured.

    The live signal wins on "is an event running": it is the programme's own view, whereas a calendar
    is a human transcription that can be wrong about the exact minute. The calendar is what supplies
    the window, since a live flag says nothing about when the event ends.

    Returns:
    - dict: {"active", "start", "end", "message", "minutes_to_start", "minutes_to_end"}, where the
      window fields are None when no calendar is configured or it holds nothing readable
    """
    result = {"active": False, "start": None, "end": None, "message": "", "minutes_to_start": None, "minutes_to_end": None}

    calendar_entity = base.get_arg("vpp_calendar", indirect=False)
    active_entity = base.get_arg("vpp_active", indirect=False)
    if not calendar_entity and not active_entity:
        return result

    if calendar_entity:
        start, end, message = _event_window(base, calendar_entity)
        if start and end:
            result["start"] = start
            result["end"] = end
            result["message"] = message or ""
            now = base.now_utc
            # Both are reported relative to now and may be negative, which is how a caller tells an
            # event that has started from one that has not
            result["minutes_to_start"] = int((start - now).total_seconds() / 60)
            result["minutes_to_end"] = int((end - now).total_seconds() / 60)
            if start <= now < end:
                result["active"] = True
        # The entity's own state is the calendar's answer to "is an event on", so trust it over the
        # arithmetic above when the two disagree - an all-day event, for instance, has a window that
        # does not line up with the state at all
        if str(base.get_state_wrapper(entity_id=calendar_entity, default="off")).lower() == "on":
            result["active"] = True

    if active_entity:
        if str(base.get_state_wrapper(entity_id=active_entity, default="off")).lower() == "on":
            result["active"] = True

    return result


def fetch_vpp_active(base):
    """Is a VPP event running right now?

    Returns:
    - bool: True when any configured source reports an active event
    """
    return fetch_vpp_event(base)["active"]
