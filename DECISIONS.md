# DECISIONS.md

1. **Indexes.**
   On `employees`: `emp_code` (unique) enforces identity; `(department, emp_code)` covers department listings; `(department, joined_on)` and `(joined_on)` back headcount filters. On `attendance_logs`: `(emp_code, date)` (unique) prevents duplicate daily punches and backs monthly queries; `(date, emp_code)` enables reverse-chronological pagination; `(emp_code, punch_in)` finds open punches; `(status, date, emp_code)` covers filtered listings. A standalone `status` index was rejected due to low cardinality (5 values).

2. **Punch-in race.**
   Both requests map to the same date and attempt `insert_one()`. The unique `(emp_code, date)` index atomically commits one insert (201 Created) and rejects the other with `DuplicateKeyError`, returning 409 Conflict.

3. **Ties.**
   The leaderboard uses `$setWindowFields` with `$rank` for competition ranking (1, 2, 2, 4). The `limit` filter (`rank <= limit`) runs after ranking, so all candidates tied at the cutoff are returned (potentially exceeding `limit`), ordered by `emp_code` ascending.

4. **Headcount.**
   The pipeline anchors on `employees` (`joined_on <= month_end`) and `$lookup`s the month's logs. Employees without attendance logs still produce documents, guaranteeing accurate headcount.

5. **100x scale change.**
   Shard `attendance_logs` on `{emp_code: "hashed", date: 1}` to distribute write throughput across nodes, and maintain daily pre-aggregated summaries to avoid scanning raw punches for multi-month analytics.
