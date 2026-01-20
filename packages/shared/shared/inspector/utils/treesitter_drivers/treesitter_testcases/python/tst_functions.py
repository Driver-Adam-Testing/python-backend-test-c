""" Tests for extracting Python functions """

import asyncio
from typing import List, Optional, Callable, Iterator, Generator
from functools import wraps, lru_cache

# 1) Simple function definition
def simple_function():
    return "simple"

# 2) Function with parameters
def function_with_params(a: int, b: str, c: Optional[float] = None):
    return f"{a} {b} {c}"

# 3) Function with various parameter types
def complex_params(
    positional_only, /,
    normal_param,
    *args,
    keyword_only,
    **kwargs
):
    return (positional_only, normal_param, args, keyword_only, kwargs)

# 4) Function with type hints
def typed_function(items: List[int]) -> int:
    return sum(items)

# 5) Async function
async def async_function(delay: float) -> str:
    await asyncio.sleep(delay)
    return "async completed"

# 6) Generator function
def generator_function(n: int) -> Generator[int, None, None]:
    for i in range(n):
        yield i * 2

# 7) Function with decorators
@lru_cache(maxsize=128)
def cached_function(x: int) -> int:
    return x ** 2

def my_decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        print(f"Calling {func.__name__}")
        return func(*args, **kwargs)
    return wrapper

@my_decorator
def decorated_function(value: str) -> str:
    return value.upper()

# 8) Multiple decorators
@staticmethod
@lru_cache(maxsize=64)
def multiple_decorators(x: int, y: int) -> int:
    return x + y

# 9) Nested function definitions
def outer_function(x: int):
    def inner_function(y: int):
        def deeply_nested(z: int):
            return x + y + z
        return deeply_nested
    return inner_function

# 10) Lambda functions (assigned to variables)
simple_lambda = lambda x: x * 2
complex_lambda = lambda x, y=10: x + y if x > 0 else y

# 11) Function returning function
def function_factory(multiplier: int) -> Callable[[int], int]:
    def multiplier_func(x: int) -> int:
        return x * multiplier
    return multiplier_func

# 12) Recursive function
def fibonacci(n: int) -> int:
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)

# 13) Function with docstring
def documented_function(param: str) -> str:
    """
    This function demonstrates docstring usage.

    Args:
        param: A string parameter

    Returns:
        The processed string
    """
    return param.strip().lower()

# 14) Function with complex default arguments
def default_args_function(
    items: List[int] = None,
    mapping: dict = None,
    callback: Callable = lambda x: x
):
    if items is None:
        items = []
    if mapping is None:
        mapping = {}
    return callback(len(items) + len(mapping))

# 15) Async generator function
async def async_generator(n: int) -> Iterator[int]:
    for i in range(n):
        await asyncio.sleep(0.1)
        yield i

# 16) Function with Union and Literal types
from typing import Union, Literal

def union_function(value: Union[int, str]) -> str:
    return str(value)

def literal_function(mode: Literal["read", "write", "append"]) -> str:
    return f"Mode: {mode}"

# 17) Function with generic type parameters
from typing import TypeVar

T = TypeVar('T')

def generic_function(item: T) -> T:
    return item

# 18) Property-like function (for use as decorator)
def property_function(func):
    return property(func)

# 19) Context manager function
from contextlib import contextmanager

@contextmanager
def context_manager_function():
    print("Entering context")
    try:
        yield "resource"
    finally:
        print("Exiting context")

# 20) Function with complex annotations
def complex_annotations(
    func: Callable[[int, str], bool],
    data: List[Optional[dict]]
) -> Optional[Callable[[], None]]:
    if not data:
        return None
    return lambda: func(len(data), str(data[0]))
