"""LLM 调用层的稳定公共接口。"""

from .base import LLMClient, Messages
from .factory import build_client, build_clients
from .json_parser import JSONParseError, parse_json_loose
from .providers.fake import FakeClient

__all__ = [
    "FakeClient",
    "LLMClient",
    "Messages",
    "JSONParseError",
    "build_client",
    "build_clients",
    "parse_json_loose",
]
