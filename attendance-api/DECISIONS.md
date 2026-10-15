# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` on logs: it is the race guard for punch-in and serves per-employee and monthly queries. `(date desc, emp_code)` serves the list sort and the date-range analytics. `(status, date desc, emp_code)` serves the status filter. `(emp_code, punch_in desc)` finds the record to punch out. On employees: unique `emp_code`, `(department, emp_code)`, `(department, joined_on)`, `joined_on`. I rejected a separate `late_minutes` index: the date range already narrows enough.
2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets exactly one succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no read before the write.
3. **Ties.** `$setWindowFields` with `$rank` gives 1, 2, 2, 4. I filter `rank <= limit` after ranking, so both tied employees are returned even if that exceeds `limit`; rows are sorted by minutes, then `emp_code`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end), then `$lookup`s each employee's logs. An employee with no logs gets an empty array, treated as zeros, but still counts as one in the group.
5. **100x data.** Pre-aggregate monthly numbers per employee into a summary collection updated on write, instead of scanning logs for every analytics call.
