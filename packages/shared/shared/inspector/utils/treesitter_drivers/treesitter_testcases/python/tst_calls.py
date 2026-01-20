"""Test cases for Python function and method calls."""

import os
import json
from pathlib import Path
from typing import List

# Test data
data = [1, 2, 3, 4, 5]
text = "hello world"
numbers = {"a": 1, "b": 2}

# 1) Simple function calls
result = len(data)
upper_text = text.upper()
joined = " ".join(["a", "b", "c"])

# 2) Built-in function calls
maximum = max(data)
minimum = min(data)
total = sum(data)
string_rep = str(42)
list_rep = list(range(10))

# 3) Method calls on objects
stripped = text.strip()
replaced = text.replace("world", "python")
path_obj = Path("test.txt")

# 4) Chained method calls
processed = text.strip().upper().replace("HELLO", "HI")
path_result = Path("some/path").parent.absolute().as_posix()

# 5) Method calls on class instances
class Example:
    def method(self, x, y=10):
        return x + y

    @classmethod
    def class_method(cls):
        return "class"

    @staticmethod
    def static_method():
        return "static"

example = Example()
instance_result = example.method(5)
class_result = Example.class_method()
static_result = Example.static_method()

# 6) Function calls in expressions
calculated = max(len(text), len(data)) + min(1, 2, 3)
conditional = print("yes") if len(data) > 0 else print("no")

# 7) Function calls with unpacking
args = [1, 2, 3]
kwargs = {"x": 10, "y": 20}
unpacked_args = max(*args)
# unpacked_kwargs = some_function(**kwargs)  # Would need a function that accepts x,y

# 8) Lambda calls
square = lambda x: x ** 2
squared_value = square(5)
immediate_lambda = (lambda x, y: x + y)(10, 20) # Not supported

# 9) Module function calls
json_string = json.dumps(numbers)
current_dir = os.getcwd()

# 10) Standard free functions
def foo() -> None:
    print("Hello, World!")

def bar(x: int, y: int) -> int:
    return x + y

foo()
bar(1, 2)

# 11) Functions spanning multiple lines
bar(
    x=1,
    y=2,
)

# 12) Function calls in control structures
for item in enumerate(data):
    processed_item = str(item)

while len(data) > 0:
    removed = data.pop()

if len(text) > 0:
    result = text.capitalize()

# 13) Function calls in class definitions
class WithCallsInDefinition:
    default_value = str(42)
    computed = len("class_name")

    def method_with_calls(self):
        return max(self.default_value, self.computed)

# 14) Async function calls (would need to be in async context)
import asyncio

async def async_example():
    await asyncio.sleep(1)
    result = await some_async_function()
    return result

async def some_async_function():
    return "async_result"

# 22) Calls to constructors
path_instance = Path("/some/path")
list_instance = list()
dict_instance = dict(a=1, b=2)

# 23) Calls with complex expressions as arguments
complex_call = max(
    len(text) if text else 0,
    sum(data) if data else 0,
    min(numbers.values()) if numbers else 0
)

# 24) Super() calls
class Parent:
    def method(self):
        return "parent"

class Child(Parent):
    def method(self):
        parent_result = super().method()
        return f"child extends {parent_result}"

# 25) Property access that looks like method calls
class PropertyExample:
    @property
    def computed_value(self):
        return len("property")

prop_example = PropertyExample()
# This is property access, not a method call: prop_example.computed_value
# But this would be: getattr(prop_example, "computed_value")
attr_value = getattr(prop_example, "computed_value")
