# mypy: disable_error_code="call-arg"

import strawberry
from database.models import Node, PrimaryAsset, Version, VersionNode
from sqlmodel import Session, select


class NodeTypeEnum:
    Directory = "directory"
    File = "file"
    Resource = "resource"
    Workspace = "workspace"


@strawberry.type
class FlatNode:
    id: strawberry.ID
    name: str | None
    path: str | None  # relative_path renamed to path
    kind: str | None
    children: list[str] | None = strawberry.field(default_factory=list)


def get_codebase_tree(
    version_id: str,  # version_id is now the primary identifier
    session: Session,
    organization_id: str,
) -> list[FlatNode]:
    # Perform a single query to fetch all necessary data via VersionNode
    results = session.exec(
        select(VersionNode, Node, Version, PrimaryAsset)
        .join(Node, VersionNode.node_id == Node.id)
        .join(Version, VersionNode.version_id == Version.id)
        .join(PrimaryAsset, Version.primary_asset_id == PrimaryAsset.id)
        .where(VersionNode.version_id == version_id)
        .where(PrimaryAsset.organization_id == organization_id)
    ).all()

    # If no results found, return an empty list
    if not results:
        return []

    # Construct the node tree
    directories_map = {}
    files = []

    for version_node, _, _, _ in results:
        relative_path = version_node.relative_path
        # Determine if it's a directory or file
        if relative_path.endswith("/"):
            kind = NodeTypeEnum.Directory
            name = relative_path.rstrip("/").split("/")[-1] if relative_path else ""
        else:
            kind = NodeTypeEnum.File
            name = relative_path.split("/")[-1] if relative_path else ""

        flat_node = FlatNode(
            id=strawberry.ID(str(version_node.id)),
            name=name,
            path=relative_path,
            kind=kind,
            children=[],
        )

        if kind == NodeTypeEnum.File:
            files.append(flat_node)
        else:
            directories_map[relative_path] = flat_node

    for file_node in files:
        if file_node.path and "/" in file_node.path:
            parent_path = file_node.path.rsplit("/", 1)[0] + "/"
        else:
            parent_path = ""

        if parent_path in directories_map:
            if directories_map[parent_path].children is None:
                directories_map[parent_path].children = []
            directories_map[parent_path].children.append(file_node.path)

        directories_map[file_node.path] = file_node

    result = []
    for _, dir_node in directories_map.items():
        if dir_node.children is not None:
            dir_node.children = [
                child_path
                for child_path in dir_node.children
                if child_path in directories_map
            ]
        result.append(dir_node)

    return result
