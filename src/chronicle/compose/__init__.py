"""Die Komposition: aus Notizen und Foundry-Fakten wird eine Chronik."""

from chronicle.compose.client import (
    ModelError,
    ModelNotConfigured,
    ModelUnreachable,
    OllamaClient,
    TextModel,
)
from chronicle.compose.composer import (
    Composition,
    SceneMaterial,
    SessionMaterial,
    compose,
)
from chronicle.compose.service import compose_session

__all__ = [
    "Composition",
    "ModelError",
    "ModelNotConfigured",
    "ModelUnreachable",
    "OllamaClient",
    "SceneMaterial",
    "SessionMaterial",
    "TextModel",
    "compose",
    "compose_session",
]
