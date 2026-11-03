"""H3 video generation application package."""
from .application import H3Application, H3JobConfig, ReferenceImage
from .characters import (
    AudioAsset,
    CharacterProfile,
    CharacterLibrary,
    VoiceReference,
    IMAGE_ID,
    AUDIO_ID,
    MENTION,
    PICTURE_MENTION,
    mention_names,
    validate_audio_asset,
    expand_characters,
    inspect_audio,
)
from .remote_h3 import DIMENSIONS, reference_prompt, uses_reference_model

__all__ = [
    'H3Application',
    'H3JobConfig',
    'ReferenceImage',
    'AudioAsset',
    'CharacterProfile',
    'CharacterLibrary',
    'VoiceReference',
    'IMAGE_ID',
    'AUDIO_ID',
    'MENTION',
    'PICTURE_MENTION',
    'mention_names',
    'validate_audio_asset',
    'expand_characters',
    'inspect_audio',
    'DIMENSIONS',
    'reference_prompt',
    'uses_reference_model',
]
