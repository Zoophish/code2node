# Copyright (C) 2026, Sam Warren, All rights reserved.
"""
Node type universe extraction.

Introspects bpy.types to build a complete registry of every node type
available for a given tree type, including properties, enum values, and
socket signatures. Handles dynamic nodes (where sockets change based on
property values) by enumerating per-variant.

Must run inside Blender (requires bpy).
"""
import inspect
from dataclasses import dataclass, field
from typing import Any

from .core import NodeIOError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SchemaExtractionError(NodeIOError):
    """Raised when schema extraction fails for a node type."""


# ---------------------------------------------------------------------------
# Schema dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SocketDef:
    """A socket signature entry."""
    name: str
    bl_idname: str  # e.g. "NodeSocketFloat", "NodeSocketVector"
    default_value: Any = None


@dataclass
class PropertyDef:
    """A node property definition."""
    identifier: str
    type: str  # ENUM, FLOAT, INT, BOOLEAN, STRING
    default: Any = None
    enum_values: list[str] | None = None
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""


@dataclass
class NodeVariant:
    """A specific configuration of a dynamic node (one set of property values)."""
    variant_key: dict[str, str]  # e.g. {"operation": "ADD"}
    inputs: list[SocketDef] = field(default_factory=list)
    outputs: list[SocketDef] = field(default_factory=list)


@dataclass
class NodeTypeDef:
    """Complete definition of a node type."""
    bl_idname: str
    bl_label: str
    description: str = ""
    properties: list[PropertyDef] = field(default_factory=list)
    # For static nodes: single entry with empty variant_key
    # For dynamic nodes: one entry per property combination that changes sockets
    variants: list[NodeVariant] = field(default_factory=list)


@dataclass
class SchemaRegistry:
    """The complete type universe for one or more tree types."""
    tree_types: dict[str, list[NodeTypeDef]] = field(default_factory=dict)
    socket_types: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# JSON round-trip (no bpy required — usable for offline validation)
# ---------------------------------------------------------------------------

