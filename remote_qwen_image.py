"""Backward compatibility shim for apps.qwen_image.remote_qwen_image."""
import sys
from apps.qwen_image import remote_qwen_image as _impl

sys.modules[__name__] = _impl
