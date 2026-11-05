from dotenv import load_dotenv

from paths import FOX_HOME

# Load .env once here so every adapter's os.environ.get(env_key) just works.
load_dotenv(FOX_HOME / ".env", override=True)

from .base import Image, ProviderResponse, Provider, Turn, ToolCall, ToolOutput, ToolResult, Usage
from .config import MODEL_REGISTRY, get_provider, has_vision

__all__ = [
    "Provider",
    "ProviderResponse",
    "Turn",
    "ToolCall",
    "ToolResult",
    "ToolOutput",
    "Image",
    "Usage",
    "MODEL_REGISTRY",
    "get_provider",
    "has_vision",
]
