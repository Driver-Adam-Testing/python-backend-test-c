from typing import NewType

import strawberry

JSON = strawberry.scalar(
    NewType("JSON", object),
    description="The `JSON` scalar type represents JSON values as specified by ECMA-404",
    serialize=lambda v: v,
    parse_value=lambda v: v,
)


@strawberry.scalar
class NodeType(str):
    @staticmethod
    def parse_value(value: str) -> str:
        return value

    @staticmethod
    def serialize(value: str) -> str:
        return value
