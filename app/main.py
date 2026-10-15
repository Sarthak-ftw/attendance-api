"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""

# ── stdlib ──────────────────────────────────────────────────────────────── #
import os
import re
import json
import logging
import calendar
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Annotated

# ── third-party ─────────────────────────────────────────────────────────── #
from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# ── configuration ───────────────────────────────────────────────────────── #
# load_dotenv() does NOT override real env vars, which is exactly what we want
load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # FIX #1: all datetimes from Mongo come back tz-aware UTC
    serverSelectionTimeoutMS=5000,  # 5s timeout instead of hanging
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))  # UTC+05:30
log = logging.getLogger("attendance_api")

# ── constants ───────────────────────────────────────────────────────────── #
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")     # strict YYYY-MM-DD format
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")  # strict YYYY-MM format

# Reusable Pydantic type: epoch ms as a strict int in the valid range
# StrictInt rejects floats, bools and strings; ge/le reject seconds-looking values
EpochMs = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000)]

# Status categories (R6: which statuses count as "present")
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


# ═══════════════════════════════════════════════════════════════════════════ #
#  HELPERS — rounding, time conversion, shift logic, derived fields
# ═══════════════════════════════════════════════════════════════════════════ #

def round_half_up(value, decimals):
    """Round using ROUND_HALF_UP, not Python's default banker's rounding.
    FIX #6: Python's round(2.5)=2 (banker's), but the spec wants 2.5→3."""
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt):
    """Convert a tz-aware UTC datetime to epoch milliseconds for the API.
    Returns None when dt is None (e.g. punch_out before punching out)."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms):
    """Convert epoch milliseconds to a tz-aware UTC datetime.
    R1: truncate to whole seconds BEFORE any calculation or storage.
    Example: 09:40:00.900 is stored and calculated as 09:40:00."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)


def resolve_shift(punch_in_utc, shift_start_str, shift_end_str):
    """Given a punch-in instant (UTC) and the employee's shift (IST HH:MM strings),
    return (attendance_date_str, shift_start_dt, shift_end_dt).

    FIX #4: the starter ignored IST and overnight shifts entirely.

    R1 overnight rule: when shift_end <= shift_start (e.g. 22:00–06:00),
    a punch-in whose IST time is EARLIER than shift_end belongs to the
    shift that STARTED the previous calendar day."""
    # Step 1: find the IST calendar date of the punch-in
    punch_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_ist.date()

    # Parse shift times into hours and minutes
    sh, sm = int(shift_start_str[:2]), int(shift_start_str[3:])
    eh, em = int(shift_end_str[:2]), int(shift_end_str[3:])

    # Overnight shift: end time is at or before start time (e.g. 22:00–06:00)
    is_overnight = (eh, em) <= (sh, sm)

    if is_overnight:
        # If the punch IST time is before shift_end, the worker arrived during
        # the tail end of YESTERDAY's shift
        punch_time = (punch_ist.hour, punch_ist.minute, punch_ist.second)
        end_time = (eh, em, 0)
        if punch_time < end_time:
            ist_date = ist_date - timedelta(days=1)

    # Step 2: build the exact shift_start and shift_end as tz-aware datetimes
    shift_start_dt = datetime(
        ist_date.year, ist_date.month, ist_date.day, sh, sm, tzinfo=IST
    )
    if is_overnight:
        # The shift end falls on the NEXT calendar day
        next_day = ist_date + timedelta(days=1)
        shift_end_dt = datetime(
            next_day.year, next_day.month, next_day.day, eh, em, tzinfo=IST
        )
    else:
        shift_end_dt = datetime(
            ist_date.year, ist_date.month, ist_date.day, eh, em, tzinfo=IST
        )

    return ist_date.isoformat(), shift_start_dt, shift_end_dt


