"""Backward compatibility shim for apps.qwen_image.application."""
import sys
from apps.qwen_image import application as _impl

sys.modules[__name__] = _impl
