"""Die Komposition: aus Notizen und Foundry-Fakten wird eine Chronik — und aus der
Chronik der Rückblick, der vor der nächsten Sitzung gelesen wird."""

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
from chronicle.compose.recap import Recap, RecapMaterial
from chronicle.compose.service import compose_session, recap_session

__all__ = [
    "Composition",
    "ModelError",
    "ModelNotConfigured",
    "ModelUnreachable",
    "OllamaClient",
    "Recap",
    "RecapMaterial",
    "SceneMaterial",
    "SessionMaterial",
    "TextModel",
    "compose",
    "compose_session",
    "recap_session",
]
