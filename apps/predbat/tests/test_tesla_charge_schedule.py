# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the Tesla Charge on Solar schedule model in tesla_charge_schedule.py.

The car grid-charges only inside a schedule window, only below its Charge on Solar minimum, at full rate
from the window's start. These pin that model and the decision built on it: leave a schedule alone when the
car's real behaviour under it is already as good as anything it could follow, and re-time it only when not.

The clock is fixed: plan minute 0 is midnight on Friday 2026-09-25, and "now" is noon that day, so the
overnight window under test runs from Friday night into Saturday morning.
"""

from datetime import datetime

from tesla_charge_schedule import (
    TESLA_ALL_DAYS,
    best_contiguous_block,
    check_schedule,
    schedule_windows,
    simulate_grid_charging,
    tesla_day_bit,
    window_for_block,
)

MIDNIGHT = datetime(2026, 9, 25)  # a Friday
NOW = 12 * 60
STEP = 30
# Saturday 07:30, as an absolute plan minute
READY = 24 * 60 + 7 * 60 + 30
RATE_KW = 7.0  # 3.5kWh per half hour


def overnight(start="00:00", end="07:30", days=TESLA_ALL_DAYS, **extra):
    """A schedule in Tesla's ChargeSchedule shape."""
    to_minutes = lambda text: int(text[:2]) * 60 + int(text[3:])
    schedule = {"id": 1727000000, "enabled": True, "days_of_week": days, "start_enabled": True, "start_time": to_minutes(start), "end_enabled": True, "end_time": to_minutes(end), "one_time": False, "latitude": 37.4, "longitude": -122.1}
    schedule.update(extra)
    return schedule


def flat_rates(price=39.0, overrides=None):
    """Import rates for two days, flat unless overridden per absolute minute range."""
    rates = {minute: price for minute in range(0, 3 * 24 * 60)}
    for (start, end), value in (overrides or {}).items():
        for minute in range(start, end):
            rates[minute] = value
    return rates


def test_day_bits():
    """Tesla counts days from Sunday; Python from Monday."""
    print("  - test_day_bits")
    failed = False
    for weekday, bit in ((6, 1), (0, 2), (1, 4), (4, 32), (5, 64)):
        if tesla_day_bit(weekday) != bit:
            print("ERROR: Python weekday {} should be Tesla bit {}, got {}".format(weekday, bit, tesla_day_bit(weekday)))
            failed = True
    if sum(tesla_day_bit(weekday) for weekday in range(7)) != TESLA_ALL_DAYS:
        print("ERROR: all seven days should add up to {}".format(TESLA_ALL_DAYS))
        failed = True
    return failed


def test_schedule_windows():
    """Windows land on the right days, wrap past midnight, and honour one-time and disabled schedules."""
    print("  - test_schedule_windows")
    failed = False
    # Every day 00:00-07:30, looking from Friday noon for 36 hours: only Saturday's window is ahead
    windows = schedule_windows(overnight(), MIDNIGHT, NOW, NOW + 36 * 60)
    if windows != [(1440, 1440 + 450)]:
        print("ERROR: expected only Saturday 00:00-07:30, got {}".format(windows))
        failed = True

    # 23:00-07:00 starting Friday night belongs to Friday and runs into Saturday
    windows = schedule_windows(overnight("23:00", "07:00", days=tesla_day_bit(4)), MIDNIGHT, NOW, NOW + 36 * 60)
    if windows != [(23 * 60, 1440 + 7 * 60)]:
        print("ERROR: a Friday-only overnight window should run Friday 23:00 to Saturday 07:00, got {}".format(windows))
        failed = True

    # At 03:00 Saturday, Friday night's window is still open - it started yesterday
    windows = schedule_windows(overnight("23:00", "07:00", days=tesla_day_bit(4)), MIDNIGHT, 1440 + 180, 1440 + 24 * 60)
    if windows != [(1440 + 180, 1440 + 7 * 60)]:
        print("ERROR: at 03:00 Saturday the Friday 23:00-07:00 window should still be open until 07:00, got {}".format(windows))
        failed = True

    # Saturday's bit alone does not open a window that starts on Friday
    if schedule_windows(overnight("23:00", "07:00", days=tesla_day_bit(5)), MIDNIGHT, NOW, NOW + 24 * 60):
        print("ERROR: a Saturday-only 23:00 window starts on Saturday night, outside the next 24 hours")
        failed = True

    # One-time: only the first occurrence
    windows = schedule_windows(overnight(one_time=True), MIDNIGHT, NOW, NOW + 60 * 60)
    if len(windows) != 1:
        print("ERROR: a one-time schedule should open once, got {}".format(windows))
        failed = True

    if schedule_windows(overnight(enabled=False), MIDNIGHT, NOW, NOW + 36 * 60):
        print("ERROR: a disabled schedule has no windows")
        failed = True
    return failed


