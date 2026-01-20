"""Test cases for Python class definitions."""

import abc
from typing import Optional, List
from dataclasses import dataclass

# 1) Simple class definition
class SimpleClass:
    pass

# 2) Class with simple inheritance
class Animal:
    def __init__(self, name: str):
        self.name = name

class Dog(Animal):
    def bark(self):
        return f"{self.name} barks!"

# 3) Class with multiple inheritance
class Mixin:
    def mixin_method(self):
        return "mixin"

class MultipleInheritance(Animal, Mixin):
    def combined_method(self):
        return f"{self.name} uses {self.mixin_method()}"

# 4) Abstract base class
class AbstractShape(abc.ABC):
    @abc.abstractmethod
    def area(self):
        pass

class Rectangle(AbstractShape):
    def __init__(self, width: float, height: float):
        self.width = width
        self.height = height

    def area(self):
        return self.width * self.height

# 5) Dataclass
@dataclass
class Point:
    x: float
    y: float
    z: Optional[float] = None

# 6) Class with decorators
@dataclass
class DecoratedClass:
    value: int

# 7) Nested classes
class OuterClass:
    class_var = "outer"

    def __init__(self):
        self.instance_var = "outer_instance"

    class InnerClass:
        inner_var = "inner"

        def __init__(self):
            self.inner_instance_var = "inner_instance"

        class DeeplyNestedClass:
            def nested_method(self):
                return "deeply nested"

# 8) Class with various method types
class MethodTypes:
    class_variable = "shared"

    def __init__(self, value):
        self.value = value

    def instance_method(self):
        return f"instance: {self.value}"

    @classmethod
    def class_method(cls):
        return f"class: {cls.class_variable}"

    @staticmethod
    def static_method():
        return "static method"

    @property
    def value_property(self):
        return self._value

    @value_property.setter
    def value_property(self, value):
        self._value = value

# 9) Class with special methods
class SpecialMethods:
    def __init__(self, data: List[int]):
        self.data = data

    def __str__(self):
        return f"SpecialMethods({self.data})"

    def __repr__(self):
        return f"SpecialMethods(data={self.data})"

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        return self.data[index]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

# 10) Generic class with type parameters
from typing import TypeVar, Generic

T = TypeVar('T')

class GenericContainer(Generic[T]):
    def __init__(self, item: T):
        self.item = item

    def get_item(self) -> T:
        return self.item

# 11) Metaclass example
class MetaClass(type):
    def __new__(cls, name, bases, namespace):
        return super().__new__(cls, name, bases, namespace)

class WithMetaclass(metaclass=MetaClass):
    def method(self):
        return "has metaclass"
