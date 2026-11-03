"""Backward compatibility shim for apps.h3.application."""
import sys
from apps.h3 import application as _impl

sys.modules[__name__] = _impl
