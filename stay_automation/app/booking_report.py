"""Aggregate Thursday booking times without saving reservation-level data."""
from calendar import monthrange
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo('America/New_York')


def report_window(now):
    end = now.astimezone(EASTERN).date()
    year = end.year - 1
    start = end.replace(year=year, day=min(end.day, monthrange(year, end.month)[1]))
    return start, end


def thursday_bookings(reservations, start, end):
    all_bookings = [0] * 24
    same_day = [0] * 24
    seen = set()
    missing = 0
    for row in reservations:
        if row.get('status') not in ('new', 'modified'):
            continue
        reservation_id = row.get('id')
        if reservation_id is not None:
            if reservation_id in seen:
                continue
            seen.add(reservation_id)
        try:
            booked = datetime.fromisoformat(row['reservationDate'])
            if booked.tzinfo is None:
                booked = booked.replace(tzinfo=timezone.utc)
            booked = booked.astimezone(EASTERN)
        except (KeyError, ValueError, TypeError):
            missing += 1
            continue
        if booked.weekday() != 3 or not start <= booked.date() < end:
            continue
        all_bookings[booked.hour] += 1
        try:
            arrival = date.fromisoformat(row['arrivalDate'])
        except (KeyError, ValueError, TypeError):
            missing += 1
            continue
        if arrival == booked.date():
            same_day[booked.hour] += 1

    total = sum(same_day)
    cumulative = 0
    rows = []
    for hour, count in enumerate(same_day):
        cumulative += count
        rows.append({
            'hour': f'{hour:02d}:00', 'same_day': count,
            'all_bookings': all_bookings[hour],
            'cumulative': round(100 * cumulative / total, 1) if total else None,
        })
    return {
        'rows': rows, 'total': total, 'all_total': sum(all_bookings),
        'before_two': sum(same_day[:14]), 'after_two': sum(same_day[14:]),
        'missing': missing,
    }
