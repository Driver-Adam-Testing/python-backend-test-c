"""Test cases for Python import statements."""

# 1) Simple module imports
import os
import sys
import json

# 2) Import with alias
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# 3) From imports - specific items
from typing import List, Dict, Optional
from pathlib import Path
from datetime import datetime, timedelta

# 4) From import with alias
from collections import defaultdict as dd
from functools import wraps as decorator

# 5) Import all (generally discouraged but valid)
from math import *

# 6) Multiple imports on one line
import socket, threading, time

# 7) Conditional imports
try:
    import ujson as json_lib
except ImportError:
    import json as json_lib

# 8) Import with complex module paths
import xml.etree.ElementTree
from xml.etree.ElementTree import Element, SubElement
import email.mime.text as email_text

# 9) Relative imports (these would work in a package)
from . import sibling_module
from .. import parent_module
from ..utils import helper_function
from .subpackage import submodule

# 10) Future imports (should be at top in real code)
from __future__ import annotations
from __future__ import print_function, unicode_literals

# 11) Import inside function (dynamic import)
def dynamic_import_example():
    import random
    from secrets import token_hex
    return random.randint(1, 100), token_hex(8)

# 12) Import with very long module path and line continuation
from very.deeply.nested.package.subpackage.module import (
    VeryLongClassName,
    another_long_function_name,
    SOME_CONSTANT
)

# 13) Import in class
class ImportInClass:
    def method_with_import(self):
        import hashlib
        return hashlib.md5(b"test").hexdigest()

# 14) Nested try-except for imports
try:
    from fast_library import fast_function
except ImportError:
    try:
        from slow_library import fast_function
    except ImportError:
        def fast_function():
            return "fallback"

# 15) Import with getattr pattern: NOT SUPPORTED
def conditional_feature():
    import sys
    feature = getattr(sys, 'some_new_feature', None)
    return feature is not None

# 16) Import with importlib (programmatic import): NOT SUPPORTED
import importlib

def programmatic_import(module_name: str):
    module = importlib.import_module(module_name)
    return module

# 17) Star import with __all__: NOT SUPPORTED
# (This would typically be in a module that defines __all__)
__all__ = ['public_function', 'PublicClass', 'PUBLIC_CONSTANT']

def public_function():
    pass

class PublicClass:
    pass

PUBLIC_CONSTANT = "visible"

def _private_function():
    pass
