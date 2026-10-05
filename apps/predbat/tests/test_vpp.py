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

from vpp import fetch_vpp_active, fetch_vpp_event, load_vpp_slot


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


def test_vpp_window_beats_calendar_state(my_predbat):
    """A readable window decides, even when the entity state disagrees.

    Some calendar integrations read "on" for the event that is merely next due, not one running now.
    Believing that stood Predbat down a full day before a dispatch. When the window is readable it is
    the better evidence, so it wins; the state is only consulted when there is no window at all.
    """
    print("  - test_vpp_window_beats_calendar_state")
    failed = False
    cal = "calendar.vpp"

    # State says on, but the event is tomorrow - must NOT be active
    base = FakeBase(my_predbat, calendar=cal)
    base.set_calendar(cal, "on", local_str(my_predbat, 1440), local_str(my_predbat, 1620), "Tomorrow 5pm")
    event = fetch_vpp_event(base)
    if event["active"]:
        print("ERROR: an event a day out must not be active just because the entity reads on")
        failed = True
    if event["minutes_to_start"] is None or abs(event["minutes_to_start"] - 1440) > 2:
        print("ERROR: the window should still be reported, got {}".format(event["minutes_to_start"]))
        failed = True

    # State says off but the window contains now - the window still decides
    base = FakeBase(my_predbat, calendar=cal)
    base.set_calendar(cal, "off", local_str(my_predbat, -30), local_str(my_predbat, 30), "Running")
    if not fetch_vpp_event(base)["active"]:
        print("ERROR: a window containing now should be active even if the entity reads off")
        failed = True

    # With no readable window the state is all there is, so it is honoured
    base = FakeBase(my_predbat, calendar=cal)
    base.set_calendar(cal, "on", None, None, "No window")
    if not fetch_vpp_event(base)["active"]:
        print("ERROR: with no window, an entity reading on should be treated as active")
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


class PricingBase:
    """Stand-in exposing what load_vpp_slot() reads: the clock, the horizon, the event and the price."""

    def __init__(self, minutes_to_start, minutes_to_end, price=None):
        """An event the given minutes from a noon clock, priced at price when given."""
        self.minutes_now = 12 * 60
        self.forecast_minutes = 48 * 60
        self.args = {} if price is None else {"vpp_pence_per_kwh": price}
        self.vpp_event = {"active": minutes_to_start is not None and minutes_to_start <= 0, "minutes_to_start": minutes_to_start, "minutes_to_end": minutes_to_end}
        self.logs = []

    def get_arg(self, name, default=None, indirect=True):
        """Return an apps.yaml style argument."""
        return self.args.get(name, default)

    def time_abs_str(self, minute):
        """Plan minute as text, for the log."""
        return str(minute)

    def log(self, message, **kwargs):
        """Capture warnings so tests can assert on them."""
        self.logs.append(message)


def flat(rate):
    """Three days of a flat rate, keyed by absolute plan minute."""
    return {minute: rate for minute in range(0, 3 * 24 * 60)}


def test_vpp_event_is_priced(my_predbat):
    """The event window gains vpp_pence_per_kwh on both rates, and nothing outside it changes.

    Both directions, as Axle and Octopus saving sessions do: exporting earns the premium, and charging
    during the event gives up the same amount, so the plan cannot count on filling up inside the window.
    """
    print("  - test_vpp_event_is_priced")
    failed = False
    base = PricingBase(120, 240, price=200)
    export, imported, replicate = flat(5.0), flat(39.0), {}
    load_vpp_slot(base, export, export=True, rate_replicate=replicate)
    load_vpp_slot(base, imported, export=False)
    start, end = base.minutes_now + 120, base.minutes_now + 240
    if export[start] != 205.0 or export[end - 1] != 205.0:
        print("ERROR: the event should export at 5 + 200, got {} and {}".format(export[start], export[end - 1]))
        failed = True
    if imported[start] != 239.0:
        print("ERROR: importing during the event should cost 39 + 200, got {}".format(imported[start]))
        failed = True
    if export[start - 1] != 5.0 or export[end] != 5.0 or imported[end] != 39.0:
        print("ERROR: minutes outside the event must be untouched")
        failed = True
    if replicate.get(start) != "saving" or start - 1 in replicate:
        print("ERROR: the event minutes, and only those, should be marked so the price is not replicated")
        failed = True
    return failed


