"""Base database helpers for tenant-scoped persistence."""

from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
from typing import Any, Dict, Type, TypeVar

T = TypeVar("T")


def serialize_model(instance: Any) -> Dict[str, Any]:
    if not is_dataclass(instance):
        raise TypeError("serialize_model expects a dataclass instance.")
    return asdict(instance)


def deserialize_model(model_type: Type[T], payload: Dict[str, Any]) -> T:
    field_names = {field.name for field in fields(model_type)}
    filtered = {key: value for key, value in payload.items() if key in field_names}
    return model_type(**filtered)