def compute_late_minutes(punch_in_utc, shift_start_dt):
    """R2: late only if punch-in is STRICTLY more than 600 s (10 min grace) after
    shift start.  When late, floor(total seconds since shift_start / 60).
    FIX #5: compares seconds > 600 first, then floors to whole minutes."""
    diff_secs = (punch_in_utc - shift_start_dt).total_seconds()
    if diff_secs > 600:
        return int(diff_secs // 60)
    return 0


def compute_work_hours(punch_in_utc, punch_out_utc):
    """R4: (punch_out − punch_in) in seconds / 3600, rounded 2 dp half-up."""
    secs = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(secs / 3600, 2)


def compute_overtime(punch_out_utc, shift_end_dt):
    """R3: floor(minutes from shift_end to punch_out), only if >= 30."""
    diff_secs = (punch_out_utc - shift_end_dt).total_seconds()
    minutes = int(diff_secs // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours_rounded):
    """R5: half_day is True when the ALREADY-ROUNDED work_hours < 4.50.
    A half day counts as 0.5 of a present day in analytics."""
    return work_hours_rounded < 4.50


# ── date / month parsing & date arithmetic ──────────────────────────────── #

def parse_date(s, label="date"):
    """Validate a strict YYYY-MM-DD string: regex for format, fromisoformat for
    calendar reality (rejects Feb 30, etc.). Returns the date object.
    Raises HTTPException(422) on any failure."""
    if not DATE_RE.match(s):
        raise HTTPException(422, f"Invalid date format for {label}: {s}")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"Invalid calendar date for {label}: {s}")


def parse_month(s):
    """Validate a YYYY-MM month string. Raises HTTPException(422) on failure."""
    if not MONTH_RE.match(s):
        raise HTTPException(422, f"Invalid month format: {s}")
    year = int(s[:4])
    if year < 1 or year > 9999:
        raise HTTPException(422, f"Invalid year in month: {s}")


def get_month_date_bounds(month_str):
    """Given a validated 'YYYY-MM', returns:
    (start_str, next_month_str, month_end_str, month_start_date, month_end_date)."""
    parse_month(month_str)
    year, m = int(month_str[:4]), int(month_str[5:7])
    start_str = f"{year:04d}-{m:02d}-01"
    last_day = calendar.monthrange(year, m)[1]
    month_end_str = f"{year:04d}-{m:02d}-{last_day:02d}"
    if m == 12:
        next_month_str = f"{year + 1:04d}-01-01"
    else:
        next_month_str = f"{year:04d}-{m + 1:02d}-01"
    return (
        start_str,
        next_month_str,
        month_end_str,
        date(year, m, 1),
        date(year, m, last_day),
    )


def count_working_days(start_date: date, end_date: date) -> int:
    """R7: Working days are Mon-Fri (no holiday calendar)."""
    if start_date > end_date:
        return 0
    cur = start_date
    count = 0
    one_day = timedelta(days=1)
    while cur <= end_date:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        cur += one_day
    return count


# ── Mongo doc → API response formatting ────────────────────────────────── #

def format_employee(doc):
    """Convert a Mongo employee document to the API response shape.
    FIX #11/#23: _id removed, created_at converted from BSON date to epoch ms."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


def format_attendance_record(doc):
    """Convert a Mongo attendance document to the API response shape.
    FIX #18/#20: no 'id' field, datetimes as epoch ms, defaults for legacy records
    that may be missing half_day or history."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),             # null until punch-out
        "late_minutes": doc.get("late_minutes", 0),       # legacy: missing = 0
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),           # legacy: missing = false
        "history": format_history(doc.get("history", [])),  # legacy: missing = []
    }


def format_history(history):
    """Convert history entries: BSON dates → epoch ms for 'at' and punch from/to.
    Non-punch fields (late_minutes, work_hours, etc.) are kept as-is."""
    result = []
    for entry in history:
        h = {
            "at": dt_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for field, change in entry["changes"].items():
            if field in ("punch_in", "punch_out"):
                # Punch fields are BSON dates in Mongo, epoch ms in the API
                h["changes"][field] = {
                    "from": dt_to_epoch_ms(change.get("from")),
                    "to": dt_to_epoch_ms(change.get("to")),
                }
            else:
                h["changes"][field] = {
                    "from": change.get("from"),
                    "to": change.get("to"),
                }
        result.append(h)
    return result


# ═══════════════════════════════════════════════════════════════════════════ #
#  PIPELINE BUILDERS (Shared between analytics endpoints & explain endpoint)
# ═══════════════════════════════════════════════════════════════════════════ #

def build_employee_monthly_pipeline(emp_code: str, month_str: str) -> list[dict]:
    """Shared pipeline builder for GET /analytics/employees/{emp_code}/monthly
    and GET /admin/explain/employee_monthly."""
    start_str, next_month_str, _, _, _ = get_month_date_bounds(month_str)

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_str, "$lt": next_month_str},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],  # Mon-Fri (1=Sun, 7=Sat)
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


