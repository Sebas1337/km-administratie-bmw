"""JSON (de)serialisation of the model dataclasses, driven by their type hints.

Datetimes are written as UTC ISO strings (so they sort lexically), dates as ISO dates,
Decimals as strings, enums by value.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from functools import cache
from typing import Any


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value}; store timezone-aware values only")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, list | tuple):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    raise TypeError(f"cannot encode {type(value).__name__}")


def _decode(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return _decode(args[0], value) if len(args) == 1 else value
    if origin is list:
        (item,) = typing.get_args(tp)
        return [_decode(item, v) for v in value]
    if origin is tuple:
        return tuple(_decode(a, v) for a, v in zip(typing.get_args(tp), value, strict=True))
    if origin is dict or tp is dict:
        return value
    if isinstance(tp, type):
        if issubclass(tp, Enum):
            return tp(value)
        if tp is datetime:
            return datetime.fromisoformat(value)
        if tp is date:
            return date.fromisoformat(value)
        if tp is Decimal:
            return Decimal(value)
        if dataclasses.is_dataclass(tp):
            return from_dict(tp, value)
    return value


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def to_dict(obj: Any) -> dict:
    return _encode(obj)


def from_dict[T](cls: type[T], data: dict) -> T:
    """Fields missing from `data` fall back to their dataclass default, so records written
    before a field was added still load."""
    hints = _hints(cls)
    kwargs = {
        f.name: _decode(hints[f.name], data[f.name])
        for f in dataclasses.fields(cls)
        if f.name in data
    }
    return cls(**kwargs)


def dumps(obj: Any) -> str:
    return json.dumps(to_dict(obj), ensure_ascii=False, sort_keys=True)


def loads[T](cls: type[T], text: str) -> T:
    return from_dict(cls, json.loads(text))
