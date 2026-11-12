"""Test bootstrap for the user-mode adapter: make ``gateway.*`` and the
``user`` package importable without a real Hermes checkout.

Uses the same lightweight ``tests/stubs/gateway`` fakes as the bot-mode
tests (``tests/bot/``) -- see that package's module docstring for what it
covers and why. No real Hermes checkout is required to run either suite.
"""

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TESTS_DIR = os.path.dirname(_THIS_DIR)
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_STUBS_DIR = os.path.join(_TESTS_DIR, "stubs")

# _REPO_ROOT (not the "user" directory itself) goes on sys.path, so the
# test module can do ``from user.adapter import ...`` -- a fully-qualified
# package import. A flat ``from adapter import ...`` would collide with
# ``tests/bot``'s own flat ``adapter`` module under the same name once
# both suites run in one pytest session.
for path in (_STUBS_DIR, _REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)
