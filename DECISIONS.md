# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

# DECISIONS.md

1. **Indexes.** I created indexes to support employee-code lookups, attendance filtering and pagination, and the aggregation queries. The unique attendance key prevents duplicate records for the same employee and date. I rejected indexes that would add write overhead without meaningfully improving the queries we need to support.

2. **Punch-in race.** Both requests can initially check for an existing record. The database's unique constraint must decide which insert succeeds: one request creates the record and returns `201`, while the other receives a duplicate-key conflict and returns `409`.

3. **Ties.** Employees with equal late-minute totals share the same rank. The next rank is skipped, and employees with equal totals are ordered by employee code. The limit is applied after ranking so ties at the cutoff are handled according to the API contract.

4. **Headcount.** The department summary starts from employees who have joined by the end of the requested month, rather than only employees with attendance records. It then associates attendance data, allowing employees with no logs to contribute to headcount.

5. **Scaling.** For 100 times the data, I would measure query plans and execution statistics, optimize indexes based on real query patterns, and review aggregation costs. I would also consider archiving older records if the retention requirements allowed it.