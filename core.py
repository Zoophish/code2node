# Copyright (C) 2026, Sam Warren, All rights reserved.
"""
Core intermediate representation for Blender node trees.

Socket references use indexes throughout. Names are optional annotations
for readability and validation, but the index is the structural identifier.
"""
import collections
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class NodeIOError(Exception):
    pass

class SerialisationError(NodeIOError):
    pass

class DeserialisationError(NodeIOError):
    pass


# ---------------------------------------------------------------------------
# Socket reference
# ---------------------------------------------------------------------------

@dataclass
class SocketRef:
    """A reference to a specific socket on a node."""
    node: str
    index: int
    name: str = ""


# ---------------------------------------------------------------------------
# IR
# ---------------------------------------------------------------------------

@dataclass
class LinkDef:
    source: SocketRef
    target: SocketRef


@dataclass
class NodeItemDef:
    """A declared item on an item-bearing node or zone: a socket the document
    itself brings into existence, named and typed by the author."""
    name: str
    socket_type: str  # full idname, e.g. NodeSocketFloat


# Item-bearing node types: sockets come from per-instance item lists rather
# than the type signature. Maps bl_idname -> (input offset, output offset),
# the socket index where item n starts (fixed sockets precede items, the
# virtual extend socket trails them). Item n sits at offset + n, so declared
# items carry canonical indices without a schema lookup.
ITEM_NODES = {
    'NodeEvaluateClosure': (1, 0),  # input 0 is the Closure socket
}


@dataclass
class NodeDef:
    name: str
    bl_idname: str
    label: str = ""
    location: tuple[float, float] = (0.0, 0.0)
    width: float = 0.0
    height: float = 0.0
    mute: bool = False
    hide: bool = False
    properties: dict[str, Any] = field(default_factory=dict)
    input_values: dict[int, Any] = field(default_factory=dict)
    input_names: dict[int, str] = field(default_factory=dict)
    # Name-keyed values awaiting schema resolution (validate.resolve_names
    # moves them into input_values once the index is known).
    named_input_values: dict[str, Any] = field(default_factory=dict)
    output_values: dict[int, Any] = field(default_factory=dict)
    output_names: dict[int, str] = field(default_factory=dict)
    node_tree_name: str | None = None
    children: list[str] = field(default_factory=list)
    # Declared items for ITEM_NODES types.
    input_items: list[NodeItemDef] = field(default_factory=list)
    output_items: list[NodeItemDef] = field(default_factory=list)


@dataclass
class InterfaceSocketDef:
    name: str
    socket_type: str
    direction: str  # "INPUT" or "OUTPUT"
    default: Any = None


@dataclass
class RepeatItemDef:
    name: str
    socket_type: str
    default_value: Any = None
    initial_connection: tuple[str, int, str] | None = None  # (node, index, name)


@dataclass
class ZoneDef:
    """A paired-node zone: a named block of inner nodes and links. Inner
    nodes read the zone's inputs from a pseudo-node (one per zone kind);
    `output_mappings` wires inner outputs back to the zone's output node.
    External nodes read the zone's outputs via the zone name."""
    name: str
    location: tuple[float, float] = (0.0, 0.0)
    nodes: list[NodeDef] = field(default_factory=list)
    links: list[LinkDef] = field(default_factory=list)
    output_mappings: dict[str, tuple[str, int, str]] = field(default_factory=dict)


@dataclass
class RepeatZoneDef(ZoneDef):
    """Carried items loop between iterations; pseudo-node `"repeat"`."""
    items: list[RepeatItemDef] = field(default_factory=list)
    iterations: int | tuple[str, int, str] = 1  # literal int or (node, index, name) connection


@dataclass
class ClosureZoneDef(ZoneDef):
    """A closure value: separate input and output item lists, as a tree
    interface. Pseudo-node `"closure"`; the zone name resolves externally to
    the single Closure output socket."""
    inputs: list[NodeItemDef] = field(default_factory=list)
    outputs: list[NodeItemDef] = field(default_factory=list)


@dataclass
class ImportDef:
    """An import statement: a dotted module path plus selected tree names."""
    module: str  # leading dots climb directories from the importing file
    names: list[str] = field(default_factory=list)  # empty = every tree


@dataclass
class TreeDef:
    name: str
    bl_idname: str
    interface: list[InterfaceSocketDef] = field(default_factory=list)
    nodes: list[NodeDef] = field(default_factory=list)
    links: list[LinkDef] = field(default_factory=list)
    zones: list[ZoneDef] = field(default_factory=list)  # repeat and closure, in file order
    groups: dict[str, "TreeDef"] = field(default_factory=dict)


