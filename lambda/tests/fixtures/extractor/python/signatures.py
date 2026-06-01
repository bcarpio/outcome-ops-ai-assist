"""Fixture: function and method signatures with varied annotations.

Exercises signature extraction across:
- bare parameters (no annotation)
- typed parameters
- typed parameters with defaults
- *args / **kwargs
- return type annotation
- methods (self + typed params)
- async functions
"""
from typing import List, Optional


def bare(a, b):
    return None


def typed(a: int, b: str) -> bool:
    return True


def typed_with_defaults(a: int, b: str = "x") -> Optional[int]:
    return None


def varargs(*args, **kwargs) -> List[int]:
    return []


class Greeter:
    def greet(self, name: str, loud: bool = False) -> str:
        return name
