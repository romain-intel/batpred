# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for generic VPP event handling.

A VPP dispatch means the programme operator is driving the battery for the duration. Predbat's job
is to notice and stand down; the failure that matters is standing down at the wrong time, either
missing an event (and fighting the operator) or standing down when there is none (and giving up
control of a normal day). Most of these tests are therefore about the boundaries of the window and
about timezone handling, which is where a hand-maintained calendar goes wrong.
"""

from datetime import timedelta

from vpp import fetch_vpp_event, fetch_vpp_active


class FakeBase:
    """Minimal stand-in exposing only what vpp.py reads."""

    def __init__(self, my_predbat, calendar=None, active=None):
        """Wire the fake to the real timezone and clock so parsing is exercised for real."""
        self.local_tz = my_predbat.local_tz
        self.now_utc = my_predbat.now_utc
        self.args = {}
        self.states = {}
        self.logs = []
        if calendar:
            self.args["vpp_calendar"] = calendar
        if active:
            self.args["vpp_active"] = active

    def get_arg(self, name, default=None, indirect=True):
        """Return an apps.yaml style argument."""
        return self.args.get(name, default)

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, **kwargs):
        """Return entity state or one of its attributes."""
        entity = self.states.get(entity_id)
        if entity is None:
            return default
        if attribute:
            return entity.get(attribute, default)
        return entity.get("state", default)

    def log(self, message, **kwargs):
        """Capture warnings so tests can assert on them."""
        self.logs.append(message)

    def set_calendar(self, entity_id, state, start=None, end=None, message=""):
        """Publish a calendar entity with the given state and window."""
        self.states[entity_id] = {"state": state, "start_time": start, "end_time": end, "message": message}


def local_str(my_predbat, offset_minutes):
    """A naive local-time string offset from now, as Home Assistant writes calendar times."""
    local = (my_predbat.now_utc + timedelta(minutes=offset_minutes)).astimezone(my_predbat.local_tz)
    return local.strftime("%Y-%m-%d %H:%M:%S")


def test_vpp_no_config(my_predbat):
    """With nothing configured there is never an event, and nothing is read."""
    print("  - test_vpp_no_config")
    failed = False
    base = FakeBase(my_predbat)
    event = fetch_vpp_event(base)
    if event["active"] or event["start"] is not None:
        print("ERROR: unconfigured VPP should report no event, got {}".format(event))
        failed = True
    if fetch_vpp_active(base):
        print("ERROR: unconfigured VPP should not report active")
        failed = True
    return failed


def test_vpp_calendar_window(my_predbat):
    """An event running now is active; one in the future is not, but its window is reported."""
    print("  - test_vpp_calendar_window")
    failed = False
    cal = "calendar.vpp"

    # Running now: started 30 minutes ago, ends in 30
    base = FakeBase(my_predbat, calendar=cal)
    base.set_calendar(cal, "on", local_str(my_predbat, -30), local_str(my_predbat, 30), "PG&E event")
    event = fetch_vpp_event(base)
    if not event["active"]:
        print("ERROR: an event spanning now should be active, got {}".format(event))
        failed = True
    if event["message"] != "PG&E event":
        print("ERROR: expected the event message to be carried through, got {}".format(event["message"]))
        failed = True
    if event["minutes_to_end"] is None or abs(event["minutes_to_end"] - 30) > 2:
        print("ERROR: expected ~30 minutes to end, got {}".format(event["minutes_to_end"]))
        failed = True
    if event["minutes_to_start"] is None or abs(event["minutes_to_start"] + 30) > 2:
        print("ERROR: expected ~-30 minutes to start, got {}".format(event["minutes_to_start"]))
        failed = True

    # Announced for later today: not active, but the window is still visible so a caller can plan
    base = FakeBase(my_predbat, calendar=cal)
    base.set_calendar(cal, "off", local_str(my_predbat, 120), local_str(my_predbat, 240), "Later")
    event = fetch_vpp_event(base)
    if event["active"]:
        print("ERROR: a future event must not be active")
        failed = True
    if event["minutes_to_start"] is None or abs(event["minutes_to_start"] - 120) > 2:
        print("ERROR: expected ~120 minutes to start, got {}".format(event["minutes_to_start"]))
        failed = True

    # Finished: neither active nor claimed to be
    base = FakeBase(my_predbat, calendar=cal)
    base.set_calendar(cal, "off", local_str(my_predbat, -240), local_str(my_predbat, -120), "Done")
    if fetch_vpp_event(base)["active"]:
        print("ERROR: a past event must not be active")
        failed = True
    return failed


def test_vpp_calendar_state_wins(my_predbat):
    """The calendar's own state marks an event active even when the window does not say so.

    An all-day entry has a window that bears no relation to the dispatch hours, so the entity state
    is the more reliable of the two. Trusting only the arithmetic would silently miss those.
    """
    print("  - test_vpp_calendar_state_wins")
    failed = False
    cal = "calendar.vpp"
    base = FakeBase(my_predbat, calendar=cal)
    base.set_calendar(cal, "on", local_str(my_predbat, 120), local_str(my_predbat, 240), "All day")
    if not fetch_vpp_event(base)["active"]:
        print("ERROR: calendar state 'on' should mark the event active regardless of the window")
        failed = True
    return failed


def test_vpp_live_signal(my_predbat):
    """A live signal alone is enough to stand down, with no calendar at all."""
    print("  - test_vpp_live_signal")
    failed = False
    sig = "binary_sensor.grid_services_active"

    base = FakeBase(my_predbat, active=sig)
    base.states[sig] = {"state": "on"}
    event = fetch_vpp_event(base)
    if not event["active"]:
        print("ERROR: a live signal of 'on' should be active")
        failed = True
    if event["start"] is not None:
        print("ERROR: with no calendar there is no window to report, got {}".format(event["start"]))
        failed = True

    base.states[sig] = {"state": "off"}
    if fetch_vpp_active(base):
        print("ERROR: a live signal of 'off' should not be active")
        failed = True

    # The live signal must also be able to override a calendar that says nothing is running - the
    # programme's own view beats a hand-maintained transcription of it
    cal = "calendar.vpp"
    base = FakeBase(my_predbat, calendar=cal, active=sig)
    base.set_calendar(cal, "off", local_str(my_predbat, 120), local_str(my_predbat, 240), "Later")
    base.states[sig] = {"state": "on"}
    if not fetch_vpp_active(base):
        print("ERROR: a live 'on' signal must win over a calendar showing no current event")
        failed = True
    return failed


def test_vpp_bad_calendar_data(my_predbat):
    """Unreadable or missing times degrade to 'no window', never to a crash or a false active."""
    print("  - test_vpp_bad_calendar_data")
    failed = False
    cal = "calendar.vpp"

    for start, end in ((None, None), ("not a time", "also not"), ("", "")):
        base = FakeBase(my_predbat, calendar=cal)
        base.set_calendar(cal, "off", start, end, "Bad")
        try:
            event = fetch_vpp_event(base)
        except Exception as e:
            print("ERROR: bad calendar times {}/{} raised {}".format(start, end, e))
            return True
        if event["active"] or event["start"] is not None:
            print("ERROR: bad calendar times {}/{} should yield no window, got {}".format(start, end, event))
            failed = True

    # A missing entity entirely
    base = FakeBase(my_predbat, calendar="calendar.does_not_exist")
    if fetch_vpp_active(base):
        print("ERROR: a missing calendar entity should not report active")
        failed = True
    return failed


def test_vpp_timezone_handling(my_predbat):
    """Naive local times are localised, not misread as UTC.

    This is the failure that would actually bite: Home Assistant writes calendar times as naive local
    strings, and treating them as UTC would shift every event by the site's offset - standing Predbat
    down hours early or late. Pinned with an explicit offset that is not zero for most of the world.
    """
    print("  - test_vpp_timezone_handling")
    failed = False
    cal = "calendar.vpp"
    base = FakeBase(my_predbat, calendar=cal)
    # 30 minutes in, 30 to go, expressed in local wall-clock exactly as HA would write it
    base.set_calendar(cal, "off", local_str(my_predbat, -30), local_str(my_predbat, 30), "TZ check")
    event = fetch_vpp_event(base)
    if not event["active"]:
        print("ERROR: naive local times were not localised - event should be active, got {}".format(event))
        failed = True

    # An ISO string carrying its own offset must also work, since some integrations write those
    base = FakeBase(my_predbat, calendar=cal)
    start_iso = (my_predbat.now_utc - timedelta(minutes=30)).isoformat()
    end_iso = (my_predbat.now_utc + timedelta(minutes=30)).isoformat()
    base.set_calendar(cal, "off", start_iso, end_iso, "ISO")
    if not fetch_vpp_event(base)["active"]:
        print("ERROR: offset-aware ISO times should also be understood")
        failed = True
    return failed


def run_vpp_tests(my_predbat):
    """Run every VPP event test."""
    print("**** Running VPP event tests ****\n")
    failed = test_vpp_no_config(my_predbat)
    failed |= test_vpp_calendar_window(my_predbat)
    failed |= test_vpp_calendar_state_wins(my_predbat)
    failed |= test_vpp_live_signal(my_predbat)
    failed |= test_vpp_bad_calendar_data(my_predbat)
    failed |= test_vpp_timezone_handling(my_predbat)
    return failed