def test_simulate_charges_from_window_start():
    """The car charges contiguously from the window's start at full rate, and stops at the minimum."""
    print("  - test_simulate_charges_from_window_start")
    failed = False
    windows = schedule_windows(overnight(), MIDNIGHT, NOW, READY)
    slots = simulate_grid_charging(10.0, 30.0, RATE_KW, 1.0, windows, NOW, READY, STEP)
    # 20kWh at 3.5kWh a slot: five full slots and a partial sixth, starting at Saturday 00:00
    if [slot["start"] for slot in slots] != [1440 + STEP * n for n in range(6)]:
        print("ERROR: expected six slots from Saturday 00:00, got {}".format([slot["start"] for slot in slots]))
        failed = True
    if abs(sum(slot["kwh"] for slot in slots) - 20.0) > 0.001:
        print("ERROR: should stop exactly at the minimum, bought {}".format(sum(slot["kwh"] for slot in slots)))
        failed = True

    if simulate_grid_charging(30.0, 30.0, RATE_KW, 1.0, windows, NOW, READY, STEP):
        print("ERROR: a car already at its minimum buys nothing")
        failed = True

    # Away for the first hour of the window: those slots are skipped and charging starts after
    away = lambda start, end: start < 1440 + 60 and end > 1440
    slots = simulate_grid_charging(10.0, 30.0, RATE_KW, 1.0, windows, NOW, READY, STEP, is_away=away)
    if slots and slots[0]["start"] != 1440 + 60:
        print("ERROR: charging should start once the car is back at 01:00, got {}".format(slots[0]["start"]))
        failed = True
    return failed


def test_best_block():
    """The cheapest contiguous run that reaches the minimum, avoiding blocked slots, earliest on a tie."""
    print("  - test_best_block")
    failed = False
    # Flat overnight: the earliest block wins the tie
    block = best_contiguous_block(10.0, 30.0, RATE_KW, 1.0, flat_rates(), NOW, READY, STEP)
    if block is None or block["start"] != NOW:
        print("ERROR: on a flat tariff the earliest block should win, got {}".format(block and block["start"]))
        failed = True

    # Cheap band 02:00-05:30 Saturday: the block sits inside it
    rates = flat_rates(45.0, {(1440 + 120, 1440 + 330): 20.0})
    block = best_contiguous_block(10.0, 30.0, RATE_KW, 1.0, rates, NOW, READY, STEP)
    if block is None or not (1440 + 120 <= block["start"] and block["end"] <= 1440 + 330):
        print("ERROR: the block should sit inside the 02:00-05:30 cheap band, got {}-{}".format(block and block["start"], block and block["end"]))
        failed = True

    # Blocking a slot in the middle of the cheap band moves the block rather than straddling it
    blocked = lambda start, end: start < 1440 + 240 and end > 1440 + 210
    block = best_contiguous_block(10.0, 30.0, RATE_KW, 1.0, rates, NOW, READY, STEP, is_blocked=blocked)
    if block and block["start"] < 1440 + 240 and block["end"] > 1440 + 210:
        print("ERROR: the block must not cover a blocked slot, got {}-{}".format(block["start"], block["end"]))
        failed = True

    # Too little time before the ready time: the longest block that fits
    block = best_contiguous_block(10.0, 30.0, RATE_KW, 1.0, flat_rates(), READY - 90, READY, STEP)
    if block is None or block["end"] != READY or len(block["slots"]) != 3:
        print("ERROR: with 90 minutes left the block should be the last three slots, got {}".format(block))
        failed = True
    return failed


def test_flat_offpeak_window_is_left_alone():
    """The everyday case: an off-peak window on a flat off-peak rate is already as good as it gets."""
    print("  - test_flat_offpeak_window_is_left_alone")
    failed = False
    # 39p all night, 61p from 16:00-21:00 - the window 00:00-07:30 sits inside the flat band
    rates = flat_rates(39.0, {(16 * 60, 21 * 60): 61.0})
    windows = schedule_windows(overnight(), MIDNIGHT, NOW, READY)
    verdict = check_schedule(windows, 10.0, 30.0, RATE_KW, 1.0, rates, NOW, READY, STEP)
    if not verdict["compatible"]:
        print("ERROR: a flat off-peak window should be left alone, got {}".format(verdict["reasons"]))
        failed = True

    # A car already at its minimum never needs a change, whatever the window
    verdict = check_schedule(schedule_windows(overnight("17:00", "19:00"), MIDNIGHT, NOW, READY), 30.0, 30.0, RATE_KW, 1.0, rates, NOW, READY, STEP)
    if not verdict["compatible"]:
        print("ERROR: a car at its minimum buys nothing, so any window is fine, got {}".format(verdict["reasons"]))
        failed = True
    return failed


