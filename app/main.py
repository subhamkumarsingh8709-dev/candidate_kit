"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
Environment: MONGO_URI, MONGO_DB
"""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv #type: ignore
from fastapi import FastAPI, HTTPException, Query#type: ignore
from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator, model_validator #type: ignore
from pymongo import MongoClient #type: ignore
from pymongo.errors import DuplicateKeyError, PyMongoError #type: ignore

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_TS, MAX_TS = 100_000_000_000, 4_102_444_800_000
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def startup():
    try:
        client.admin.command("ping")
        db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employee_code")
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_employee_date"
        )
        db.attendance_logs.create_index(
            [("date", -1), ("emp_code", 1)], name="ix_attendance_date_employee"
        )
        db.attendance_logs.create_index(
            [("emp_code", 1), ("punch_out", 1), ("punch_in", -1)],
            name="ix_open_punch"
        )
        db.employees.create_index(
            [("department", 1), ("emp_code", 1)], name="ix_employee_department_code"
        )
        db.employees.create_index(
            [("department", 1), ("joined_on", 1)], name="ix_employee_department_joined"
        )
    except PyMongoError as exc:
        raise RuntimeError("MongoDB startup initialization failed") from exc


# ------------------------------ Helpers -------------------------------------

def millis_to_dt(value):
    if type(value) is not int or not MIN_TS <= value <= MAX_TS:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def to_utc_aware(value: datetime) -> datetime:
    # PyMongo normally returns naive BSON datetimes representing UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def validate_month(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month_num = map(int, month.split("-"))
    first = date(year, month_num, 1)
    return first, date(year, month_num, calendar.monthrange(year, month_num)[1])


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, "date must be YYYY-MM-DD")


def shift_datetimes(shift_date: str, shift_start: str, shift_end: str):
    day = date.fromisoformat(shift_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start_clock, end_clock = (sh, sm), (eh, em)
    start_day = day
    end_day = day + timedelta(days=1) if end_clock <= start_clock else day
    start_dt = datetime.combine(start_day, time(sh, sm), tzinfo=IST).astimezone(UTC)
    end_dt = datetime.combine(end_day, time(eh, em), tzinfo=IST).astimezone(UTC)
    return start_dt, end_dt


def attendance_shift_date(local_punch: datetime, shift_start: str, shift_end: str) -> date:
    start = tuple(map(int, shift_start.split(":")))
    end = tuple(map(int, shift_end.split(":")))
    local_clock = (local_punch.hour, local_punch.minute)
    if end <= start and local_clock < end:
        return local_punch.date() - timedelta(days=1)
    return local_punch.date()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    local_in = to_utc_aware(punch_in).astimezone(IST)
    shift_date = attendance_shift_date(local_in, shift_start, shift_end)
    start_dt, _ = shift_datetimes(shift_date.isoformat(), shift_start, shift_end)
    elapsed_seconds = int((local_in.astimezone(UTC) - start_dt).total_seconds())
    if elapsed_seconds <= 600:
        return 0
    return max(0, elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((to_utc_aware(punch_out) - to_utc_aware(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, shift_date: str) -> int:
    _, shift_end_dt = shift_datetimes(shift_date, shift_start, shift_end)
    elapsed_seconds = int((to_utc_aware(punch_out) - shift_end_dt).total_seconds())
    minutes = max(0, elapsed_seconds // 60)
    return minutes if minutes >= 30 else 0


def serialize_attendance(record: dict) -> dict:
    record.pop("_id", None)
    for key in ("punch_in", "punch_out"):
        value = record.get(key)
        if value is not None:
            record[key] = int(to_utc_aware(value).timestamp() * 1000)
    return record


def month_bounds(month: str):
    first, last = validate_month(month)
    return first.isoformat(), last.isoformat(), first, last


def month_match(month: str):
    first, last, _, _ = month_bounds(month)
    return {"date": {"$gte": first, "$lte": last}}


def round_half_up_expr(expression, places: int):
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, factor]}, 0.5]}}, factor]}


def present_value_expr():
    return {"$cond": [
        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
        0,
    ]}


# -------------------------------- Models ------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    department: str = Field(min_length=1, max_length=100)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("shift times must use HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_ts(cls, value):
        if value is not None:
            millis_to_dt(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def valid_ts(cls, value):
        if value is not None:
            millis_to_dt(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def valid_ts(cls, value):
        if value is not None:
            millis_to_dt(value)
        return value


# -------------------------------- Health ------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


# -------------------------------- Employees ---------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1)
                 .skip((page - 1) * page_size).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ------------------------------- Attendance ---------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = millis_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    local_ts = ts.astimezone(IST)
    shift_day = attendance_shift_date(local_ts, emp["shift_start"], emp["shift_end"])
    d = shift_day.isoformat()
    doc = {
        "emp_code": body.emp_code, "date": d, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this shift date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query, {"_id": 0})
                .sort([("date", -1), ("emp_code", 1)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance(x) for x in docs],
            "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = millis_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "punch_out": None
    }, sort=[("punch_in", -1)])
    if not record:
        raise HTTPException(404, "no open punch-in found")
    punch_in_dt = to_utc_aware(record["punch_in"])
    if ts <= punch_in_dt:
        raise HTTPException(422, "punch-out must be after punch-in")
    if ts - punch_in_dt > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be within 24 hours")
    hours = compute_work_hours(punch_in_dt, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours,
                  "overtime_minutes": overtime, "half_day": hours < 4.50}}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]}, {"_id": 0})
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize(emp_code: str, attendance_date: str, body: RegularizeIn):
    parse_date(attendance_date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    old_status = record["status"]
    new_status = body.status or old_status
    punch_in = millis_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    punch_out = millis_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")
    punch_in = to_utc_aware(punch_in) if punch_in else None
    punch_out = to_utc_aware(punch_out) if punch_out else None

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        punch_in = punch_out = None
    elif punch_in is None:
        raise HTTPException(422, "punch-in is required for present status")

    if punch_out is not None:
        if punch_in is None or punch_out <= punch_in:
            raise HTTPException(422, "invalid punch-out time")
        if punch_out - punch_in > timedelta(hours=24):
            raise HTTPException(422, "punch-out must be within 24 hours")

    hours = compute_work_hours(punch_in, punch_out) if punch_in and punch_out and new_status in PRESENCE else None
    late = compute_late_minutes(punch_in, emp["shift_start"], emp["shift_end"]) if punch_in and new_status in PRESENCE else 0
    overtime = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], attendance_date) if punch_in and punch_out and new_status in PRESENCE else 0
    half_day = hours is not None and hours < 4.50

    changes = {}
    for key, value in {
        "status": new_status, "punch_in": punch_in, "punch_out": punch_out,
        "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
        "half_day": half_day,
    }.items():
        if record.get(key) != value:
            changes[key] = {"from": record.get(key), "to": value}
    if not changes:
        raise HTTPException(422, "no changes to save")

    history_item = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by, "reason": body.reason, "changes": changes,
    }
    # Optimistic concurrency: update only if the record has not changed since read.
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": record.get("history", []),
         "status": old_status, "punch_in": record.get("punch_in"), "punch_out": record.get("punch_out")},
        {"$set": {"status": new_status, "punch_in": punch_in, "punch_out": punch_out,
                  "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
                  "half_day": half_day},
         "$push": {"history": history_item}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance changed concurrently; retry regularization")
    return {"message": "attendance updated", "changes": changes}


# -------------------------------- Analytics ----------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    if not db.employees.find_one({"emp_code": emp_code}):
        raise HTTPException(404, "employee not found")
    first, last, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$set": {"weekday": {"$lte": [{"$isoDayOfWeek": {
            "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
        }}, 5]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE)]}, "$weekday"]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
        {"$project": {"_id": 0, "present_days": 1, "leave_days": 1, "late_count": 1,
                      "total_late_minutes": 1, "total_overtime_minutes": 1}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    metrics = rows[0] if rows else {
        "present_days": 0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0,
    }
    return {"emp_code": emp_code, "month": month, **metrics}


@app.get("/analytics/departments/summary")
def department_summary(month: str):
    first, last, _, month_end = month_bounds(month)
    pipeline = [
        {"$match": {"joined_on": {"$lte": month_end.isoformat()}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        {"$lookup": {"from": "attendance_logs", "let": {"dept": "$_id"},
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$lookup": {"from": "employees", "localField": "emp_code",
                             "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
                {"$match": {"$expr": {"$eq": ["$employee.department", "$$dept"]}}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": present_value_expr()},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ], "as": "attendance"}},
        {"$set": {"metrics": {"$ifNull": [{"$arrayElemAt": ["$attendance", 0]}, {
            "present_days": 0, "late_count": 0, "total_late_minutes": 0,
            "leave_count": 0, "on_duty_count": 0,
        }]}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
                      "present_days": "$metrics.present_days", "late_count": "$metrics.late_count",
                      "total_late_minutes": "$metrics.total_late_minutes",
                      "leave_count": "$metrics.leave_count", "on_duty_count": "$metrics.on_duty_count"}},
        {"$sort": {"department": 1}},
    ]
    return {"month": month, "items": list(db.employees.aggregate(pipeline))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}},
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start, end = parse_date(from_date), parse_date(to_date)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "Invalid date range; maximum is 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")
    return {"department": department, "items": list(db.aggregate(_trend_pipeline(department, start, end)))}


def _trend_pipeline(department: str, start: date, end: date):
    n = (end - start).days + 1
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    return [
        {"$documents": [{}]},
        {"$set": {"offset": {"$range": [0, n]}}},
        {"$unwind": "$offset"},
        {"$set": {"day": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$offset"}}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "count"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"$expr": {"$eq": ["$e.department", department]}}},
            {"$group": {"_id": None, "present_count": {"$sum": present_value_expr()},
                        "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}}
        ], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                  "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
                  "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1,
                      "moving_avg_7d": round_half_up_expr("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}},
    ]


# Explain endpoint contract uses one of five supported endpoint names.
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if from_date:
            parse_date(from_date)
        if to_date:
            parse_date(to_date)
        if from_date or to_date:
            query["date"] = {}
            if from_date:
                query["date"]["$gte"] = from_date
            if to_date:
                query["date"]["$lte"] = to_date
        return _explain_find("attendance_logs", query, [("date", -1), ("emp_code", 1)])

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first, last, _, _ = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}}]
        return _explain_aggregate("attendance_logs", pipeline, endpoint)

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first, last, _, _ = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}}]
        return _explain_aggregate("attendance_logs", pipeline, endpoint)

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first, last, _, _ = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": "$emp_code", "late": {"$sum": "$late_minutes"}}},
                    {"$sort": {"late": -1}}, {"$limit": limit}]
        return _explain_aggregate("attendance_logs", pipeline, endpoint)

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start, end = parse_date(from_date), parse_date(to_date)
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "Invalid date range; maximum is 92 days")
        if not db.employees.find_one({"department": department}):
            raise HTTPException(404, "Department not found")
        return _explain_aggregate("database", _trend_pipeline(department, start, end), endpoint)

    raise HTTPException(422, "endpoint must be one of attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend")


def _explain_find(collection: str, query: dict, sort: list):
    result = db.command("explain", {
        "find": collection, "filter": query, "sort": dict(sort)
    }, verbosity="executionStats")
    return {"endpoint": "attendance_list", "collection": collection, "explain": result}


def _explain_aggregate(collection: str, pipeline: list, endpoint: str):
    command = {"aggregate": 1 if collection == "database" else collection,
               "pipeline": pipeline, "cursor": {}}
    result = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": result}
