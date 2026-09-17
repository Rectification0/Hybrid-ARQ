"""Frontend: a presentation and control layer over the finished protocol (Phase 11).

It reads the recorded artifacts — ``logs/<run>/<endpoint>/events.csv``,
``summary.json``, ``experiments/results/*.csv``, ``plots/``, ``captures/`` — and
launches the *existing* CLI entry points as subprocesses. It computes no
protocol behaviour of its own: the switching policy stays in
``protocol/hybrid.py`` and the metrics come from ``metrics.py`` (design.md §13).

Deleting this package leaves the protocol, the experiments and every recorded
result exactly as they were; ``test/test_frontend.py`` demonstrates that rather
than asserting it (T11.15).
"""

__all__ = ["data", "runner", "server"]
