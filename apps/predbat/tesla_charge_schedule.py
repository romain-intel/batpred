# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Model of how a Tesla on Charge on Solar uses its charge schedule, and when Predbat should change it.

A Tesla with Charge on Solar enabled behaves like this, per slot:

    inside a schedule window, below the Charge on Solar minimum  -> charges from any source at full rate
    anywhere else                                                 -> charges from surplus sun only

So grid energy only ever goes in up to the minimum, and only inside the window, and once it starts it runs
at full rate from the window's start until the minimum is reached. The window's placement is therefore the
only lever Predbat has over when the car buys, and it only matters while the car is below the minimum.

Two consequences shape everything here:

- The scattered cheapest slots Predbat would pick on its own are not something the car can do. It charges
  contiguously from the window start, so the realistic options are contiguous blocks, and the right window
  is the cheapest block that delivers the minimum by the ready time without covering a slot where the car
  is away or where Predbat has planned sun (the grid would displace free energy there).

- A window that already makes the car do something as cheap as that block should be left alone. On a flat
  off-peak tariff almost any window inside the off-peak band qualifies, which is why a well-set schedule
  rarely needs touching - and each write can wake the car.

Pure functions only: no I/O, no Predbat state, so the rules can be pinned by tests before anything talks to
a car. Schedules use the shape of Tesla's ChargeSchedule message (vehicle-command common.proto): times in
minutes after local midnight, days_of_week a bitmask with Sunday as bit 0, and an end time that may fall on
the following day.
"""

import math
from datetime import timedelta

# Tesla's days_of_week bitmask, from tesla-control's dayNamesBitMask: SUN=1, MON=2 ... SAT=64
TESLA_ALL_DAYS = 127
MINUTES_PER_DAY = 24 * 60


def tesla_day_bit(weekday):
    """
    Tesla days_of_week bit for a Python weekday (Monday=0 ... Sunday=6).

    Tesla counts from Sunday, Python from Monday, so Sunday is bit 0 and Monday bit 1.
    """
    return 1 << ((weekday + 1) % 7)


def schedule_windows(schedule, midnight, horizon_start, horizon_end):
    """
    The absolute plan-minute intervals a schedule is active for, within a horizon.

    A window belongs to the day it starts on, so an overnight 23:00-07:00 window on a Friday runs into
    Saturday morning whatever Saturday's bit says. A missing start means midnight; a missing end means the
    window stays open for a day, which is how "start at X, charge until done" behaves. A one-time schedule
    applies only to its first occurrence that has not already ended.

    Args:
    - schedule: dict with enabled, days_of_week, start_enabled, start_time, end_enabled, end_time, one_time
    - midnight: datetime of plan minute 0 (Predbat's local midnight today)
    - horizon_start, horizon_end: absolute plan minutes to report within

    Returns:
    - list: sorted, non-overlapping (start, end) plan-minute tuples
    """
    if not schedule.get("enabled", True):
        return []
    days = schedule.get("days_of_week", TESLA_ALL_DAYS)
    start_of_day = schedule.get("start_time", 0) if schedule.get("start_enabled", True) else 0
    end_of_day = schedule.get("end_time") if schedule.get("end_enabled", True) else None

    intervals = []
    # Start a day early: yesterday's overnight window can still be open now
    for day in range(horizon_start // MINUTES_PER_DAY - 1, horizon_end // MINUTES_PER_DAY + 1):
        if not days & tesla_day_bit((midnight + timedelta(days=day)).weekday()):
            continue
        start = day * MINUTES_PER_DAY + start_of_day
        if end_of_day is None:
            end = start + MINUTES_PER_DAY
        else:
            end = day * MINUTES_PER_DAY + end_of_day
            if end <= start:
                end += MINUTES_PER_DAY
        if end <= horizon_start or start >= horizon_end:
            continue
        intervals.append((max(start, horizon_start), min(end, horizon_end)))
        if schedule.get("one_time", False):
            break
    return merge_intervals(intervals)


def merge_intervals(intervals):
    """Sort and merge overlapping or touching (start, end) intervals."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def covered_minutes(start, end, intervals):
    """How many minutes of [start, end) fall inside the intervals."""
    return sum(max(0, min(end, i_end) - max(start, i_start)) for i_start, i_end in intervals)


def simulate_grid_charging(car_soc, min_kwh, rate_kw, loss, intervals, start_minute, end_minute, step, is_away=None):
    """
    The grid charging the car will actually do under a set of schedule windows.

    Walks forward from start_minute: in every step the car is present for, it charges at full rate for the
    part of the step inside a window, until it reaches the minimum. What it does above the minimum or
    outside a window is sun only, which this does not model - the point is what gets bought.

    Args:
    - car_soc: car charge now, kWh
    - min_kwh: the Charge on Solar minimum, kWh
    - rate_kw: charger rate, kW
    - loss: car_charging_loss - grid kWh times this is what reaches the battery
    - intervals: schedule windows, as from schedule_windows()
    - start_minute, end_minute: absolute plan minutes to simulate over
    - step: slot length in minutes
    - is_away: optional callable (start, end) -> bool, True when the car is not plugged in

    Returns:
    - list: {"start", "end", "kwh"} grid slots, kwh being what is drawn from the grid
    """
    slots = []
    soc = car_soc
    first = int(start_minute / step) * step
    for minute in range(first, end_minute, step):
        if soc >= min_kwh - 0.001:
            break
        slot_start = max(minute, start_minute)
        slot_end = min(minute + step, end_minute)
        if slot_end <= slot_start or (is_away and is_away(slot_start, slot_end)):
            continue
        inside = covered_minutes(slot_start, slot_end, intervals)
        if inside <= 0:
            continue
        kwh = min(rate_kw * inside / 60.0, (min_kwh - soc) / loss)
        soc += kwh * loss
        slots.append({"start": slot_start, "end": slot_end, "kwh": kwh})
    return slots


def slots_cost(slots, rate_import):
    """Cost of grid slots at the import rate of each slot's start minute."""
    return sum(slot["kwh"] * rate_import.get(slot["start"], 0.0) for slot in slots)


def best_contiguous_block(car_soc, min_kwh, rate_kw, loss, rate_import, start_minute, ready_minute, step, is_blocked=None):
    """
    The cheapest window the car can actually follow to reach the minimum by the ready time.

    The car charges contiguously from a window's start, so the candidates are runs of consecutive slots
    long enough to deliver the shortfall. A run may not include a slot that is blocked - the car away, or
    sun Predbat has planned for it - since grid charging there would either not happen or displace free
    energy. Ties go to the earliest run, which leaves the most slack before the ready time.

    Args:
    - car_soc, min_kwh, rate_kw, loss: as for simulate_grid_charging()
    - rate_import: dict of absolute plan minute -> import rate
    - start_minute: now, as an absolute plan minute
    - ready_minute: the minimum must be reached by this absolute plan minute
    - step: slot length in minutes
    - is_blocked: optional callable (start, end) -> bool

    Returns:
    - dict: {"start", "end", "slots", "cost"} for the best block, or None when there is nothing to buy or no
      block avoids the blocked slots. When the shortfall cannot fit before the ready time at all, the block
      is the longest that can, so the car gets as close as it physically can.
    """
    needed = min_kwh - car_soc
    if needed <= 0.001:
        return None
    per_slot = rate_kw * step / 60.0 * loss
    if per_slot <= 0:
        return None
    count = int(math.ceil(needed / per_slot - 1e-9))
    first = int(start_minute / step) * step
    if first < start_minute:
        first += step
    # When the whole shortfall cannot fit before the ready time, the best is the most that can
    count = min(count, (ready_minute - first) // step)
    if count <= 0:
        return None
    best = None
    for block_start in range(first, ready_minute - count * step + 1, step):
        block_end = block_start + count * step
        if is_blocked and any(is_blocked(minute, minute + step) for minute in range(block_start, block_end, step)):
            continue
        slots = simulate_grid_charging(car_soc, min_kwh, rate_kw, loss, [(block_start, block_end)], block_start, block_end, step)
        cost = slots_cost(slots, rate_import)
        if best is None or cost < best["cost"] - 1e-9:
            best = {"start": block_start, "end": block_end, "slots": slots, "cost": cost}
    return best


def check_schedule(intervals, car_soc, min_kwh, rate_kw, loss, rate_import, start_minute, ready_minute, step, planned_solar=None, is_away=None, cost_tolerance=0.02):
    """
    Decide whether the car's current schedule already does what Predbat wants.

    It does when the grid charging the windows cause, simulated as the car will really do it, reaches the
    minimum by the ready time (or as much of it as any block could), never lands on a slot Predbat has
    planned sun for, and costs no more than the best block the car could follow, within cost_tolerance
    (a fraction of that block's cost). Anything the car does at or above the minimum is sun only, so a car
    already there is always compatible - there is nothing a schedule can make it buy.

    Args:
    - intervals: the car's schedule windows, as from schedule_windows()
    - car_soc, min_kwh, rate_kw, loss, rate_import, start_minute, ready_minute, step: as above
    - planned_solar: optional list of {"start", "end"} slots Predbat plans to fill from the sun
    - is_away: optional callable (start, end) -> bool
    - cost_tolerance: fraction of the best block's cost the current windows may exceed it by

    Returns:
    - dict: {"compatible": bool, "reasons": [str], "tesla": [slots], "best": block or None}
    """
    planned_solar = planned_solar or []

    def on_planned_sun(start, end):
        return any(start < slot["end"] and end > slot["start"] for slot in planned_solar)

    def blocked(start, end):
        return on_planned_sun(start, end) or bool(is_away and is_away(start, end))

    result = {"compatible": True, "reasons": [], "tesla": [], "best": None}
    if min_kwh - car_soc <= 0.001:
        return result

    tesla = simulate_grid_charging(car_soc, min_kwh, rate_kw, loss, intervals, start_minute, ready_minute, step, is_away=is_away)
    best = best_contiguous_block(car_soc, min_kwh, rate_kw, loss, rate_import, start_minute, ready_minute, step, is_blocked=blocked)
    result["tesla"] = tesla
    result["best"] = best

    delivered = sum(slot["kwh"] for slot in tesla)
    achievable = sum(slot["kwh"] for slot in best["slots"]) if best else 0.0
    if delivered + 0.01 < achievable:
        result["reasons"].append("the windows deliver {:.2f}kWh of the {:.2f}kWh needed by the ready time".format(delivered, achievable))
    clashes = [slot for slot in tesla if on_planned_sun(slot["start"], slot["end"])]
    if clashes:
        result["reasons"].append("the windows grid-charge over {} slot(s) of planned sun".format(len(clashes)))
    if best is not None and delivered + 0.01 >= achievable:
        tesla_cost = slots_cost(tesla, rate_import)
        if tesla_cost > best["cost"] * (1.0 + cost_tolerance) + 0.01:
            result["reasons"].append("the windows cost {:.2f} against {:.2f} for the best block".format(tesla_cost, best["cost"]))
    result["compatible"] = not result["reasons"]
    return result


def window_for_block(block, schedule):
    """
    The schedule to write so the car follows a block: the same schedule, re-timed.

    Only the start and end move. The id is kept so the write updates the car's schedule in place rather than
    adding a second one, and the days, location and one-time flag are the user's, left as they were.

    Args:
    - block: as from best_contiguous_block()
    - schedule: the car's current schedule

    Returns:
    - dict: the schedule with start_time/end_time set to the block, both enabled
    """
    updated = dict(schedule)
    updated["start_enabled"] = True
    updated["end_enabled"] = True
    updated["start_time"] = block["start"] % MINUTES_PER_DAY
    updated["end_time"] = block["end"] % MINUTES_PER_DAY
    updated["enabled"] = True
    return updated