def _jsonable(val: Any) -> Any:
    """Coerce a default value to something JSON-safe; drop anything exotic
    (ID datablock references like VectorFont, Image, etc.)."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (list, tuple)):
        items = [_jsonable(v) for v in val]
        return items if all(
            i is None or isinstance(i, (bool, int, float, str)) for i in items
        ) else None
    return None


def registry_to_dict(registry: SchemaRegistry) -> dict:
    return {
        "socket_types": registry.socket_types,
        "tree_types": {
            tree_type: [
                {
                    "bl_idname": n.bl_idname,
                    "bl_label": n.bl_label,
                    "description": n.description,
                    "properties": [
                        {
                            "identifier": p.identifier,
                            "type": p.type,
                            "default": _jsonable(p.default),
                            "enum_values": p.enum_values,
                            "min_value": p.min_value,
                            "max_value": p.max_value,
                            "description": p.description,
                        }
                        for p in n.properties
                    ],
                    "variants": [
                        {
                            "variant_key": v.variant_key,
                            "inputs": [
                                {"name": s.name, "bl_idname": s.bl_idname,
                                 "default_value": _jsonable(s.default_value)}
                                for s in v.inputs
                            ],
                            "outputs": [
                                {"name": s.name, "bl_idname": s.bl_idname,
                                 "default_value": _jsonable(s.default_value)}
                                for s in v.outputs
                            ],
                        }
                        for v in n.variants
                    ],
                }
                for n in node_defs
            ]
            for tree_type, node_defs in registry.tree_types.items()
        },
    }


def registry_from_dict(data: dict) -> SchemaRegistry:
    registry = SchemaRegistry(socket_types=list(data.get("socket_types", [])))
    for tree_type, nodes in data.get("tree_types", {}).items():
        node_defs = []
        for n in nodes:
            node_defs.append(NodeTypeDef(
                bl_idname=n["bl_idname"],
                bl_label=n.get("bl_label", ""),
                description=n.get("description", ""),
                properties=[
                    PropertyDef(
                        identifier=p["identifier"],
                        type=p["type"],
                        default=p.get("default"),
                        enum_values=p.get("enum_values"),
                        min_value=p.get("min_value"),
                        max_value=p.get("max_value"),
                        description=p.get("description", ""),
                    )
                    for p in n.get("properties", [])
                ],
                variants=[
                    NodeVariant(
                        variant_key=dict(v.get("variant_key", {})),
                        inputs=[
                            SocketDef(s["name"], s["bl_idname"], s.get("default_value"))
                            for s in v.get("inputs", [])
                        ],
                        outputs=[
                            SocketDef(s["name"], s["bl_idname"], s.get("default_value"))
                            for s in v.get("outputs", [])
                        ],
                    )
                    for v in n.get("variants", [])
                ],
            ))
        registry.tree_types[tree_type] = node_defs
    return registry


def load_registry(path: str) -> SchemaRegistry:
    import json
    with open(path, "r", encoding="utf-8") as f:
        return registry_from_dict(json.load(f))


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _socket_default(socket) -> Any:
    if not hasattr(socket, "default_value"):
        return None
    val = socket.default_value
    if hasattr(val, "__len__") and not isinstance(val, str):
        return tuple(round(v, 6) if isinstance(v, float) else v for v in val)
    return round(val, 6) if isinstance(val, float) else val


def _read_sockets(collection) -> list[SocketDef]:
    return [
        SocketDef(
            name=s.name,
            bl_idname=s.bl_idname,
            default_value=_socket_default(s),
        )
        for s in collection
    ]


def _extract_properties(cls) -> list[PropertyDef]:
    """Extract non-inherited properties from a node class."""
    base_props = set()
    if cls.bl_rna.base:
        base_props = {p.identifier for p in cls.bl_rna.base.properties}

    props = []
    for prop in cls.bl_rna.properties:
        if prop.identifier in base_props or prop.is_readonly:
            continue

        pdef = PropertyDef(
            identifier=prop.identifier,
            type=prop.type,
            description=prop.description,
        )

        if prop.type == 'ENUM':
            pdef.enum_values = [item.identifier for item in prop.enum_items]
            pdef.default = prop.default
        elif prop.type in ('FLOAT', 'INT'):
            pdef.default = prop.default
            if hasattr(prop, 'hard_min'):
                pdef.min_value = prop.hard_min
            if hasattr(prop, 'hard_max'):
                pdef.max_value = prop.hard_max
        elif prop.type in ('BOOLEAN', 'STRING'):
            pdef.default = prop.default

        props.append(pdef)
    return props


def _socket_signature(node) -> tuple:
    return (
        tuple((s.name, s.bl_idname) for s in node.inputs),
        tuple((s.name, s.bl_idname) for s in node.outputs),
    )


def _probe_dynamic_properties(node, props: list[PropertyDef]) -> list[PropertyDef]:
    """Enum properties that actually change the socket layout, found by
    setting each value and watching the signature (a name heuristic misses
    e.g. noise_dimensions on Noise Texture)."""
    dynamic = []
    for prop in props:
        if not prop.enum_values:
            continue
        base_sig = _socket_signature(node)
        original = getattr(node, prop.identifier)
        changed = False
        for enum_val in prop.enum_values:
            try:
                setattr(node, prop.identifier, enum_val)
            except (TypeError, AttributeError):
                continue
            if _socket_signature(node) != base_sig:
                changed = True
                break
        try:
            setattr(node, prop.identifier, original)
        except (TypeError, AttributeError):
            pass
        if changed:
            dynamic.append(prop)
    return dynamic


# ---------------------------------------------------------------------------
# Per-node extraction (requires scratch node tree)
# ---------------------------------------------------------------------------

def _extract_node_type(bl_idname: str, scratch_tree) -> NodeTypeDef | None:
    """Instantiate a node in the scratch tree and extract its full definition."""
    try:
        node = scratch_tree.nodes.new(type=bl_idname)
    except (RuntimeError, TypeError):
        return None

    cls = type(node)
    node_def = NodeTypeDef(
        bl_idname=bl_idname,
        bl_label=cls.bl_rna.name,
        description=cls.bl_rna.description,
        properties=_extract_properties(cls),
    )

    dynamic_props = _probe_dynamic_properties(node, node_def.properties)

    if not dynamic_props:
        node_def.variants.append(NodeVariant(
            variant_key={},
            inputs=_read_sockets(node.inputs),
            outputs=_read_sockets(node.outputs),
        ))
    else:
        # Enumerate each dynamic property's enum values independently,
        # resetting it afterwards so props don't contaminate each other.
        # Deduplicate by socket signature to avoid redundant variants.
        seen_signatures: set[tuple] = set()
        for prop in dynamic_props:
            original = getattr(node, prop.identifier)
            for enum_val in prop.enum_values:
                try:
                    setattr(node, prop.identifier, enum_val)
                except (TypeError, AttributeError):
                    continue

                sig = _socket_signature(node)
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)

                node_def.variants.append(NodeVariant(
                    variant_key={prop.identifier: enum_val},
                    inputs=_read_sockets(node.inputs),
                    outputs=_read_sockets(node.outputs),
                ))
            try:
                setattr(node, prop.identifier, original)
            except (TypeError, AttributeError):
                pass

    scratch_tree.nodes.remove(node)
    return node_def


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _discover_tree_types():
    """Discover all registered NodeTree subclasses and their associated
    Node base classes from bpy.types."""
    import bpy

    tree_types = {}
    for name, cls in inspect.getmembers(bpy.types):
        if (
            inspect.isclass(cls)
            and issubclass(cls, bpy.types.NodeTree)
            and cls is not bpy.types.NodeTree
            and hasattr(cls, 'bl_rna')
        ):
            tree_types[cls.bl_rna.identifier] = cls

    return tree_types


def _all_node_classes():
    """Every registered Node subclass. Which ones belong to a given tree type
    is decided by instantiation: nodes.new raises for illegal combinations,
    and that check is authoritative — base-class naming conventions miss
    legal cross-tree types (FunctionNode*, ShaderNodeMath in geometry trees)."""
    import bpy

    return [
        (name, cls) for name, cls in inspect.getmembers(bpy.types)
        if inspect.isclass(cls)
        and issubclass(cls, bpy.types.Node)
        and hasattr(cls, 'bl_rna')
    ]


def extract_schema(tree_type_ids: list[str] | None = None) -> SchemaRegistry:
    """Extract the full node type schema for the given tree types.

    Automatically discovers all registered NodeTree subclasses if no
    specific types are requested. This includes custom tree types from
    other addons.

    Args:
        tree_type_ids: List of tree bl_idnames, e.g. ["ShaderNodeTree"].
                       Defaults to all discovered tree types.

    Must be called from within Blender.
    """
    import bpy

    all_tree_types = _discover_tree_types()

    if tree_type_ids is None:
        tree_type_ids = sorted(all_tree_types.keys())

    socket_types = sorted(
        name for name, cls in inspect.getmembers(bpy.types)
        if inspect.isclass(cls) and issubclass(cls, bpy.types.NodeSocket)
        and cls is not bpy.types.NodeSocket
    )

    registry = SchemaRegistry(socket_types=socket_types)

    for tree_type_id in tree_type_ids:
        tree_cls = all_tree_types.get(tree_type_id)
        if tree_cls is None:
            raise SchemaExtractionError(
                f"Unknown tree type '{tree_type_id}'. "
                f"Discovered types: {sorted(all_tree_types.keys())}"
            )

        node_classes = _all_node_classes()

        scratch = bpy.data.node_groups.new("_schema_scratch", tree_type_id)
        try:
            node_defs = []
            for name, cls in sorted(node_classes, key=lambda x: x[0]):
                bl_idname = cls.bl_rna.identifier
                node_def = _extract_node_type(bl_idname, scratch)
                if node_def is not None:
                    node_defs.append(node_def)
            registry.tree_types[tree_type_id] = node_defs
        finally:
            bpy.data.node_groups.remove(scratch)

    return registry
