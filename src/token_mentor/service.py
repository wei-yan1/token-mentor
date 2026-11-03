"""Backward-compatible import alias; implementation lives in orchestration.service."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("token_mentor.orchestration.service")
