"""Backward compatibility shim for apps.h3.h3_prompt_worker."""
import sys
from apps.h3 import h3_prompt_worker as _impl

sys.modules[__name__] = _impl
