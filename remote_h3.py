"""Backward compatibility shim for apps.h3.remote_h3."""
import sys
from apps.h3 import remote_h3 as _impl

sys.modules[__name__] = _impl
