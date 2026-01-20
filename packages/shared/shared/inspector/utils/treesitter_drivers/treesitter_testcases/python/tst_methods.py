"""Test cases for Python method definitions."""

from typing import Optional, List, Self, ClassVar
from abc import abstractmethod
import asyncio

# 1) Basic class with instance methods
class BasicClass:
    def __init__(self, value: int):
        self.value = value

    def instance_method(self) -> int:
        return self.value

    def method_with_params(self, multiplier: int, suffix: str = "!") -> str:
        return f"{self.value * multiplier}{suffix}"

# 2) Class methods and static methods
class ClassAndStatic:
    count: ClassVar[int] = 0

    def __init__(self, name: str):
        self.name = name
        ClassAndStatic.count += 1

    @classmethod
    def get_count(cls) -> int:
        return cls.count

    @classmethod
    def create_default(cls) -> "ClassAndStatic":
        return cls("default")

    @staticmethod
    def utility_function(x: int, y: int) -> int:
        return x + y

    @staticmethod
    def static_with_types(data: List[str]) -> str:
        return ",".join(data)

# 3) Property methods
class PropertyExample:
    def __init__(self, value: int):
        self._value = value
        self._computed = None

    @property
    def value(self) -> int:
        return self._value

    @value.setter
    def value(self, new_value: int) -> None:
        self._value = new_value
        self._computed = None  # Reset computed value

    @value.deleter
    def value(self) -> None:
        self._value = 0
        self._computed = None

    @property
    def computed(self) -> int:
        if self._computed is None:
            self._computed = self._value ** 2
        return self._computed

    @property
    def read_only(self) -> str:
        return f"Value is {self._value}"

# 4) Special/magic methods
class SpecialMethods:
    def __init__(self, data: List[int]):
        self.data = data

    def __str__(self) -> str:
        return f"SpecialMethods({len(self.data)} items)"

    def __repr__(self) -> str:
        return f"SpecialMethods(data={self.data})"

    def __len__(self) -> int:
        return len(self.data)

# 5) Context manager methods
class ContextManager:
    def __init__(self, resource_name: str):
        self.resource_name = resource_name
        self.resource = None

    def __enter__(self):
        print(f"Acquiring {self.resource_name}")
        self.resource = f"acquired_{self.resource_name}"
        return self.resource

    def __exit__(self, exc_type, exc_val, exc_tb):
        print(f"Releasing {self.resource_name}")
        self.resource = None
        return False  # Don't suppress exceptions

# 6) Async methods
class AsyncExample:
    def __init__(self):
        self.data = []

    async def async_method(self, delay: float) -> str:
        await asyncio.sleep(delay)
        return f"Async completed after {delay}s"

    async def async_generator(self, count: int):
        for i in range(count):
            await asyncio.sleep(0.1)
            yield i

    async def __aenter__(self):
        await asyncio.sleep(0.1)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await asyncio.sleep(0.1)
        return False

# 7) Abstract methods
from abc import ABC, abstractmethod

class AbstractBase(ABC):
    @abstractmethod
    def required_method(self) -> str:
        pass

    @abstractmethod
    def another_required(self, param: int) -> bool:
        pass

    def concrete_method(self) -> str:
        return "This is implemented"

class ConcreteImplementation(AbstractBase):
    def required_method(self) -> str:
        return "implemented"

    def another_required(self, param: int) -> bool:
        return param > 0

# 8) Methods with decorators
def method_decorator(func):
    def wrapper(self, *args, **kwargs):
        print(f"Calling {func.__name__}")
        return func(self, *args, **kwargs)
    return wrapper

class DecoratedMethods:
    @method_decorator
    def decorated_method(self) -> str:
        return "decorated"

    @staticmethod
    @method_decorator
    def decorated_static():
        return "static decorated"

    @classmethod
    @method_decorator
    def decorated_class(cls):
        return "class decorated"

# 9) Private and protected methods
class PrivateProtected:
    def public_method(self) -> str:
        return self._protected_method() + self.__private_method()

    def _protected_method(self) -> str:
        return "protected_"

    def __private_method(self) -> str:
        return "private"

# 10) Methods with complex signatures
class ComplexSignatures:
    def method_with_many_params(
        self,
        required: str,
        optional: Optional[int] = None,
        *args: str,
        keyword_only: bool = False,
        **kwargs: str
    ) -> dict:
        return {
            "required": required,
            "optional": optional,
            "args": args,
            "keyword_only": keyword_only,
            "kwargs": kwargs
        }

# 11) Methods in nested classes
class OuterClass:
    def outer_method(self) -> str:
        return "outer"

    class InnerClass:
        def inner_method(self) -> str:
            return "inner"

# 12) Free functions -- should not be extracted
def foo(x: int, y: int) -> int:
    return x + y

def bar(word: str) -> None:
    def baz(word: str) -> None:
        print(f"Hello, World! Word of the day: {word}")

    baz(word)
