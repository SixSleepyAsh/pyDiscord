from typing import Any, Dict, Type, TypeVar, Optional

T = TypeVar("T")

class Registry:
    def __init__(self) -> None:
        self._services: Dict[Type[Any], Any] = {}

    def add(self, iface: Type[T], impl: T) -> None:
        self._services[iface] = impl

    def get(self, iface: Type[T]) -> T:
        return self._services[iface]

    def try_get(self, iface: Type[T]) -> Optional[T]:
        return self._services.get(iface)
