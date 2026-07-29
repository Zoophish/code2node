# Copyright (C) 2026, Sam Warren, All rights reserved.
"""
Cross-file tree loading.

A tree name is a datablock identity, as in Blender: defined once, referenced
by name anywhere. An import statement brings trees from another module into
the importing document's namespace — the selected trees plus their transitive
dependencies, or the whole module when no selection is given. Module paths
resolve relative to the importing file. Runs without Blender.

load(path) returns the full closure as a flat list in build order (imported
trees before the trees that reference them), deduplicated across diamond
imports. The same name from two different definitions is an error.
"""
from pathlib import Path

from . import format as fmt
from .core import NodeIOError, TreeDef


class LoadError(NodeIOError):
    pass


def _dependencies(tree: TreeDef) -> set[str]:
    """Names of trees this tree references as groups."""
    names = {n.node_tree_name for n in tree.nodes if n.node_tree_name}
    for zone in tree.zones:
        names |= {n.node_tree_name for n in zone.nodes if n.node_tree_name}
    return names


def _closure(requested: list[str], trees: list[TreeDef],
             origin: Path) -> list[TreeDef]:
    """The requested trees plus their transitive dependencies, in the
    original file order (dependencies were defined earlier in the file)."""
    by_name = {t.name: t for t in trees}
    needed: set[str] = set()
    stack = list(requested)
    while stack:
        name = stack.pop()
        if name in needed:
            continue
        tree = by_name.get(name)
        if tree is None:
            raise LoadError(
                f"{origin}: no tree named '{name}'; "
                f"has: {', '.join(t.name for t in trees)}")
        needed.add(name)
        stack.extend(_dependencies(tree) & set(by_name))
    return [t for t in trees if t.name in needed]


def _resolve_module(module: str, base_dir: Path) -> Path:
    """Resolve a dotted module path to a .nodes file. Segments map to path
    components under base_dir; each leading dot beyond the first climbs one
    directory."""
    stripped = module.lstrip('.')
    ups = len(module) - len(stripped)
    d = base_dir
    for _ in range(max(0, ups - 1)):
        d = d.parent
    segments = stripped.split('.')
    return (d.joinpath(*segments)).resolve().with_name(segments[-1] + ".nodes")


def load(path: str | Path, _cache: dict | None = None,
         _stack: tuple = ()) -> list[TreeDef]:
    path = Path(path).resolve()
    if _cache is None:
        _cache = {}
    if path in _cache:
        return _cache[path]
    if path in _stack:
        chain = " -> ".join(str(p) for p in (*_stack, path))
        raise LoadError(f"import cycle: {chain}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise LoadError(f"cannot read {path}: {e}") from e
    doc = fmt.parse_document(text)

    result: list[TreeDef] = []
    origin: dict[str, Path] = {}

    def merge(trees: list[TreeDef], source: Path):
        for tree in trees:
            existing = origin.get(tree.name)
            if existing is None:
                origin[tree.name] = source
                result.append(tree)
            elif result[[t.name for t in result].index(tree.name)] is not tree:
                raise LoadError(
                    f"tree '{tree.name}' defined in both {existing} "
                    f"and {source}; datablock names are unique — rename one")

    for imp in doc.imports:
        sub_path = _resolve_module(imp.module, path.parent)
        sub = load(sub_path, _cache, (*_stack, path))
        merge(_closure(imp.names, sub, sub_path) if imp.names else sub,
              sub_path)

    merge(doc.trees, path)

    _cache[path] = result
    return result


def flatten(path: str | Path) -> str:
    """The resolved closure as a single self-contained document."""
    return "\n\n".join(fmt.serialise(t) for t in load(path))
