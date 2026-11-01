from dotenv import load_dotenv

from paths import FOX_HOME

# Load .env once here so every adapter's os.environ.get(env_key) just works.
load_dotenv(FOX_HOME / ".env", override=True)

from .base import ProviderResponse, Provider, Turn, ToolCall, ToolResult, Usage
from .config import MODEL_REGISTRY, get_provider

__all__ = [
    "Provider",
    "ProviderResponse",
    "Turn",
    "ToolCall",
    "ToolResult",
    "Usage",
    "MODEL_REGISTRY",
    "get_provider",
]
