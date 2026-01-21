"""Test cases for Python variable definitions."""

from typing import List, Dict, Optional, Final, ClassVar
import os

# 1) Simple module-level variables
simple_var = "hello world"
number_var = 42

# 2) Constants (conventionally uppercase)
API_VERSION = "1.0.0"
MAX_CONNECTIONS = 100

# 3) Type-annotated variables
typed_string: str = "annotated"
typed_list: List[int] = [1, 2, 3, 4, 5]

# 4) Final variables (Python 3.8+)
FINAL_CONSTANT: Final[str] = "cannot be reassigned"
FINAL_NUMBER: Final = 42

# 5) Multiple assignment
a, b, c = 1, 2, 3

# 6) Module-level collections
ALLOWED_EXTENSIONS = {".py", ".pyx", ".pyi"}
ERROR_CODES = {
    404: "Not Found",
    500: "Internal Server Error",
    200: "OK"
}

# 7) Class with various variable types -- not global
class VariableExamples:
    # Class variables
    class_var: ClassVar[str] = "shared across instances"
    counter: ClassVar[int] = 0

    def __init__(self, name: str, value: int):
        # Instance variables
        self.name: str = name
        self.value: int = value
        self._private_var: Optional[str] = None
        self.__very_private: int = 42

    @property
    def computed_property(self) -> str:
        return f"{self.name}:{self.value}"

# 8) Global variables that might be modified
global_counter = 0
global_registry: List[str] = []

def modify_globals():
    global global_counter, global_registry
    global_counter += 1
    global_registry.append(f"entry_{global_counter}")

# 9) Variables in different scopes
module_scope = "module"

def function_scope_example():
    function_scope = "function"

    def nested_scope():
        nested_scope = "nested"
        nonlocal function_scope
        function_scope = "modified"

        return nested_scope, function_scope

    return nested_scope()