def test_vpp_price_needs_a_price_and_an_event(my_predbat):
    """No price configured, or no event known, leaves the rates exactly as they were."""
    print("  - test_vpp_price_needs_a_price_and_an_event")
    failed = False
    for base, why in ((PricingBase(120, 240), "no price"), (PricingBase(None, None, price=200), "no event"), (PricingBase(120, 240, price=0), "a zero price")):
        rates = flat(5.0)
        load_vpp_slot(base, rates, export=True)
        if rates != flat(5.0):
            print("ERROR: with {} the rates should not change".format(why))
            failed = True
    return failed


def test_vpp_price_is_clipped(my_predbat):
    """A running event is priced from midnight-relative zero up, and an event past the horizon is cut at it."""
    print("  - test_vpp_price_is_clipped")
    failed = False
    # Running now: started 30 minutes ago - the rest of it still counts
    base = PricingBase(-30, 60, price=200)
    rates = flat(5.0)
    load_vpp_slot(base, rates, export=True)
    if rates[base.minutes_now] != 205.0 or rates[base.minutes_now + 59] != 205.0 or rates[base.minutes_now + 60] != 5.0:
        print("ERROR: a running event should still be priced until it ends")
        failed = True

    # Ends beyond the forecast: nothing past the horizon is written
    base = PricingBase(48 * 60 - 60, 48 * 60 + 120, price=200)
    rates = {}
    load_vpp_slot(base, rates, export=True)
    if not rates or max(rates) >= base.minutes_now + base.forecast_minutes:
        print("ERROR: the event should be cut at the forecast horizon, last minute {}".format(max(rates) if rates else None))
        failed = True
    return failed


def test_vpp_bad_price_is_ignored(my_predbat):
    """A price that is not a number is warned about and ignored rather than crashing the rate build."""
    print("  - test_vpp_bad_price_is_ignored")
    failed = False
    base = PricingBase(120, 240, price="two dollars")
    rates = flat(5.0)
    try:
        load_vpp_slot(base, rates, export=True)
    except Exception as e:
        print("ERROR: a bad price raised {}: {}".format(type(e).__name__, e))
        return True
    if rates != flat(5.0) or not any("vpp_pence_per_kwh" in message for message in base.logs):
        print("ERROR: a bad price should leave the rates alone and say why, logged {}".format(base.logs))
        failed = True
    return failed


def test_vpp_price_is_wired_into_the_rates(my_predbat):
    """Both rate builds call load_vpp_slot, after the event is fetched and before the user's own overrides.

    Reads the source rather than running a full fetch, as the other wiring tests do. Order matters: the user's
    rates_*_override and manual rates are applied afterwards so they still have the final say.
    """
    print("  - test_vpp_price_is_wired_into_the_rates")
    import os

    failed = False
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fetch = open(os.path.join(here, "fetch.py")).read()
    for direction, override in (("import_rates, export=False", "rates_import_override"), ("export_rates, export=True", "rates_export_override")):
        call = "load_vpp_slot(self, {}".format(direction)
        if call not in fetch:
            print("ERROR: the {} build never prices the VPP event".format(direction.split(",")[0]))
            failed = True
        elif fetch.index(call) > fetch.index('self.get_arg("{}"'.format(override)):
            print("ERROR: the VPP price must go in before {} so the user's override still wins".format(override))
            failed = True
    if fetch.index("def fetch_config_options") < fetch.index("def fetch_sensor_data(") and "self.vpp_event = fetch_vpp_event(self)" not in fetch.split("def fetch_config_options")[1]:
        print("ERROR: the event has to be fetched in fetch_config_options, before the rates are built")
        failed = True
    return failed


def run_vpp_tests(my_predbat):
    """Run every VPP event test."""
    print("**** Running VPP event tests ****\n")
    failed = test_vpp_no_config(my_predbat)
    failed |= test_vpp_calendar_window(my_predbat)
    failed |= test_vpp_window_beats_calendar_state(my_predbat)
    failed |= test_vpp_live_signal(my_predbat)
    failed |= test_vpp_bad_calendar_data(my_predbat)
    failed |= test_vpp_timezone_handling(my_predbat)
    failed |= test_vpp_event_is_priced(my_predbat)
    failed |= test_vpp_price_needs_a_price_and_an_event(my_predbat)
    failed |= test_vpp_price_is_clipped(my_predbat)
    failed |= test_vpp_bad_price_is_ignored(my_predbat)
    failed |= test_vpp_price_is_wired_into_the_rates(my_predbat)
    return failed
