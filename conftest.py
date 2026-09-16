"""Pytest configuration.

Its presence at the project root is load-bearing: pytest prepends the directory
containing the rootmost conftest.py to sys.path, which is what lets the tests
under test/ do ``import config`` and ``from protocol import packet`` exactly the
way sender.py and receiver.py do when run from the project root. Without it the
protocol package would import fine but its own ``import config`` would not.
"""
