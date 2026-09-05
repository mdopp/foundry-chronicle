"""Die Komposition: aus Notizen und Foundry-Fakten wird eine Chronik — und aus der
Chronik der Rückblick, der vor der nächsten Sitzung gelesen wird."""

from chronicle.compose.client import (
    ModelError,
    ModelNotConfigured,
    ModelUnreachable,
    OllamaClient,
    OpenAIClient,
    TextModel,
)
from chronicle.compose.composer import (
    Composition,
    Notiz,
    SceneMaterial,
    SessionMaterial,
    compose,
)
from chronicle.compose.nacherzaehlung import (
    Abschnitt,
    ErzaehlStoff,
    Nacherzaehlung,
    nacherzaehlen,
)
from chronicle.compose.recap import Recap, RecapMaterial
from chronicle.compose.service import compose_session, erzaehlen, recap_session

__all__ = [
    "Abschnitt",
    "Composition",
    "ErzaehlStoff",
    "ModelError",
    "ModelNotConfigured",
    "ModelUnreachable",
    "Nacherzaehlung",
    "Notiz",
    "OllamaClient",
    "OpenAIClient",
    "Recap",
    "RecapMaterial",
    "SceneMaterial",
    "SessionMaterial",
    "TextModel",
    "compose",
    "compose_session",
    "erzaehlen",
    "nacherzaehlen",
    "recap_session",
]
