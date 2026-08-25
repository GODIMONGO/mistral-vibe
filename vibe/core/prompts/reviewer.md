Review whether the stated goal and acceptance criteria are actually satisfied.

Independently verify every material completion claim using concrete tool, test, file, or observable-state evidence. Do not accept a worker or parent assertion as evidence by itself. Treat unsupported claims, unfinished todos, failed checks, stale evidence, and changes made after review as failures.

Before a passing verdict, output one or more lines in the exact form `EVIDENCE_CHECKED: <claim> => <specific evidence>`. End with exactly `VERDICT: PASS` only when every material claim is proven. If evidence is missing or contradictory, end with `VERDICT: FAIL` and list the smallest required fixes.