def build_department_summary_pipeline(month_str: str, department: Optional[str] = None) -> list[dict]:
    """Shared pipeline builder for GET /analytics/departments/summary
    and GET /admin/explain/department_summary.
    Starts from employees (joined_on <= month_end) to guarantee headcount includes
    zero-log employees, then $lookups attendance_logs with index support."""
    start_str, next_month_str, month_end_str, _, _ = get_month_date_bounds(month_str)

    emp_match = {"joined_on": {"$lte": month_end_str}}
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": start_str, "$lt": next_month_str}
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$l.date"
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                    "$$l.work_hours",
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "work_hours_sum": {"$sum": "$emp_work_hours_sum"},
                "work_hours_count": {"$sum": "$emp_work_hours_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(month_str: str, limit: int = 10, department: Optional[str] = None) -> list[dict]:
    """Shared pipeline builder for GET /analytics/leaderboard/late
    and GET /admin/explain/late_leaderboard.
    Uses $setWindowFields with $rank for standard competition ranking (1,2,2,4).
    Filters by department before ranking if department is specified."""
    start_str, next_month_str, _, _, _ = get_month_date_bounds(month_str)

    pipeline = [
        # 1. Match month date range and positive late minutes (index supported)
        {
            "$match": {
                "date": {"$gte": start_str, "$lt": next_month_str},
                "late_minutes": {"$gt": 0},
            }
        },
        # 2. Group by employee
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # 3. Lookup employee details (drops logs of non-existent employees)
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    # 4. If department filter is supplied, apply before ranking
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    # 5. Standard competition ranking via $setWindowFields
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        # 6. Apply limit AFTER ranking: rank <= limit (ties at cutoff both included)
        {"$match": {"rank": {"$lte": limit}}},
        # 7. Order: total_late_minutes desc, emp_code asc
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return pipeline


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[dict, dict, int, int]:
    """Shared query builder for GET /attendance and GET /admin/explain/attendance_list."""
    filt = {}
    if emp_code is not None:
        filt["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        date_cond = {}
        if date_from is not None:
            df = parse_date(date_from, "date_from")
            date_cond["$gte"] = df.isoformat()
        if date_to is not None:
            dt = parse_date(date_to, "date_to")
            date_cond["$lte"] = dt.isoformat()
        if date_from is not None and date_to is not None:
            if date_from > date_to:
                raise HTTPException(422, "date_from cannot be after date_to")
        filt["date"] = date_cond

    if status is not None:
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(422, f"Invalid status: {status}")
        filt["status"] = status

    sort_spec = {"date": -1, "emp_code": 1}
    skip = (page - 1) * page_size
    limit = page_size
    return filt, sort_spec, skip, limit


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> list[dict]:
    """Shared pipeline builder for GET /analytics/departments/{department}/trend
    and GET /admin/explain/department_trend.
    Anchors on employees with {department: department} to guarantee an indexed IXSCAN.
    Generates exactly one row per calendar day from..to inclusive.
    Computes headcount via joined_ons array filter, and dept_logs via index-supported lookup.
    Computes attendance_rate and 7-day moving average using integer half-up arithmetic."""
    from_d = date.fromisoformat(from_str)
    to_d = date.fromisoformat(to_str)
    num_days = (to_d - from_d).days + 1

    return [
    {'$match': {'department': department}},
    {
        '$group': {
            '_id': '$department',
            'emp_codes': {'$push': '$emp_code'},
            'joined_ons': {'$push': '$joined_on'},
        }
    },
    {'$project': {'emp_codes': 1, 'joined_ons': 1, 'day_offset': {'$range': [0, num_days]}}},
    {'$unwind': '$day_offset'},
    {
        '$project': {
            'emp_codes': 1,
            'joined_ons': 1,
            'date_val': {
                '$dateAdd': {
                    'startDate': {'$dateFromString': {'dateString': from_str}},
                    'unit': 'day',
                    'amount': '$day_offset',
                }
            }
        }
    },
    {
        '$project': {
            'emp_codes': 1,
            'joined_ons': 1,
            'date': {'$dateToString': {'date': '$date_val', 'format': '%Y-%m-%d'}},
            'iso_day': {'$isoDayOfWeek': '$date_val'},
        }
    },
    {
        '$project': {
            'emp_codes': 1,
            'date': 1,
            'iso_day': 1,
            'headcount': {
                '$size': {
                    '$filter': {
                        'input': '$joined_ons',
                        'as': 'j',
                        'cond': {'$lte': ['$$j', '$date']}
                    }
                }
            }
        }
    },
    {
        '$lookup': {
            'from': 'attendance_logs',
            'let': {'day_str': '$date', 'emps': '$emp_codes'},
            'pipeline': [
                {
                    '$match': {
                        '$expr': {
                            '$and': [
                                {'$eq': ['$date', '$$day_str']},
                                {'$in': ['$emp_code', '$$emps']}
                            ]
                        }
                    }
                }
            ],
            'as': 'dept_logs'
        }
    },
    {
        '$project': {
            'date': 1,
            'is_working_day': {'$in': ['$iso_day', [1, 2, 3, 4, 5]]},
            'headcount': 1,
            'present_count': {
                '$sum': {
                    '$map': {
                        'input': '$dept_logs',
                        'as': 'l',
                        'in': {
                            '$cond': [
                                {'$in': ['$$l.status', ['PRESENT', 'WFH', 'ON_DUTY']]},
                                {'$cond': [{'$ifNull': ['$$l.half_day', False]}, 0.5, 1.0]},
                                0.0
                            ]
                        }
                    }
                }
            },
            'late_count': {
                '$sum': {
                    '$map': {
                        'input': '$dept_logs',
                        'as': 'l',
                        'in': {
                            '$cond': [
                                {'$gt': [{'$ifNull': ['$$l.late_minutes', 0]}, 0]},
                                1,
                                0
                            ]
                        }
                    }
                }
            }
        }
    },
    {
        '$project': {
            'date': 1,
            'is_working_day': 1,
            'headcount': 1,
            'present_count': 1,
            'late_count': 1,
            'rate_int': {
                '$cond': [
                    {
                        '$and': [
                            {'$eq': ['$is_working_day', True]},
                            {'$gt': ['$headcount', 0]},
                        ]
                    },
                    {
                        '$floor': {
                            '$divide': [
                                {
                                    '$add': [
                                        {'$multiply': [10000, {'$multiply': ['$present_count', 2]}]},
                                        '$headcount',
                                    ]
                                },
                                {'$multiply': [2, '$headcount']},
                            ]
                        }
                    },
                    None,
                ]
            },
        }
    },
    {
        '$project': {
            'date': 1,
            'is_working_day': 1,
            'headcount': 1,
            'present_count': 1,
            'late_count': 1,
            'rate_int': 1,
            'attendance_rate': {
                '$cond': [
                    {'$eq': ['$rate_int', None]},
                    None,
                    {'$divide': ['$rate_int', 10000]},
                ]
            },
        }
    },
    {
        '$setWindowFields': {
            'sortBy': {'date': 1},
            'output': {
                'avg_rate_int': {
                    '$avg': '$rate_int',
                    'window': {
                        'documents': [-6, 0]
                    },
                }
            },
        }
    },
    {
        '$project': {
            '_id': 0,
            'date': 1,
            'is_working_day': 1,
            'headcount': 1,
            'present_count': 1,
            'late_count': 1,
            'attendance_rate': 1,
            'moving_avg_7d': {
                '$cond': [
                    {'$eq': ['$avg_rate_int', None]},
                    None,
                    {
                        '$divide': [
                            {'$floor': {'$add': ['$avg_rate_int', 0.5]}},
                            10000,
                        ]
                    },
                ]
            },
        }
    },
    {'$sort': {'date': 1}},
]


# ═══════════════════════════════════════════════════════════════════════════ #
#  PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════════════════ #

class EmployeeIn(BaseModel):
    """POST /employees request body.
    FIX #8: validation for patterns, lengths, shifts differing, calendar date."""
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name:     str = Field(min_length=1, max_length=100)
    email:    str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:   str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("not a valid calendar date")
        return v

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    """POST /attendance/punch-in request body."""
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    """POST /attendance/punch-out request body."""
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    """PATCH /attendance/{emp_code}/{date} request body.
    Extra fields (derived values sent by client) are ignored by default."""
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None


# ═══════════════════════════════════════════════════════════════════════════ #
#  STARTUP & LIFESPAN — create indexes idempotently
# ═══════════════════════════════════════════════════════════════════════════ #

def create_indexes():
    """FIX #2: create all needed indexes at startup idempotently.
    Each index is wrapped so a single failure is logged, never crashes the app."""
    try:
        client.admin.command("ping")
    except Exception as exc:
        log.warning("MongoDB unreachable at startup, skipping index creation: %s", exc)
        return

    def safe(coll, keys, **kw):
        try:
            coll.create_index(keys, **kw)
        except Exception as exc:
            log.warning("Index creation failed on %s: %s", coll.name, exc)

    # ── employees ──
    safe(db.employees, [("emp_code", 1)], unique=True)
    safe(db.employees, [("department", 1), ("emp_code", 1)])
    safe(db.employees, [("department", 1), ("joined_on", 1)])
    safe(db.employees, [("joined_on", 1)])

    # ── attendance_logs ──
    safe(db.attendance_logs, [("emp_code", 1), ("date", 1)], unique=True)
    safe(db.attendance_logs, [("date", -1), ("emp_code", 1)])
    safe(db.attendance_logs, [("emp_code", 1), ("punch_in", -1)])
    safe(db.attendance_logs, [("status", 1), ("date", -1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager replacing deprecated @app.on_event('startup')."""
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ═══════════════════════════════════════════════════════════════════════════ #
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════ #

# ── GET /health ────────────────────────────────────────────────────────── #

@app.get("/health")
def health():
    """FIX #3: readiness probe pings Mongo, returns 503 on failure."""
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


# ── POST /employees ────────────────────────────────────────────────────── #

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create an employee. Unique index catches duplicates → 409."""
    doc = {
        "emp_code":    body.emp_code,
        "name":        body.name,
        "email":       body.email,
        "department":  body.department,
        "shift_start": body.shift_start,
        "shift_end":   body.shift_end,
        "joined_on":   body.joined_on,
        "created_at":  datetime.now(timezone.utc),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_employee(doc)


# ── GET /employees ─────────────────────────────────────────────────────── #

@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees paginated, sorted by emp_code asc."""
    filt = {}
    if department is not None:
        filt["department"] = department

    total = db.employees.count_documents(filt)
    skip = (page - 1) * page_size

    items = list(
        db.employees.find(filt, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [format_employee(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ── POST /attendance/punch-in ──────────────────────────────────────────── #

@app.post("/attendance/punch-in", status_code=201)
def punch_in_endpoint(body: PunchInIn):
    """Record a punch-in. Creates the attendance record for the day (R1, R2)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    att_date, shift_start_dt, _ = resolve_shift(
        ts, emp["shift_start"], emp["shift_end"]
    )
    late = compute_late_minutes(ts, shift_start_dt)

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         ts,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    return format_attendance_record(doc)


# ── POST /attendance/punch-out ─────────────────────────────────────────── #

@app.post("/attendance/punch-out", status_code=200)
def punch_out_endpoint(body: PunchOutIn):
    """Closes the employee's most recent record whose punch_in <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3), and half_day (R5).
    Atomic update on punch_out: None guarantees exactly one 200 under parallel punch-outs."""
    if body.punched_at is not None:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(404, "No punch-in found for employee")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Attendance record is already punched out")

    punch_in_dt = rec["punch_in"]
    diff_secs = (ts - punch_in_dt).total_seconds()
    if diff_secs <= 0 or diff_secs > 86400:
        raise HTTPException(422, "Punch-out must be strictly after punch-in and within 24 hours")

    _, _, shift_end_dt = resolve_shift(punch_in_dt, emp["shift_start"], emp["shift_end"])
    work_hours = compute_work_hours(punch_in_dt, ts)
    overtime = compute_overtime(ts, shift_end_dt)
    half_day = compute_half_day(work_hours)

    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }},
    )
    if res.modified_count == 0:
        raise HTTPException(409, "Attendance record is already punched out")

    updated_rec = db.attendance_logs.find_one({"_id": rec["_id"]})
    return format_attendance_record(updated_rec)


# ── GET /attendance ────────────────────────────────────────────────────── #

@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records sorted by date desc, emp_code asc."""
    filt, sort_spec, skip, limit = build_attendance_list_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        page_size=page_size,
    )
    total = db.attendance_logs.count_documents(filt)
    cursor = (
        db.attendance_logs.find(filt, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(limit)
    )

    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ── PATCH /attendance/{emp_code}/{date} ────────────────────────────────── #

@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    """Regularize (manually correct) one day's attendance record.
    Recomputes derived fields from final state and appends one history entry.
    Uses optimistic locking ($expr on history length + stored fields) to prevent lost history."""

    # 1. Path validation (422)
    parse_date(date, "date")

    # 2. Resource existence checks (404)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, f"No attendance record found for {emp_code} on {date}")

    old_status = rec["status"]
    old_punch_in = rec.get("punch_in")
    old_punch_out = rec.get("punch_out")
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)
    history_len = len(rec.get("history") or [])

    # FIX #2: Early no-op check based on request fields vs stored fields
    req_punch_in_dt = epoch_ms_to_dt(body.punch_in) if body.punch_in is not None else None
    req_punch_out_dt = epoch_ms_to_dt(body.punch_out) if body.punch_out is not None else None

    status_same = (body.status is None or body.status == old_status)
    punch_in_same = (body.punch_in is None or (old_punch_in is not None and req_punch_in_dt == old_punch_in))
    punch_out_same = (body.punch_out is None or (old_punch_out is not None and req_punch_out_dt == old_punch_out))

    if status_same and punch_in_same and punch_out_same:
        raise HTTPException(422, "Request resulted in no changes to the record")

    target_status = body.status if body.status is not None else old_status

    # 3. Status and punch times cross-validation (422)
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"Cannot supply punch times when status is {target_status}")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        if body.punch_in is not None:
            final_punch_in = req_punch_in_dt
            # FIX #3: R1 date check ONLY when body.punch_in is supplied
            mapped_date, shift_start_dt, shift_end_dt = resolve_shift(
                final_punch_in, emp["shift_start"], emp["shift_end"]
            )
            if mapped_date != date:
                raise HTTPException(
                    422, f"punch_in maps to attendance date {mapped_date}, but record date is {date}"
                )
        else:
            final_punch_in = old_punch_in
            if final_punch_in is None:
                raise HTTPException(422, f"Status {target_status} requires a punch_in instant")
            _, shift_start_dt, shift_end_dt = resolve_shift(
                final_punch_in, emp["shift_start"], emp["shift_end"]
            )

        if body.punch_out is not None:
            final_punch_out = req_punch_out_dt
        else:
            final_punch_out = old_punch_out

        if final_punch_out is not None:
            diff_secs = (final_punch_out - final_punch_in).total_seconds()
            if diff_secs <= 0 or diff_secs > 86400:
                raise HTTPException(422, "punch_out must be strictly after punch_in and within 24 hours")
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, shift_end_dt)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, shift_start_dt)

    # 4. Detect what actually changed among the 7 tracked fields
    changes = {}
    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if old_punch_in != final_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": final_punch_in}
    if old_punch_out != final_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": final_punch_out}
    if old_work_hours != final_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": final_work_hours}
    if old_late_minutes != final_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": final_late_minutes}
    if old_overtime_minutes != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": final_overtime_minutes}
    if old_half_day != final_half_day:
        changes["half_day"] = {"from": old_half_day, "to": final_half_day}

    if not changes:
        raise HTTPException(422, "Request resulted in no changes to the record")

    # 5. Build the history entry (punch datetimes stored as BSON UTC dates)
    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # FIX #4: Optimistic lock matches _id, stored status/punch_in/punch_out, and history length
    filter_doc = {
        "_id": rec["_id"],
        "status": old_status,
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "$expr": {
            "$eq": [
                {"$size": {"$ifNull": ["$history", []]}},
                history_len,
            ]
        },
    }
    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": final_punch_in,
            "punch_out": final_punch_out,
            "work_hours": final_work_hours,
            "late_minutes": final_late_minutes,
            "overtime_minutes": final_overtime_minutes,
            "half_day": final_half_day,
        },
        "$push": {
            "history": history_entry,
        },
    }

    res = db.attendance_logs.update_one(filter_doc, update_doc)
    if res.matched_count == 0:
        raise HTTPException(409, "Concurrent modification detected; please re-read and retry")

    updated_rec = db.attendance_logs.find_one({"_id": rec["_id"]})
    return format_attendance_record(updated_rec)


# ── GET /analytics/employees/{emp_code}/monthly ────────────────────────── #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee (backed by aggregation pipeline)."""
    parse_month(month)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    _, _, _, month_start_d, month_end_d = get_month_date_bounds(month)
    joined_on_d = date.fromisoformat(emp["joined_on"])

    if joined_on_d > month_end_d:
        working_days = 0
    else:
        effective_start = max(month_start_d, joined_on_d)
        working_days = count_working_days(effective_start, month_end_d)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        row = results[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# ── GET /analytics/departments/summary ─────────────────────────────────── #

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month. Headcount includes zero-log employees (R9).
    Averages work_hours over records, rounded 2 dp half-up. Omit departments with headcount 0."""
    parse_month(month)

    pipeline = build_department_summary_pipeline(month, department)
    groups = list(db.employees.aggregate(pipeline))

    items = []
    for g in groups:
        wh_count = g.get("work_hours_count", 0)
        if wh_count > 0:
            wh_sum = g.get("work_hours_sum", 0.0)
            avg_work_hours = round_half_up(wh_sum / wh_count, 2)
        else:
            avg_work_hours = None

        items.append({
            "department": g["_id"],
            "headcount": int(g["headcount"]),
            "present_days": float(g.get("present_days", 0.0)),
            "avg_work_hours": avg_work_hours,
            "late_count": int(g.get("late_count", 0)),
            "total_late_minutes": int(g.get("total_late_minutes", 0)),
            "leave_count": int(g.get("leave_count", 0)),
            "on_duty_count": int(g.get("on_duty_count", 0)),
        })

    return {
        "month": month,
        "items": items,
    }


# ── GET /analytics/leaderboard/late ────────────────────────────────────── #

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Ranks employees by total late minutes in the month (employees with late > 0 only).
    Standard competition ranking (1, 2, 2, 4) via $setWindowFields and $rank.
    Limit applies after ranking (more than limit rows allowed on ties)."""
    parse_month(month)

    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


# ── GET /analytics/departments/{department}/trend ──────────────────────── #

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date:   str = Query(..., alias="to"),
):
    """Daily attendance trend for a department. Exactly one row per calendar day from..to inclusive.
    Computes attendance_rate and 7-day moving average using half-up integer arithmetic."""
    # 1. Validate dates and range constraints (422 first)
    d_from = parse_date(from_date, "from")
    d_to = parse_date(to_date, "to")

    if d_to < d_from:
        raise HTTPException(422, "'to' date must not be earlier than 'from' date")

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(422, "Date range must not exceed 92 days inclusive")

    # 2. Check department exists in employees (404 if department has no employees)
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, f"Department '{department}' not found")

    # 3. Execute aggregation pipeline
    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }


# ═══════════════════════════════════════════════════════════════════════════ #
#  GET /admin/explain/{endpoint} (Stage 5)
# ═══════════════════════════════════════════════════════════════════════════ #

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = None,
    department: Optional[str] = None,
    limit:      int = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_:      Optional[str] = Query(default=None, alias="from"),
    to:         Optional[str] = None,
    page:       int = Query(default=1, ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    """Returns raw MongoDB explain executionStats for an endpoint query.
    Reuses the exact same builders as real endpoints to ensure parity."""
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"Unknown explain endpoint '{endpoint}'")

    if endpoint == "attendance_list":
        filt, sort_spec, skip, lim = build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=page,
            page_size=page_size,
        )
        coll_name = "attendance_logs"
        cmd = {
            "find": coll_name,
            "filter": filt,
            "sort": sort_spec,
            "skip": skip,
            "limit": lim,
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code query parameter is required")
        if not month:
            raise HTTPException(422, "month query parameter is required")
        parse_month(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        coll_name = "attendance_logs"
        cmd = {
            "aggregate": coll_name,
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month query parameter is required")
        parse_month(month)
        pipeline = build_department_summary_pipeline(month, department)
        coll_name = "employees"
        cmd = {
            "aggregate": coll_name,
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month query parameter is required")
        parse_month(month)
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        coll_name = "attendance_logs"
        cmd = {
            "aggregate": coll_name,
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department query parameter is required")
        if not from_:
            raise HTTPException(422, "from query parameter is required")
        if not to:
            raise HTTPException(422, "to query parameter is required")
        d_from = parse_date(from_, "from")
        d_to = parse_date(to, "to")
        if d_to < d_from:
            raise HTTPException(422, "'to' date must not be earlier than 'from' date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(422, "Date range must not exceed 92 days inclusive")
        pipeline = build_department_trend_pipeline(department, from_, to)
        coll_name = "employees"
        cmd = {
            "aggregate": coll_name,
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")

    explain_json = json.loads(json_util.dumps(raw_explain))
    return {
        "endpoint": endpoint,
        "collection": coll_name,
        "explain": explain_json,
    }
