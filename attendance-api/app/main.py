"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)
"""
import calendar
import logging
import os
import re
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Literal, Optional

from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False (default): real environment variables take priority

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_LIMIT = Decimal("4.50")

MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000

MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent)
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes() -> None:
    specs = [
        # employees
        ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
        ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
        ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
        ("employees", [("joined_on", ASCENDING)], {"name": "joined_on"}),
        # attendance_logs
        # natural key + race guard + per-employee ranges (monthly, list by emp_code)
        ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "emp_date_unique"}),
        # global list order (date desc, emp_code asc) and date-range analytics
        ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
        # status filter on the list endpoint
        ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
        # punch-out: latest record of an employee with punch_in <= t
        ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
    ]
    for coll, keys, opts in specs:
        try:
            db[coll].create_index(keys, **opts)
        except PyMongoError as exc:  # keep the app up; log loudly
            log.error("index %s on %s failed: %s", opts.get("name"), coll, exc)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: naive values from Mongo are UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc(truncate_ms: bool = True) -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000) if truncate_ms else n


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_hhmm(s: str) -> (int, int):
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: IST date of punch_in; overnight shifts: earlier than shift_end -> previous day."""
    ist = aware(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and (ist.hour, ist.minute, ist.second) < (*parse_hhmm(shift_end), 0):
        d -= timedelta(days=1)
    return d


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    h, m = parse_hhmm(shift_start)
    d = date_cls.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    h, m = parse_hhmm(shift_end)
    d = date_cls.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


# --------------------------------------------------------------------------- #
# Business rules R2 - R5, R8
# --------------------------------------------------------------------------- #
def round_half_up(value, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: strictly more than 10:00 after shift start -> whole minutes since shift start."""
    secs = int((aware(punch_in) - shift_start_dt(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals (exact Decimal arithmetic)."""
    secs = int((aware(punch_out) - aware(punch_in)).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only when >= 30."""
    secs = int((aware(punch_out) - shift_end_dt(date_str, shift_start, shift_end)).total_seconds())
    minutes = secs // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
           date_str: str, shift_start: str, shift_end: str) -> Dict[str, Any]:
    """All derived fields for a record (DATA_MODEL.md table)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, date_str, shift_start),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, shift_start, shift_end)
        out["half_day"] = Decimal(str(wh)) < HALF_DAY_LIMIT
    return out


# --------------------------------------------------------------------------- #
# Validation helpers / models
# --------------------------------------------------------------------------- #
def valid_date_str(s: str) -> str:
    if not DATE_RE.match(s):
        raise ValueError("must be YYYY-MM-DD")
    try:
        date_cls.fromisoformat(s)
    except ValueError:
        raise ValueError("not a real calendar date")
    return s


def check_date_param(s: str, name: str) -> str:
    try:
        return valid_date_str(s)
    except ValueError as exc:
        raise RequestValidationError([{"type": "value_error", "loc": ("query", name), "msg": str(exc), "input": s}])


def check_month(month: str) -> str:
    if not re.match(MONTH_RE, month or ""):
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "month"), "msg": "must be YYYY-MM", "input": month}])
    return month


