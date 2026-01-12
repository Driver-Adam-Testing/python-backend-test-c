import argparse
import subprocess
import sys
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

import toml


def check_poetry_version() -> None:
    try:
        result = subprocess.run(
            ["poetry", "--version"], capture_output=True, text=True, check=True
        )
        version = result.stdout.strip()
        if " 2." not in version:
            raise ValueError("Poetry version 2.x is required.")
    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")
        sys.exit(1)


def locate_pyproject_files(root_dir: str) -> list[Path]:
    candidates = list(Path(root_dir).rglob("pyproject.toml"))
    filtered_candidates = [
        path
        for path in candidates
        if not any(part in path.parts for part in [".venv", "dev_stack"])
        and (path.parent / "poetry.lock").exists()
    ]
    return filtered_candidates


def extract_project_info(pyproject_path: Path) -> tuple[str, list[str]]:
    data = toml.load(pyproject_path)

    # Get project name - prefer PEP 621, fall back to Poetry
    project_name = data.get("project", {}).get("name")
    if not project_name:
        project_name = data.get("tool", {}).get("poetry", {}).get("name")
    if not project_name:
        raise KeyError(f"Project name not found in {pyproject_path}")

    # Get dependencies from both sections
    dependencies: set[str] = set()

    # PEP 621 dependencies (list of strings like "boto3>=1.0" or "database")
    pep621_deps = data.get("project", {}).get("dependencies", [])
    for dep in pep621_deps:
        # Extract package name from PEP 508 string
        dep_name = (
            dep.split("[")[0]
            .split(">")[0]
            .split("<")[0]
            .split("=")[0]
            .split("!")[0]
            .strip()
        )
        dependencies.add(dep_name)

    # Poetry dependencies (dict of name -> version)
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    for dep_name in poetry_deps:
        if dep_name.lower() != "python":
            dependencies.add(dep_name)

    return project_name, list(dependencies)


def create_dependency_graph(
    pyproject_files: list[Path],
) -> tuple[dict[str, list[str]], dict[str, Path]]:
    dependency_graph = {}
    project_name_to_project_dir = {}

    for pyproject_path in pyproject_files:
        try:
            project_name, dependencies = extract_project_info(pyproject_path)
            project_name_to_project_dir[project_name] = pyproject_path.parent
            dependency_graph[project_name] = dependencies
        except KeyError as e:
            print(f"Warning: {e} in {pyproject_path}")
            continue

    project_name_to_project_deps = {
        project: [dep for dep in deps if dep in dependency_graph]
        for project, deps in dependency_graph.items()
    }

    return project_name_to_project_deps, project_name_to_project_dir


def determine_execution_order(
    project_name_to_project_deps: dict[str, list[str]],
) -> list[str]:
    sorter = TopologicalSorter(project_name_to_project_deps)
    try:
        return list(sorter.static_order())
    except CycleError as error:
        print(f"Error: Circular dependency detected: {error}")
        sys.exit(1)


def execute_poetry_lock(
    ordered_project_names_and_dirs: list[tuple[str, Path]],
    project_name_to_project_deps: dict[str, list[str]],
    dry_run: bool,
) -> None:
    """Execute 'poetry lock' in each project directory, showing reasons for each action."""
    for project_name, project_dir in ordered_project_names_and_dirs:
        deps = project_name_to_project_deps.get(project_name, [])
        reasons_text = (
            f"depends on local projects: {', '.join(deps)}"
            if deps
            else "has no dependencies on other local projects"
        )
        action_text = "Would run" if dry_run else "Running"
        print(
            f"{action_text} `poetry lock` for `{project_name}` in `{project_dir}` because it {reasons_text}"
        )

        if not dry_run:
            try:
                subprocess.run(["poetry", "lock"], cwd=project_dir, check=True)
            except subprocess.CalledProcessError as e:
                print(
                    f"Error: Failed to run 'poetry lock' in `{project_dir}`. Error: {e}"
                )
                sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="""
        This script automates the process of running 'poetry lock' in multiple interdependent Python projects
        within a directory structure, such as in a monorepo.

        Motivation:
        When working in a monorepo, managing dependencies
        consistently across projects can be challenging, especially when these projects have interdependencies.
        For example, consider the following scenario:

        - You have four projects: A, B, C, and D.
        - Project A depends on Project B.
        - Project C depends on Project A.
        - Project D has no dependencies on the other projects.

        In this scenario, running 'poetry lock' directly in each project without considering dependencies may cause errors
        or inconsistent lock files, especially if Project A is processed before Project B. This script solves that problem
        by determining the correct order to lock dependencies based on their inter-project dependencies, ensuring that all
        prerequisite dependencies are handled first.

        Example Usage:
        - To run the script on a directory of projects:
            python script.py --root-dir /path/to/projects

        - To perform a dry run and see the plan:
            python script.py --root-dir /path/to/projects --dry-run
        """
    )

    parser.add_argument(
        "--root-dir",
        default=".",
        help="Root directory to search for pyproject.toml files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a dry run without executing commands.",
    )
    args = parser.parse_args()

    pyproject_files = locate_pyproject_files(args.root_dir)
    project_name_to_project_deps, project_name_to_project_dir = create_dependency_graph(
        pyproject_files
    )
    project_names_in_execution_order = determine_execution_order(
        project_name_to_project_deps
    )

    ordered_project_names_and_dirs = [
        (project_name, project_name_to_project_dir[project_name])
        for project_name in project_names_in_execution_order
    ]
    execute_poetry_lock(
        ordered_project_names_and_dirs,
        project_name_to_project_deps,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    check_poetry_version()
    main()
