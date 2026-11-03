"""Backward compatibility shim for apps.h3.h3_prompt."""
import sys
from apps.h3 import h3_prompt as _impl

sys.modules[__name__] = _impl