def test_window_too_small():
    """Failure mode one: the window cannot deliver what Predbat needs bought by the ready time."""
    print("  - test_window_too_small")
    failed = False
    windows = schedule_windows(overnight("06:00", "07:30"), MIDNIGHT, NOW, READY)
    verdict = check_schedule(windows, 10.0, 30.0, RATE_KW, 1.0, flat_rates(), NOW, READY, STEP)
    if verdict["compatible"] or not any("deliver" in reason for reason in verdict["reasons"]):
        print("ERROR: a 90 minute window cannot deliver 20kWh, got {}".format(verdict))
        failed = True
    return failed


def test_window_over_planned_sun():
    """Failure mode two: below the minimum, a window over planned sun makes the car buy instead."""
    print("  - test_window_over_planned_sun")
    failed = False
    # Ready at 14:00 Saturday, sun planned 09:00-12:00, window 08:00-14:00 on Saturdays only - an every-day
    # window would also be open this Friday afternoon, and the car would reach its minimum today instead
    ready = 1440 + 14 * 60
    saturday = tesla_day_bit(5)
    sun = [{"start": 1440 + 9 * 60 + STEP * n, "end": 1440 + 9 * 60 + STEP * (n + 1)} for n in range(6)]
    windows = schedule_windows(overnight("08:00", "14:00", days=saturday), MIDNIGHT, NOW, ready)
    verdict = check_schedule(windows, 25.0, 30.0, RATE_KW, 1.0, flat_rates(), NOW, ready, STEP, planned_solar=sun)
    # 5kWh needed from 08:00 is two slots, 08:00-09:00, which stop before the sun - no clash yet
    if not verdict["compatible"]:
        print("ERROR: grid charging that finishes before the sun starts does not clash, got {}".format(verdict["reasons"]))
        failed = True

    # A window starting at 09:00 grid-charges straight over the planned sun
    windows = schedule_windows(overnight("09:00", "14:00", days=saturday), MIDNIGHT, NOW, ready)
    verdict = check_schedule(windows, 25.0, 30.0, RATE_KW, 1.0, flat_rates(), NOW, ready, STEP, planned_solar=sun)
    if verdict["compatible"] or not any("planned sun" in reason for reason in verdict["reasons"]):
        print("ERROR: a window over planned sun below the minimum should be flagged, got {}".format(verdict))
        failed = True
    if verdict["best"] and any(slot["start"] < 1440 + 12 * 60 and slot["end"] > 1440 + 9 * 60 for slot in verdict["best"]["slots"]):
        print("ERROR: the proposed block must avoid the planned sun, got {}".format(verdict["best"]))
        failed = True
    return failed


def test_window_costs_more_than_it_needs_to():
    """A window that makes the car buy at a dearer time than it could is re-timed, keeping the user's settings."""
    print("  - test_window_costs_more_than_it_needs_to")
    failed = False
    # 61p until 02:00, 20p from 02:00 - the window starts in the dear part
    rates = flat_rates(39.0, {(1440, 1440 + 120): 61.0, (1440 + 120, 1440 + 450): 20.0})
    schedule = overnight("00:00", "07:30")
    windows = schedule_windows(schedule, MIDNIGHT, NOW, READY)
    verdict = check_schedule(windows, 10.0, 30.0, RATE_KW, 1.0, rates, NOW, READY, STEP)
    if verdict["compatible"] or not any("cost" in reason for reason in verdict["reasons"]):
        print("ERROR: charging at 61p when 20p was available should be flagged, got {}".format(verdict))
        return True

    updated = window_for_block(verdict["best"], schedule)
    if updated["start_time"] < 120:
        print("ERROR: the new window should start in the 20p band, got {}".format(updated["start_time"]))
        failed = True
    for key in ("id", "days_of_week", "latitude", "longitude", "one_time"):
        if updated.get(key) != schedule[key]:
            print("ERROR: re-timing must keep the schedule's {} ({} -> {})".format(key, schedule[key], updated.get(key)))
            failed = True

    # And the re-timed window is itself compatible, so the next check leaves it alone
    again = check_schedule(schedule_windows(updated, MIDNIGHT, NOW, READY), 10.0, 30.0, RATE_KW, 1.0, rates, NOW, READY, STEP)
    if not again["compatible"]:
        print("ERROR: the proposed window should pass its own check, got {}".format(again["reasons"]))
        failed = True
    return failed


def run_tesla_charge_schedule_tests(my_predbat):
    """Run every Tesla charge schedule model test. The module is pure, so no shared state is touched."""
    print("**** Running Tesla charge schedule tests ****\n")
    failed = test_day_bits()
    failed |= test_schedule_windows()
    failed |= test_simulate_charges_from_window_start()
    failed |= test_best_block()
    failed |= test_flat_offpeak_window_is_left_alone()
    failed |= test_window_too_small()
    failed |= test_window_over_planned_sun()
    failed |= test_window_costs_more_than_it_needs_to()
    return failed