@dataclass
class DocumentDef:
    """One parsed .nodes file: import statements plus its own trees."""
    imports: list[ImportDef] = field(default_factory=list)
    trees: list[TreeDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _socket_value(socket) -> Any:
    if not hasattr(socket, "default_value"):
        return None
    val = socket.default_value
    if hasattr(val, "__len__") and not isinstance(val, str):
        return tuple(val)
    return val


@dataclass
class _Defaults:
    width: float = 0.0
    height: float = 0.0
    socket_values: dict[int, Any] = field(default_factory=dict)
    output_values: dict[int, Any] = field(default_factory=dict)


class _Extractor:
    """Walks bpy node trees into IR, caching factory defaults per node type."""

    def __init__(self, scratch_tree, verbose: bool = False):
        self._scratch = scratch_tree
        self._cache: dict = {}
        self._verbose = verbose

    def extract_node(self, node, linked_indices: set[tuple[str, int]]) -> NodeDef:
        properties = self._extract_properties(node)
        defaults = self._get_defaults(node.bl_idname, properties)

        actual_w = round(node.width, 1)
        actual_h = round(node.height, 1)

        node_def = NodeDef(
            name=node.name,
            bl_idname=node.bl_idname,
            label=node.label,
            location=(round(node.location.x, 1), round(node.location.y, 1)),
            width=actual_w if actual_w != defaults.width else 0.0,
            height=actual_h if actual_h != defaults.height else 0.0,
            mute=node.mute,
            hide=node.hide,
            properties=properties,
        )

        for i, socket in enumerate(node.inputs):
            if (node.name, i) in linked_indices:
                continue
            val = _socket_value(socket)
            if val is None:
                continue
            if not self._verbose:
                factory_val = defaults.socket_values.get(i)
                if factory_val is not None and val == factory_val:
                    continue
            node_def.input_values[i] = val
            node_def.input_names[i] = socket.name

        for i, socket in enumerate(node.outputs):
            val = _socket_value(socket)
            if val is None:
                continue
            if not self._verbose:
                factory_val = defaults.output_values.get(i)
                if factory_val is not None and val == factory_val:
                    continue
            node_def.output_values[i] = val
            node_def.output_names[i] = socket.name

        if hasattr(node, "node_tree") and node.node_tree is not None:
            node_def.node_tree_name = node.node_tree.name

        if node.bl_idname in ITEM_NODES:
            in_off, out_off = ITEM_NODES[node.bl_idname]
            # Socket bl_idname read from the live socket at the item's index —
            # items store short enum identifiers, the DSL uses full idnames.
            for i, item in enumerate(node.input_items):
                node_def.input_items.append(NodeItemDef(
                    item.name, node.inputs[in_off + i].bl_idname))
            for i, item in enumerate(node.output_items):
                node_def.output_items.append(NodeItemDef(
                    item.name, node.outputs[out_off + i].bl_idname))

        return node_def

    def _extract_properties(self, node) -> dict[str, Any]:
        if not hasattr(type(node), 'bl_rna') or type(node).bl_rna.base is None:
            return {}
        props = {}
        base_props = {p.identifier for p in type(node).bl_rna.base.properties}
        for prop in node.bl_rna.properties:
            if prop.identifier in base_props or prop.is_readonly:
                continue
            try:
                val = getattr(node, prop.identifier)
            except AttributeError:
                continue
            if hasattr(prop, 'default') and val == prop.default:
                continue
            if prop.type == 'ENUM':
                props[prop.identifier] = val
            elif prop.type == 'FLOAT':
                props[prop.identifier] = round(val, 6) if isinstance(val, float) else val
            elif prop.type in ('INT', 'BOOLEAN', 'STRING'):
                props[prop.identifier] = val
        return props

    def _get_defaults(self, bl_idname: str, properties: dict) -> _Defaults:
        cache_key = (bl_idname, tuple(sorted(properties.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            ref = self._scratch.nodes.new(type=bl_idname)
        except RuntimeError:
            result = _Defaults()
            self._cache[cache_key] = result
            return result
        for key, val in properties.items():
            try:
                setattr(ref, key, val)
            except (AttributeError, TypeError):
                pass
        sv = {}
        for i, socket in enumerate(ref.inputs):
            v = _socket_value(socket)
            if v is not None:
                sv[i] = v
        ov = {}
        for i, socket in enumerate(ref.outputs):
            v = _socket_value(socket)
            if v is not None:
                ov[i] = v
        result = _Defaults(
            width=round(ref.width, 1),
            height=round(ref.height, 1),
            socket_values=sv,
            output_values=ov,
        )
        self._scratch.nodes.remove(ref)
        self._cache[cache_key] = result
        return result


def _get_linked_indices(node_tree) -> set[tuple[str, int]]:
    """Return set of (node_name, input_index) for all linked input sockets."""
    linked = set()
    for link in node_tree.links:
        if link.is_valid:
            # Find the index of the target socket
            for i, socket in enumerate(link.to_node.inputs):
                if socket == link.to_socket:
                    linked.add((link.to_node.name, i))
                    break
    return linked


def _extract_links(node_tree) -> list[LinkDef]:
    links = []
    for link in node_tree.links:
        if not link.is_valid:
            continue
        # Find output index
        from_idx = 0
        for i, s in enumerate(link.from_node.outputs):
            if s == link.from_socket:
                from_idx = i
                break
        # Find input index
        to_idx = 0
        for i, s in enumerate(link.to_node.inputs):
            if s == link.to_socket:
                to_idx = i
                break
        links.append(LinkDef(
            source=SocketRef(link.from_node.name, from_idx, link.from_socket.name),
            target=SocketRef(link.to_node.name, to_idx, link.to_socket.name),
        ))
    return links


def _extract_interface(node_tree) -> list[InterfaceSocketDef]:
    if not hasattr(node_tree, 'interface'):
        return []
    return [
        InterfaceSocketDef(
            name=item.name, socket_type=item.socket_type, direction=item.in_out,
            default=_socket_value(item),
        )
        for item in node_tree.interface.items_tree
        if item.item_type == 'SOCKET'
    ]


# ---------------------------------------------------------------------------
# Serialisation: zone extraction (repeat, closure)
# ---------------------------------------------------------------------------

REPEAT_INPUT_TYPES = {'ShaderNodeRepeatInput', 'GeometryNodeRepeatInput'}
CLOSURE_INPUT_TYPES = {'NodeClosureInput'}


def _find_zone_pairs(bpy_tree, input_types: set[str]) -> dict[str, str]:
    pairs = {}
    for node in bpy_tree.nodes:
        if node.bl_idname in input_types:
            if hasattr(node, 'paired_output') and node.paired_output is not None:
                pairs[node.name] = node.paired_output.name
    return pairs


def _find_inner_nodes(input_name: str, output_name: str, links: list[LinkDef]) -> set[str]:
    adjacency: dict[str, list[str]] = {}
    for link in links:
        adjacency.setdefault(link.source.node, []).append(link.target.node)
    inner = set()
    queue = collections.deque(adjacency.get(input_name, []))
    while queue:
        name = queue.popleft()
        if name in inner or name == input_name or name == output_name:
            continue
        inner.add(name)
        for next_name in adjacency.get(name, []):
            queue.append(next_name)
    return inner


def _classify_zone_links(links: list[LinkDef], input_name: str, output_name: str,
                         zone_names: set[str], zone_name: str, pseudo: str,
                         src_offset: int):
    """Split a tree's links around one zone. Returns (inner_links,
    remaining_links, output_mappings). Inner reads from the zone's input node
    become pseudo-node refs with item-relative indices (src_offset skips
    fixed sockets before the items); writes into the output node become
    output mappings; links leaving the zone read from the zone name."""
    inner_links: list[LinkDef] = []
    remaining_links: list[LinkDef] = []
    output_mappings: dict[str, tuple[str, int, str]] = {}

    for link in links:
        src, tgt = link.source, link.target
        from_in = src.node in zone_names
        to_in = tgt.node in zone_names

        if from_in and to_in:
            src_index = src.index - src_offset if src.node == input_name else src.index
            src_node = pseudo if src.node == input_name else src.node
            if tgt.node == output_name:
                output_mappings[tgt.name] = (src_node, src_index, src.name)
            else:
                inner_links.append(LinkDef(
                    source=SocketRef(src_node, src_index, src.name),
                    target=SocketRef(tgt.node, tgt.index, tgt.name),
                ))
        elif to_in and not from_in:
            if tgt.node == output_name:
                output_mappings[tgt.name] = (src.node, src.index, src.name)
            else:
                inner_links.append(link)
        elif from_in and not to_in:
            remaining_links.append(LinkDef(
                source=SocketRef(zone_name, src.index, src.name),
                target=tgt,
            ))
        else:
            remaining_links.append(link)

    return inner_links, remaining_links, output_mappings


def _extract_repeat_zones(bpy_tree, tree_def: TreeDef):
    pairs = _find_zone_pairs(bpy_tree, REPEAT_INPUT_TYPES)
    if not pairs:
        return

    node_by_name = {n.name: n for n in tree_def.nodes}

    for input_name, output_name in pairs.items():
        input_def = node_by_name.get(input_name)
        if input_def is None:
            continue

        bpy_input = bpy_tree.nodes[input_name]
        bpy_output = bpy_tree.nodes[output_name]

        # Extract items and detect initial connections from links
        items = []
        for item in bpy_output.repeat_items:
            socket = bpy_input.inputs.get(item.name)
            socket_type = socket.bl_idname if socket is not None else item.socket_type
            default = _socket_value(socket) if socket is not None else None
            items.append(RepeatItemDef(name=item.name, socket_type=socket_type, default_value=default))

        # Iterations — literal or connected
        iterations = 1
        iter_socket = bpy_input.inputs.get("Iterations")
        if iter_socket is not None:
            iter_val = _socket_value(iter_socket)
            if iter_val is not None:
                iterations = iter_val

        # Find inner nodes
        inner_names = _find_inner_nodes(input_name, output_name, tree_def.links)
        zone_names = inner_names | {input_name, output_name}

        zone_name = input_name.replace("Repeat Input", "Repeat").strip() or "Repeat"
        item_names = {item.name for item in items}

        # Header links read from outside the zone: the iteration count and
        # each item's initial value target the input node directly.
        body_links = []
        for link in tree_def.links:
            src, tgt = link.source, link.target
            if tgt.node == input_name and tgt.name == "Iterations":
                iterations = (src.node, src.index, src.name)
                continue
            if tgt.node == input_name and tgt.name in item_names and src.node not in zone_names:
                for item in items:
                    if item.name == tgt.name:
                        item.initial_connection = (src.node, src.index, src.name)
                        break
                continue
            body_links.append(link)

        # Repeat input outputs: [Iteration, item0, item1, ...] — offset 1.
        inner_links, remaining_links, output_mappings = _classify_zone_links(
            body_links, input_name, output_name, zone_names, zone_name,
            "repeat", 1)

        inner_node_defs = [n for n in tree_def.nodes if n.name in inner_names]

        tree_def.zones.append(RepeatZoneDef(
            name=zone_name,
            location=input_def.location,
            items=items,
            iterations=iterations,
            nodes=inner_node_defs,
            links=inner_links,
            output_mappings=output_mappings,
        ))

        tree_def.nodes = [n for n in tree_def.nodes if n.name not in zone_names]
        tree_def.links = remaining_links


def _extract_closure_zones(bpy_tree, tree_def: TreeDef):
    pairs = _find_zone_pairs(bpy_tree, CLOSURE_INPUT_TYPES)
    if not pairs:
        return

    node_by_name = {n.name: n for n in tree_def.nodes}

    for input_name, output_name in pairs.items():
        input_def = node_by_name.get(input_name)
        if input_def is None:
            continue

        bpy_input = bpy_tree.nodes[input_name]
        bpy_output = bpy_tree.nodes[output_name]

        # Items live on the output node; input items surface as the input
        # node's outputs, output items as the output node's inputs. Socket
        # bl_idnames come from the live sockets (items store short enums).
        zone_inputs = [NodeItemDef(item.name, bpy_input.outputs[i].bl_idname)
                       for i, item in enumerate(bpy_output.input_items)]
        zone_outputs = [NodeItemDef(item.name, bpy_output.inputs[i].bl_idname)
                        for i, item in enumerate(bpy_output.output_items)]

        inner_names = _find_inner_nodes(input_name, output_name, tree_def.links)
        zone_names = inner_names | {input_name, output_name}
        zone_name = input_name.replace("Closure Input", "Closure").strip() or "Closure"

        # Closure input outputs: [item0, item1, ...] — no fixed sockets.
        inner_links, remaining_links, output_mappings = _classify_zone_links(
            tree_def.links, input_name, output_name, zone_names, zone_name,
            "closure", 0)

        inner_node_defs = [n for n in tree_def.nodes if n.name in inner_names]

        tree_def.zones.append(ClosureZoneDef(
            name=zone_name,
            location=input_def.location,
            inputs=zone_inputs,
            outputs=zone_outputs,
            nodes=inner_node_defs,
            links=inner_links,
            output_mappings=output_mappings,
        ))

        tree_def.nodes = [n for n in tree_def.nodes if n.name not in zone_names]
        tree_def.links = remaining_links


# ---------------------------------------------------------------------------
# Serialisation: frame extraction
# ---------------------------------------------------------------------------

def _collect_frame_children(bpy_tree, tree_def: TreeDef):
    frame_defs = {n.name: n for n in tree_def.nodes if n.bl_idname == 'NodeFrame'}
    for bpy_node in bpy_tree.nodes:
        if bpy_node.parent is not None and bpy_node.parent.name in frame_defs:
            frame_defs[bpy_node.parent.name].children.append(bpy_node.name)


# ---------------------------------------------------------------------------
# Serialisation: top-level
# ---------------------------------------------------------------------------

def tree_to_ir(node_tree, verbose: bool = False) -> TreeDef:
    import bpy

    if node_tree is None:
        raise SerialisationError("Cannot serialise None node tree")

    scratch = bpy.data.node_groups.new("_nodeio_scratch", node_tree.bl_idname)
    extractor = _Extractor(scratch, verbose=verbose)

    try:
        visited: set[str] = set()
        all_trees: dict[str, TreeDef] = {}
        queue = collections.deque([node_tree])
        root_name = node_tree.name

        while queue:
            current_tree = queue.popleft()
            if current_tree.name in visited:
                continue
            visited.add(current_tree.name)

            linked = _get_linked_indices(current_tree)
            tree_def = TreeDef(
                name=current_tree.name,
                bl_idname=current_tree.bl_idname,
                interface=_extract_interface(current_tree),
            )

            for node in current_tree.nodes:
                tree_def.nodes.append(extractor.extract_node(node, linked))
                if (
                    hasattr(node, "node_tree")
                    and node.node_tree is not None
                    and node.node_tree.name not in visited
                ):
                    queue.append(node.node_tree)

            tree_def.links = _extract_links(current_tree)

            _collect_frame_children(current_tree, tree_def)
            _extract_repeat_zones(current_tree, tree_def)
            _extract_closure_zones(current_tree, tree_def)

            all_trees[current_tree.name] = tree_def
    finally:
        bpy.data.node_groups.remove(scratch)

    root_def = all_trees[root_name]
    for name, td in all_trees.items():
        if name != root_name:
            _attach_group(root_def, name, td)

    return root_def


def _attach_group(root: TreeDef, group_name: str, group_def: TreeDef):
    queue = collections.deque([root])
    visited: set[str] = set()
    while queue:
        td = queue.popleft()
        if td.name in visited:
            continue
        visited.add(td.name)
        for node_def in td.nodes:
            if node_def.node_tree_name == group_name:
                td.groups[group_name] = group_def
                return
        for child in td.groups.values():
            queue.append(child)


# ---------------------------------------------------------------------------
# Deserialisation helpers
# ---------------------------------------------------------------------------

def _apply_properties(node, properties: dict[str, Any]) -> list[str]:
    warnings = []
    for key, val in properties.items():
        if not hasattr(node, key):
            warnings.append(f"Node '{node.name}' ({node.bl_idname}): skipped unknown property '{key}'")
            continue
        try:
            setattr(node, key, val)
        except (TypeError, ValueError) as e:
            warnings.append(f"Node '{node.name}': cannot set property '{key}': {e}")
    return warnings


def _apply_input_values(node, input_values: dict[int, Any]):
    for idx, val in input_values.items():
        if idx >= len(node.inputs):
            continue
        socket = node.inputs[idx]
        if not hasattr(socket, "default_value"):
            continue
        try:
            socket.default_value = val
        except (TypeError, ValueError):
            continue


def _apply_output_values(node, output_values: dict[int, Any]):
    for idx, val in output_values.items():
        if idx >= len(node.outputs):
            continue
        socket = node.outputs[idx]
        if not hasattr(socket, "default_value"):
            continue
        try:
            socket.default_value = val
        except (TypeError, ValueError):
            continue


def _build_node(node_def: NodeDef, node_tree, existing_trees: dict) -> tuple:
    try:
        node = node_tree.nodes.new(type=node_def.bl_idname)
    except RuntimeError as e:
        raise DeserialisationError(f"Cannot create node type '{node_def.bl_idname}': {e}") from e

    node.name = node_def.name
    node.label = node_def.label
    if node_def.width:
        node.width = node_def.width
    if node_def.height:
        node.height = node_def.height
    node.mute = node_def.mute
    node.hide = node_def.hide

    warnings = _apply_properties(node, node_def.properties)

    if node_def.node_tree_name and hasattr(node, "node_tree"):
        ref = existing_trees.get(node_def.node_tree_name)
        if ref is None:
            raise DeserialisationError(
                f"Node '{node_def.name}' references unknown node tree '{node_def.node_tree_name}'"
            )
        node.node_tree = ref

    if node_def.input_items or node_def.output_items:
        if not hasattr(node, 'input_items'):
            raise DeserialisationError(
                f"Node '{node_def.name}' ({node_def.bl_idname}) declares "
                "items but the node type has no item lists"
            )
        for item_def in node_def.input_items:
            node.input_items.new(
                _short_socket_type(item_def.socket_type), item_def.name)
        for item_def in node_def.output_items:
            node.output_items.new(
                _short_socket_type(item_def.socket_type), item_def.name)

    if node_def.named_input_values:
        raise DeserialisationError(
            f"Node '{node_def.name}' has name-based input values "
            f"({', '.join(node_def.named_input_values)}); resolve against a "
            "schema registry first (code2node.validate.resolve_names)"
        )
    _apply_input_values(node, node_def.input_values)
    _apply_output_values(node, node_def.output_values)
    return node, warnings


def _create_links(link_defs: list[LinkDef], node_map: dict, node_tree) -> list[str]:
    for link_def in link_defs:
        src = node_map.get(link_def.source.node)
        tgt = node_map.get(link_def.target.node)
        if link_def.source.index < 0 or link_def.target.index < 0:
            raise DeserialisationError(
                f"Link {link_def.source.node} -> {link_def.target.node} uses a "
                "name-based socket ref; resolve against a schema registry first "
                "(code2node.validate.resolve_names)"
            )
        if src is None:
            raise DeserialisationError(f"Link references unknown source node '{link_def.source.node}'")
        if tgt is None:
            raise DeserialisationError(f"Link references unknown target node '{link_def.target.node}'")
        if link_def.source.index >= len(src.outputs):
            raise DeserialisationError(
                f"Node '{link_def.source.node}' has no output at index {link_def.source.index}"
            )
        if link_def.target.index >= len(tgt.inputs):
            raise DeserialisationError(
                f"Node '{link_def.target.node}' has no input at index {link_def.target.index}"
            )
        node_tree.links.new(src.outputs[link_def.source.index], tgt.inputs[link_def.target.index])

    # A link into a socket a node property has disabled (e.g. Sample
    # Curve's Curve Index under use_all_curves) either vanishes or stays in
    # the tree dead (is_valid False, socket disabled) — silently inert
    # either way. Verify every requested link exists and is live.
    present: dict = {}
    for link in node_tree.links:
        key = (link.from_node.name, link.from_socket.identifier,
               link.to_node.name, link.to_socket.identifier)
        present.setdefault(key, []).append(link)
    warnings = []
    for link_def in link_defs:
        src = node_map[link_def.source.node]
        tgt = node_map[link_def.target.node]
        sock = tgt.inputs[link_def.target.index]
        key = (src.name, src.outputs[link_def.source.index].identifier,
               tgt.name, sock.identifier)
        where = (f"Link '{link_def.source.node}'({link_def.source.index}) -> "
                 f"'{link_def.target.node}'({link_def.target.index}: "
                 f"'{sock.name}')")
        matches = present.get(key) or []
        if not matches:
            warnings.append(f"{where} was dropped by Blender — the target "
                            "socket is likely disabled by a node property")
            continue
        link = matches.pop()
        if not link.is_valid or not link.to_socket.enabled \
                or not link.from_socket.enabled:
            warnings.append(f"{where} is dead: the socket is disabled by a "
                            "node property, so the link has no effect")
    return warnings


def _set_frame_parents(tree_def: TreeDef, node_map: dict):
    for node_def in tree_def.nodes:
        if node_def.children and node_def.name in node_map:
            frame = node_map[node_def.name]
            for child_name in node_def.children:
                child = node_map.get(child_name)
                if child is not None:
                    child.parent = frame


# Item collections take short enum identifiers ('FLOAT'); the DSL uses full
# socket idnames. Subtypes (NodeSocketFloatFactor, NodeSocketVectorXYZ, ...)
# exist only on interfaces — item enums know the base types, so subtypes
# collapse to theirs.
_SHORT_TYPE_IRREGULAR = {'NodeSocketBool': 'BOOLEAN', 'NodeSocketColor': 'RGBA'}
_ITEM_BASE_TYPES = ('Float', 'Int', 'Vector', 'Rotation', 'Matrix', 'String',
                    'Menu', 'Object', 'Image', 'Geometry', 'Collection',
                    'Material', 'Texture', 'Bundle', 'Closure', 'Shader')


def _short_socket_type(socket_idname: str) -> str:
    if socket_idname in _SHORT_TYPE_IRREGULAR:
        return _SHORT_TYPE_IRREGULAR[socket_idname]
    if not socket_idname.startswith('NodeSocket'):
        return socket_idname
    rest = socket_idname[len('NodeSocket'):]
    for base in _ITEM_BASE_TYPES:
        if rest.startswith(base):
            return base.upper()
    return rest.upper()


def _build_zone_body(zone_def: ZoneDef, pseudo: str, src_offset: int,
                     input_node, output_node, node_tree, node_map: dict,
                     existing_trees: dict) -> list[str]:
    """Inner nodes, inner links, and output mappings — the part every zone
    kind shares. `pseudo` refs read the input node's outputs at index +
    src_offset (fixed sockets precede the items there)."""
    warnings: list[str] = []

    for node_def in zone_def.nodes:
        node, w = _build_node(node_def, node_tree, existing_trees)
        warnings.extend(w)
        node_map[node_def.name] = node

    scope = dict(node_map)
    scope[pseudo] = input_node

    def source_socket(node_name: str, index: int, where: str):
        src = scope.get(node_name)
        if src is None:
            raise DeserialisationError(
                f"Zone '{zone_def.name}': {where} references unknown node '{node_name}'")
        if index < 0:
            raise DeserialisationError(
                f"Zone '{zone_def.name}': {where} uses a name-based socket ref; "
                "resolve against a schema registry first (code2node.validate.resolve_names)")
        idx = index + src_offset if node_name == pseudo else index
        if idx >= len(src.outputs):
            raise DeserialisationError(
                f"Zone '{zone_def.name}': output index {idx} "
                f"out of range on '{node_name}' (has {len(src.outputs)} outputs)")
        return src.outputs[idx]

    for link_def in zone_def.links:
        from_s = source_socket(link_def.source.node, link_def.source.index, "link")
        tgt = scope.get(link_def.target.node)
        if tgt is None:
            raise DeserialisationError(
                f"Zone '{zone_def.name}': link references unknown node "
                f"'{link_def.target.node}'")
        if link_def.target.index < 0:
            raise DeserialisationError(
                f"Zone '{zone_def.name}': link uses a name-based socket ref; "
                "resolve against a schema registry first (code2node.validate.resolve_names)")
        if link_def.target.index >= len(tgt.inputs):
            raise DeserialisationError(
                f"Zone '{zone_def.name}': input index {link_def.target.index} "
                f"out of range on '{link_def.target.node}' (has {len(tgt.inputs)} inputs)")
        node_tree.links.new(from_s, tgt.inputs[link_def.target.index])

    for item_name, (src_name, src_idx, _) in zone_def.output_mappings.items():
        from_s = source_socket(src_name, src_idx, f"output '{item_name}'")
        to_s = output_node.inputs.get(item_name)
        if to_s is None:
            raise DeserialisationError(
                f"Zone '{zone_def.name}': output mapping names "
                f"undeclared item '{item_name}'")
        node_tree.links.new(from_s, to_s)

    return warnings


def _repeat_pair_types(tree_idname: str) -> tuple[str, str]:
    """Repeat zone nodes are per-tree-type variants; closures are not."""
    prefix = 'ShaderNode' if tree_idname.startswith('ShaderNode') else 'GeometryNode'
    return prefix + 'RepeatInput', prefix + 'RepeatOutput'


def _build_repeat_zone(zone_def: RepeatZoneDef, node_tree, node_map: dict,
                       existing_trees: dict) -> list[str]:
    input_type, output_type = _repeat_pair_types(node_tree.bl_idname)
    input_node = node_tree.nodes.new(input_type)
    output_node = node_tree.nodes.new(output_type)
    input_node.pair_with_output(output_node)
    # Extraction derives the zone name from the input node's name; naming it
    # after the zone makes the name survive a bpy roundtrip.
    input_node.name = zone_def.name
    input_node.location = zone_def.location

    # The output node arrives with a default Geometry item; the document's
    # item list is authoritative, and a leftover default shifts every index.
    output_node.repeat_items.clear()
    for item_def in zone_def.items:
        output_node.repeat_items.new(
            _short_socket_type(item_def.socket_type), item_def.name)

    # Set iterations
    if isinstance(zone_def.iterations, int):
        iter_socket = input_node.inputs.get("Iterations")
        if iter_socket and hasattr(iter_socket, 'default_value'):
            try:
                iter_socket.default_value = zone_def.iterations
            except (TypeError, ValueError):
                pass
    elif isinstance(zone_def.iterations, tuple):
        src_node_name, src_idx, _ = zone_def.iterations
        src = node_map.get(src_node_name)
        if src and 0 <= src_idx < len(src.outputs):
            iter_socket = input_node.inputs.get("Iterations")
            if iter_socket:
                node_tree.links.new(src.outputs[src_idx], iter_socket)

    # Set item initial values and connections
    for item_def in zone_def.items:
        socket = input_node.inputs.get(item_def.name)
        if socket is None:
            continue
        if item_def.initial_connection is not None:
            src_node_name, src_idx, _ = item_def.initial_connection
            src = node_map.get(src_node_name)
            if src and 0 <= src_idx < len(src.outputs):
                node_tree.links.new(src.outputs[src_idx], socket)
        elif item_def.default_value is not None and hasattr(socket, 'default_value'):
            try:
                socket.default_value = item_def.default_value
            except (TypeError, ValueError):
                pass

    node_map[zone_def.name] = output_node

    # Repeat input outputs: [Iteration, item0, item1, ...] — offset 1.
    return _build_zone_body(zone_def, "repeat", 1, input_node, output_node,
                            node_tree, node_map, existing_trees)


def _build_closure_zone(zone_def: ClosureZoneDef, node_tree, node_map: dict,
                        existing_trees: dict) -> list[str]:
    input_node = node_tree.nodes.new('NodeClosureInput')
    output_node = node_tree.nodes.new('NodeClosureOutput')
    input_node.pair_with_output(output_node)
    # Extraction derives the zone name from the input node's name; naming it
    # after the zone makes the name survive a bpy roundtrip.
    input_node.name = zone_def.name
    input_node.location = zone_def.location

    # Items live on the output node; input items surface as the input node's
    # outputs, output items as the output node's inputs.
    for item_def in zone_def.inputs:
        output_node.input_items.new(
            _short_socket_type(item_def.socket_type), item_def.name)
    for item_def in zone_def.outputs:
        output_node.output_items.new(
            _short_socket_type(item_def.socket_type), item_def.name)

    # External refs read the Closure socket via the zone name: output 0.
    node_map[zone_def.name] = output_node

    return _build_zone_body(zone_def, "closure", 0, input_node, output_node,
                            node_tree, node_map, existing_trees)


# ---------------------------------------------------------------------------
# Deserialisation: top-level
# ---------------------------------------------------------------------------

def _build_interface(node_tree, interface_defs: list[InterfaceSocketDef]):
    if not interface_defs or not hasattr(node_tree, 'interface'):
        return
    for sock_def in interface_defs:
        socket = node_tree.interface.new_socket(
            name=sock_def.name,
            socket_type=sock_def.socket_type,
            in_out=sock_def.direction,
        )
        if sock_def.default is not None and hasattr(socket, "default_value"):
            try:
                socket.default_value = sock_def.default
            except (TypeError, ValueError):
                pass


def _fill_tree(tree_def: TreeDef, node_tree, existing_trees: dict) -> list[str]:
    warnings: list[str] = []

    _build_interface(node_tree, tree_def.interface)

    node_map = {}
    for node_def in tree_def.nodes:
        node, w = _build_node(node_def, node_tree, existing_trees)
        warnings.extend(w)
        node_map[node_def.name] = node

    for zone_def in tree_def.zones:
        build = _build_closure_zone if isinstance(zone_def, ClosureZoneDef) \
            else _build_repeat_zone
        warnings.extend(build(zone_def, node_tree, node_map, existing_trees))

    _set_frame_parents(tree_def, node_map)

    # Set locations after parents (frame-relative coordinates)
    for node_def in tree_def.nodes:
        node = node_map.get(node_def.name)
        if node is not None:
            node.location = node_def.location
    for zone_def in tree_def.zones:
        for node_def in zone_def.nodes:
            node = node_map.get(node_def.name)
            if node is not None:
                node.location = node_def.location

    warnings.extend(_create_links(tree_def.links, node_map, node_tree))

    return warnings


def ir_to_tree(tree_def: TreeDef, existing_trees: dict | None = None, target_tree=None):
    import bpy

    if existing_trees is None:
        existing_trees = {nt.name: nt for nt in bpy.data.node_groups}

    # Build order: leaves first
    build_order: list[TreeDef] = []
    queue = collections.deque([tree_def])
    seen: set[str] = set()
    while queue:
        current = queue.popleft()
        if current.name in seen:
            continue
        seen.add(current.name)
        build_order.append(current)
        for child in current.groups.values():
            queue.append(child)
    build_order.reverse()

    all_warnings: list[str] = []
    for td in build_order:
        if td.name in existing_trees and td.name != tree_def.name:
            continue
        if td.name == tree_def.name and target_tree is not None:
            target_tree.nodes.clear()
            if hasattr(target_tree, 'interface'):
                target_tree.interface.clear()
            existing_trees[td.name] = target_tree
            warnings = _fill_tree(td, target_tree, existing_trees)
        else:
            nt = bpy.data.node_groups.new(name=td.name, type=td.bl_idname)
            existing_trees[td.name] = nt
            warnings = _fill_tree(td, nt, existing_trees)
        all_warnings.extend(warnings)

    result = target_tree if target_tree is not None else existing_trees[tree_def.name]
    return result, all_warnings
