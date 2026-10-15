# Employee Attendance & Analytics API

A high-performance, contract-compliant FastAPI + MongoDB REST API for employee attendance tracking and organization-wide analytics, engineered for the HROne backend assessment.

---

## 1. Overview & Architecture

- **Framework**: FastAPI (Python 3.11+) running with Uvicorn.
- **Database**: MongoDB 7.0+ via PyMongo (synchronous client executed in FastAPI threadpools).
- **Core Principle**: All production application code resides in [`app/main.py`](file:///c:/Users/sarth/Downloads/attendance-api/app/main.py).
- **Performance**: Guaranteed indexed query plans (`IXSCAN` with zero `COLLSCAN`) across 100,000+ attendance records.
- **Data Integrity**: Concurrency-safe operations using database-level unique constraints and optimistic concurrency locking.

---

## 2. Quickstart & Local Setup

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ (running locally on port 27017 or accessible via URI)

### Setup Instructions

1. **Create and activate a virtual environment**:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .venv\Scripts\Activate.ps1
   # macOS/Linux:
   source .venv/bin/activate
   ```

2. **Install runtime dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**:
   Real environment variables take precedence over `.env`:
   ```bash
   # Windows PowerShell:
   $env:MONGO_URI = "mongodb://localhost:27017"
   $env:MONGO_DB  = "attendance_db"
   ```

4. **Seed sample documents** (optional for local dev):
   ```bash
   python sample_seed.py
   ```

5. **Start the API server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive Swagger documentation is available at `http://127.0.0.1:8000/docs`.

---

## 3. Business Rules Implementation (R1 – R10)

| Rule | Description & Implementation |
|---|---|
| **R1 (IST & Shifts)** | All dates and shifts operate in Indian Standard Time (UTC+05:30). In [`resolve_shift()`](file:///c:/Users/sarth/Downloads/attendance-api/app/main.py#L90), overnight shifts (`shift_end <= shift_start`) map punches before `shift_end` to the previous calendar day. Timestamps are truncated to whole seconds (`microsecond=0`) before any calculation. |
| **R2 (Late Minutes)** | 10-minute grace period. Punch-in must be **strictly greater than 600 seconds** after shift start to be counted as late; once late, `late_minutes = floor(seconds_since_shift_start / 60)`. |
| **R3 (Overtime)** | Minimum 30-minute threshold. `overtime_minutes = floor(seconds_after_shift_end / 60)` if $\ge 30$, otherwise `0`. |
| **R4 (Work Hours)** | `(punch_out - punch_in) / 3600` rounded to 2 decimal places **half-up** using `decimal.Decimal` (preventing banker's rounding bias). Null until punch-out. |
| **R5 (Half Day)** | Computed at punch-out. `half_day = True` if rounded `work_hours < 4.50`. Counts as 0.5 in presence analytics. |
| **R6 (Presence Status)** | `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not. |
| **R7 (Working Days)** | Mon–Fri calendar days from `max(month_start, joined_on)` to `month_end`. `present_days` only accrues on Mon–Fri records. Weekend records still contribute to late minutes, late count, and overtime. |
| **R8 (Rounding Precision)** | Amounts and hours round half-up to 2 decimal places (`0.01`). Rates round half-up to 4 decimal places (`0.0001`). |
| **R9 (Headcount)** | Headcount counts all department employees with `joined_on <= period_end`, including employees with zero attendance logs. |
| **R10 (Pagination)** | `page >= 1`, `page_size` 1–100 (default 20). `total` accurately reflects applied filters. |

---

## 4. Database Indexing Strategy

All indexes are created idempotently at startup in [`create_indexes()`](file:///c:/Users/sarth/Downloads/attendance-api/app/main.py#L880):

### `employees` Collection
1. `{"emp_code": 1}` (**Unique**): Fast employee lookup; prevents duplicate employee registration under parallel requests.
2. `{"department": 1, "emp_code": 1}`: Covers department-filtered employee listings and sorts by `emp_code` with zero in-memory sort.
3. `{"department": 1, "joined_on": 1}`: Optimizes department monthly summaries matching `department` and `joined_on <= month_end`.
4. `{"joined_on": 1}`: Serves organization-wide department summaries when no department filter is specified.

### `attendance_logs` Collection
5. `{"emp_code": 1, "date": 1}` (**Unique**): Guarantees at most one log per employee per day; enables atomic O(1) lookups for monthly analytics and regularizations.
6. `{"date": -1, "emp_code": 1}`: Supports reverse chronological attendance listings (`GET /attendance`) and leaderboard month range filtering without in-memory sort.
7. `{"emp_code": 1, "punch_in": -1}`: Finds an employee's open punch record during `POST /attendance/punch-out` in O(1) backward index traversal.
8. `{"status": 1, "date": -1, "emp_code": 1}`: Serves status-filtered attendance queries with reverse chronological sorting.

---

## 5. Concurrency & Race Condition Guarantees

1. **Concurrent Employee Creation**:
   Protected by the unique index `{"emp_code": 1}`. 20 concurrent threads attempting to create the same `emp_code` result in exactly one HTTP 201 Created and nineteen HTTP 409 Conflict responses.
2. **Concurrent Punch-Ins**:
   Protected by the compound unique index `{"emp_code": 1, "date": 1}`. Concurrent punches for the same employee on the same date result in exactly one HTTP 201 Created and nineteen HTTP 409 Conflict responses.
3. **Concurrent Punch-Outs**:
   Atomic `update_one` with filter `{"_id": rec["_id"], "punch_out": None}`. Only the first punch-out modifies the record; concurrent attempts detect `modified_count == 0` and return HTTP 409 Conflict.
4. **Concurrent Regularizations (PATCH)**:
   Protected via optimistic locking using MongoDB `$expr`:
   ```python
   filter = {
       "emp_code": emp_code,
       "date": date,
       "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]}
   }
   ```
   If a concurrent update occurred after the document was read, the history length check fails, modifying 0 documents and returning HTTP 409 Conflict. Zero history entries are lost.

---

## 6. Judgment Calls Where the Spec Was Silent

1. **R1 Overnight Shift Boundary**: A punch at exactly `06:00:00.000` is treated as belonging to the new day ($D$), while punches `< 06:00:00.000` map to the overnight shift of Day $D-1$.
2. **Missing Derived Values in Legacy Records**: Handled gracefully using `$ifNull` inside aggregation pipelines (`late_minutes: 0`, `overtime_minutes: 0`, `half_day: False`, `history: []`).
3. **Punched-At Default**: When `punched_at` is omitted in requests, the server uses current UTC time truncated to the whole second (`microsecond=0`).
4. **Leaderboard Ties**: Standard competition ranking (1, 2, 2, 4) is implemented via `$setWindowFields` and `$rank`. Ties at the limit boundary are both returned (responses may contain more than `limit` rows). Tied rows are ordered by `emp_code` ascending.
5. **Department Trend Gap-Free Generation**: Generated by anchoring on department employees and expanding date offsets via `$range` and `$dateAdd` in MongoDB, ensuring every calendar day appears even when a department has zero logs.
6. **Readiness Probe Resiliency**: At startup, an initial lightweight `ping` verifies MongoDB availability. If MongoDB is unreachable, index creation is skipped so Uvicorn can start and `/health` can respond with HTTP 503 within seconds, satisfying the 20-second startup requirement.

---

## 7. Running the Test Suite

All tests reside in `local_tests/` (isolated from production dependencies):

```bash
# Install test-only dependencies
pip install -r local_tests/requirements-test.txt

# Run the complete test suite (Groups A through J, 144 tests)
python -m local_tests.test_suite

# Run dedicated concurrency verification (20 threads with Barrier)
python local_tests/test_concurrency.py

# Run explain verification & performance benchmarks (100k records)
python local_tests/verify_explain.py
```