def month_bounds(month: str) -> (str, str):
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def working_days(month: str, joined_on: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on if that is later."""
    first, last = month_bounds(month)
    start = max(date_cls.fromisoformat(first), date_cls.fromisoformat(joined_on))
    end = date_cls.fromisoformat(last)
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


EpochMs = StrictInt  # strict: no floats, strings or bools; range checked via Field below


class EmployeeIn(BaseModel):
    emp_code: StrictStr = Field(pattern=r"^EMP\d{4,6}$")
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = Field(default="09:30", pattern=TIME_RE)
    shift_end: StrictStr = Field(default="18:30", pattern=TIME_RE)
    joined_on: StrictStr

    @field_validator("joined_on")
    @classmethod
    def _joined(cls, v):
        return valid_date_str(v)

    @model_validator(mode="after")
    def _shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: StrictStr = Field(min_length=1)
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: StrictStr = Field(min_length=1)
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)


def paging(page: int, page_size: int) -> Dict[str, int]:
    return {"skip": (page - 1) * page_size, "limit": page_size}


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def employee_out(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _hist_value(key: str, v):
    return dt_to_ms(v) if key in ("punch_in", "punch_out") and isinstance(v, datetime) else v


def attendance_out(doc: Dict[str, Any]) -> Dict[str, Any]:
    history = []
    for h in doc.get("history") or []:
        history.append({
            "at": dt_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                k: {"from": _hist_value(k, c.get("from")), "to": _hist_value(k, c.get("to"))}
                for k, c in (h.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def get_employee_or_404(emp_code: str) -> Dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index decides, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q) if q else db.employees.estimated_document_count()
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc().replace(microsecond=0)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"]).isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, d, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: exactly one winner
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc().replace(microsecond=0)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = aware(rec["punch_in"])
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")
    fields = derive(rec["status"], pin, ts, rec["date"], emp["shift_start"], emp["shift_end"])
    fields["punch_out"] = ts
    # Atomic guard: only the first of simultaneous punch-outs matches punch_out == null.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


def attendance_query(emp_code, date_from, date_to, status) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def validated_range(date_from: Optional[str], date_to: Optional[str]) -> None:
    if date_from:
        check_date_param(date_from, "date_from")
    if date_to:
        check_date_param(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "date_from"), "msg": "date_from must be <= date_to", "input": date_from}])


ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validated_range(date_from, date_to)
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cur = db.attendance_logs.find(q).sort(ATT_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    try:
        valid_date_str(date)
    except ValueError as exc:
        raise RequestValidationError([{"type": "value_error", "loc": ("path", "date"), "msg": str(exc), "input": date}])
    emp = get_employee_or_404(emp_code)
    ss, se = emp["shift_start"], emp["shift_end"]

    for _ in range(5):  # optimistic concurrency: retry if someone else changed the record meanwhile
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not rec:
            raise HTTPException(404, "no attendance record for that date")

        old_status = rec["status"]
        new_status = body.status or old_status
        old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
        old_in = aware(old_in) if old_in else None
        old_out = aware(old_out) if old_out else None

        if new_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
            new_in = new_out = None
        else:
            new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
            new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
            if new_in is None:
                raise HTTPException(422, "a presence status requires punch_in")
            if attendance_date(new_in, ss, se).isoformat() != date:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if new_out is not None:
                if new_out <= new_in:
                    raise HTTPException(422, "punch_out must be after punch_in")
                if new_out - new_in > timedelta(hours=24):
                    raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

        new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                    **derive(new_status, new_in, new_out, date, ss, se)}
        old_vals = {
            "status": old_status, "punch_in": old_in, "punch_out": old_out,
            "work_hours": rec.get("work_hours"),
            "late_minutes": rec.get("late_minutes") or 0,
            "overtime_minutes": rec.get("overtime_minutes") or 0,
            "half_day": bool(rec.get("half_day", False)),
        }
        changes = {k: {"from": old_vals[k], "to": new_vals[k]} for k in new_vals if new_vals[k] != old_vals[k]}
        if not changes:
            raise HTTPException(422, "request changes nothing")

        n = len(rec.get("history") or [])
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # The filter pins the state we computed from: a concurrent punch-out / correction
        # makes it miss, and we loop again instead of losing history.
        guard = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            f"history.{n}": {"$exists": False},
        }
        if n > 0:
            guard[f"history.{n - 1}"] = {"$exists": True}
        updated = db.attendance_logs.find_one_and_update(
            guard,
            {"$set": new_vals, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated:
            return attendance_out(updated)
    raise HTTPException(409, "concurrent modification, please retry")


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
# MongoDB $dayOfWeek: 1 = Sunday ... 7 = Saturday  -> Mon-Fri is 2..6
def _dow(expr):
    return {"$dayOfWeek": {"$dateFromString": {"dateString": expr, "format": "%Y-%m-%d"}}}


_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_PRESENT_VALUE = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
_IS_WEEKDAY = {"$and": [{"$gte": [_dow("$date"), 2]}, {"$lte": [_dow("$date"), 6]}]}
_LATE = {"$ifNull": ["$late_minutes", 0]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _PRESENT_VALUE, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": _LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    check_month(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    wd = working_days(month, emp["joined_on"])
    present = float(r.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(Decimal(str(present)) / Decimal(wd) * 100, 2) if wd else None,
    }


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last}}  # R9
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        # per employee: one pre-aggregated stats document (zero logs -> empty array)
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _PRESENT_VALUE, 0]}},
                    "work_sum": {"$sum": {"$cond": [
                        {"$and": [_IS_PRESENT, {"$ne": [{"$type": "$work_hours"}, "null"]},
                                  {"$ne": [{"$type": "$work_hours"}, "missing"]}]},
                        {"$toDecimal": "$work_hours"}, 0]}},
                    "work_cnt": {"$sum": {"$cond": [
                        {"$and": [_IS_PRESENT, {"$ne": [{"$type": "$work_hours"}, "null"]},
                                  {"$ne": [{"$type": "$work_hours"}, "missing"]}]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": _LATE},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "s",
        }},
        {"$set": {"s": {"$ifNull": [{"$arrayElemAt": ["$s", 0]}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
            "work_sum": {"$sum": {"$ifNull": ["$s.work_sum", 0]}},
            "work_cnt": {"$sum": {"$ifNull": ["$s.work_cnt", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$set": {"avg_work_hours": {"$cond": [{"$gt": ["$work_cnt", 0]}, {"$divide": ["$work_sum", "$work_cnt"]}, None]}}},
        {"$sort": {"_id": 1}},
    ]


def _num(v):
    if isinstance(v, Decimal128):
        return v.to_decimal()
    return v


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    check_month(month)
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        avg = _num(r.get("avg_work_hours"))
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(_num(r["present_days"])),
            "avg_work_hours": float(Decimal(avg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if avg is not None else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipe: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipe.append({"$match": {"e.department": department}})  # rank inside the department only
    pipe += [
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},  # standard competition ranking: 1,2,2,4
        }},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    check_month(month)
    rows = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in rows]}


# Half-up rounding in the database (the $round operator rounds half-to-even).
def _half_up(expr, places: int = 4):
    f = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, {"$toDecimal": f}]}, {"$toDecimal": "0.5"}]}},
                        {"$toDecimal": f}]}


def trend_pipeline(department: str, d_from: str, d_to: str) -> list:
    start = datetime.fromisoformat(d_from).replace(tzinfo=UTC)
    end_excl = datetime.fromisoformat(d_to).replace(tzinfo=UTC) + timedelta(days=1)
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs"}},
        {"$unwind": "$logs"},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]},
                                                  {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        # anchor row just outside the range so $densify never sees empty input (dropped again below)
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"_id": 0, "d": start - timedelta(days=1), "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}}]}},
        {"$set": {"d": {"$ifNull": ["$d", {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}]}}},
        # gap filling: one row per calendar day, weekends and empty days included
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$d"}, 6]}]},
        }},
        {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
        # headcount per day (R9): department employees joined on/before that day
        {"$lookup": {
            "from": "employees",
            "let": {"ds": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$ds"]}}},
                {"$count": "n"},
            ],
            "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$set": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _half_up({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}),
            None]}}},
        {"$setWindowFields": {
            "sortBy": {"d": 1},
            "output": {"avg7": {"$avg": "$rate", "window": {"documents": [-6, "current"]}}},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$toDouble": "$present_count"}, "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate", None]}, None, {"$toDouble": "$rate"}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$avg7", None]}, None, {"$toDouble": _half_up("$avg7")}]},
        }},
        {"$sort": {"date": 1}},
    ]


def validated_trend_range(d_from: str, d_to: str) -> None:
    check_date_param(d_from, "from")
    check_date_param(d_to, "to")
    if d_to < d_from:
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "to"), "msg": "to must be >= from", "input": d_to}])
    if (date_cls.fromisoformat(d_to) - date_cls.fromisoformat(d_from)).days + 1 > 92:
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "to"), "msg": "range exceeds 92 days", "input": d_to}])


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    validated_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    items = list(db.employees.aggregate(trend_pipeline(department, from_, to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
EXPLAIN_ENDPOINTS = ("attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend")


def _need(value, name):
    if value is None or value == "":
        raise RequestValidationError([{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None}])
    return value


def _jsonable(obj):
    """Raw explain output -> plain JSON types."""
    from bson import json_util
    import json
    return json.loads(json_util.dumps(obj, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        validated_range(date_from, date_to)
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": attendance_query(emp_code, date_from, date_to, status),
               "sort": dict(ATT_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            coll, pipe = "attendance_logs", employee_monthly_pipeline(_need(emp_code, "emp_code"), check_month(_need(month, "month")))
        elif endpoint == "department_summary":
            coll, pipe = "employees", department_summary_pipeline(check_month(_need(month, "month")), department)
        elif endpoint == "late_leaderboard":
            coll, pipe = "attendance_logs", leaderboard_pipeline(check_month(_need(month, "month")), limit, department)
        else:
            dep, f, t = _need(department, "department"), _need(from_, "from"), _need(to, "to")
            validated_trend_range(f, t)
            coll, pipe = "employees", trend_pipeline(dep, f, t)
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": _jsonable(result)}
