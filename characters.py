"""Backward compatibility shim for apps.h3.characters."""
import sys
from apps.h3 import characters as _impl

sys.modules[__name__] = _impl
