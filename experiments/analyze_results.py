"""Result aggregation, metrics, and graphs.

NOT YET IMPLEMENTED — scaffold only. Implemented by T9.1-T9.3.

Loads every run with pandas, aggregates per condition with across-trial spread,
and excludes integrity failures from goodput while reporting them separately.
Every metric in specs.md §20 must be computable from events.csv alone (T6.2).
"""
