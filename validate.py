# Copyright (C) 2026, Sam Warren, All rights reserved.
"""
Offline analysis of parsed node trees against a schema registry: name-based
socket resolution and validation. Runs without Blender.

Resolution turns name-only socket refs into indices, the canonical form;
names resolve on group interface sockets, built-in node sockets are addressed
by index. Validation catches unknown node types, bad
properties/enums, out-of-range socket indices, and dangling references before
bpy ever sees the tree. The registry comes from schema.load_registry
(registry.json, generated inside Blender by `cli.py schema`).
"""
from dataclasses import dataclass

from .core import (ClosureZoneDef, ITEM_NODES, LinkDef, NodeDef,
                   RepeatZoneDef, SocketRef, TreeDef)
from .schema import NodeTypeDef, NodeVariant, SchemaRegistry

# Node types that exist structurally but are not in the per-tree registry,
# or whose sockets come from elsewhere (interfaces, zone items).
STRUCTURAL_TYPES = {"NodeFrame", "NodeReroute"}
GROUP_TYPES = {"GeometryNodeGroup", "ShaderNodeGroup", "CompositorNodeGroup", "TextureNodeGroup"}
INTERFACE_TYPES = {"NodeGroupInput", "NodeGroupOutput"}


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    tree: str
    node: str
    message: str

    def __str__(self):
        loc = f"{self.tree}/{self.node}" if self.node else self.tree
        return f"{self.severity.upper()} [{loc}]: {self.message}"


# ---------------------------------------------------------------------------
# Shared registry/tree lookups
# ---------------------------------------------------------------------------

def _global_types(registry: SchemaRegistry) -> dict[str, NodeTypeDef]:
    """bl_idname -> NodeTypeDef across every tree type. Blender allows some
    cross-tree node reuse (e.g. ShaderNodeMath inside geometry trees), so
    lookups fall back to this before calling a type unknown."""
    out: dict[str, NodeTypeDef] = {}
    for defs in registry.tree_types.values():
        for n in defs:
            out.setdefault(n.bl_idname, n)
    return out


def _effective_props(node_def: NodeDef, type_def: NodeTypeDef) -> dict:
    """The node's property values with schema defaults filled in."""
    props = {p.identifier: p.default for p in type_def.properties}
    props.update(node_def.properties)
    return props


def _interface_names(tree: TreeDef, direction: str) -> list[str]:
    """Socket names of a tree interface side, in index order."""
    return [s.name for s in tree.interface if s.direction == direction]


def _collect_trees(tree_defs: list[TreeDef]) -> dict[str, TreeDef]:
    out: dict[str, TreeDef] = {}
    stack = list(tree_defs)
    while stack:
        td = stack.pop()
        if td.name in out:
            continue
        out[td.name] = td
        stack.extend(td.groups.values())
    return out


def _active_variant(node_def: NodeDef,
                    type_def: NodeTypeDef) -> tuple[NodeVariant | None, bool]:
    """Returns (variant, names_reliable). Schema variants are deduplicated
    by socket signature, so a property value with no exact variant match is
    normal (e.g. Math operation=SINE shares the ADD signature). When all
    variants agree on socket counts we can still range-check indices, but
    socket names may belong to a different variant."""
    if not type_def.variants:
        return None, False
    props = _effective_props(node_def, type_def)
    for variant in type_def.variants:
        if all(props.get(k) == v for k, v in variant.variant_key.items()):
            return variant, True
    if len(type_def.variants) == 1:
        return type_def.variants[0], True
    counts = {(len(v.inputs), len(v.outputs)) for v in type_def.variants}
    if len(counts) == 1:
        return type_def.variants[0], False
    return None, False


# ---------------------------------------------------------------------------
# Name-based socket resolution
# ---------------------------------------------------------------------------

class _NameResolver:
    """Resolves name-only socket refs (index == -1) to indices, mutating the
    trees in place. Names resolve on group interface sockets, where they are
    author-chosen and stable while a library tool's parameter list evolves:
    group instances via the referenced tree's interface, NodeGroupInput/Output
    via the containing tree's interface, zone pseudo-nodes ("repeat",
    "closure") via the zone's items, item-bearing nodes via their declared
    items. Built-in node sockets are addressed by index (the schema pins them
    per Blender version, and their names collide freely)."""

    def __init__(self, all_trees: dict[str, TreeDef]):
        self.all_trees = all_trees

    def _candidate_indices(self, node_def: NodeDef, tree: TreeDef,
                           name: str, side: str) -> tuple[list[int] | None, str]:
        """All indices `name` could mean on this node's `side` ("in"/"out").
        Returns (indices, error_message); indices is None when the node's
        sockets are unknowable offline."""
        idname = node_def.bl_idname

        if idname == "NodeReroute":
            return [0], ""

        if idname in ITEM_NODES:
            in_off, out_off = ITEM_NODES[idname]
            offset = in_off if side == "in" else out_off
            items = node_def.input_items if side == "in" else node_def.output_items
            return [offset + i for i, it in enumerate(items) if it.name == name], ""

        if idname in GROUP_TYPES:
            ref = self.all_trees.get(node_def.node_tree_name or "")
            if ref is None:
                return None, (f"group tree '{node_def.node_tree_name}' is outside "
                              "this document; use an index")
            names = _interface_names(ref, "INPUT" if side == "in" else "OUTPUT")
        elif idname == "NodeGroupInput":
            names = _interface_names(tree, "INPUT")
        elif idname == "NodeGroupOutput":
            names = _interface_names(tree, "OUTPUT")
        else:
            return None, (f"sockets on {idname} are addressed by index; "
                          "names resolve on group interface sockets only")

        return [i for i, n in enumerate(names) if n == name], ""

    def _resolve_ref(self, ref: SocketRef, tree: TreeDef,
                     nodes_by_name: dict[str, NodeDef], side: str,
                     issues: list[Issue]) -> bool:
        node_def = nodes_by_name.get(ref.node)
        if node_def is None:
            issues.append(Issue("error", tree.name, ref.node,
                                f"Name ref ({ref.name!r}) on unknown node"))
            return False
        indices, err = self._candidate_indices(node_def, tree, ref.name, side)
        if indices is None or err or len(indices) != 1:
            issues.append(Issue("error", tree.name, ref.node,
                                f"Cannot resolve socket name {ref.name!r}: "
                                f"{err or 'ambiguous'}"))
            return False
        ref.index = indices[0]
        return True

    def _resolve_named_values(self, node_def: NodeDef, tree: TreeDef,
                              nodes_by_name: dict[str, NodeDef],
                              issues: list[Issue]):
        for sock_name in list(node_def.named_input_values):
            probe = SocketRef(node=node_def.name, index=-1, name=sock_name)
            if self._resolve_ref(probe, tree, nodes_by_name, "in", issues):
                node_def.input_values[probe.index] = \
                    node_def.named_input_values.pop(sock_name)
                node_def.input_names[probe.index] = sock_name

    def _resolve_outer(self, ref: tuple[str, int, str], tree: TreeDef,
                       nodes_by_name: dict[str, NodeDef],
                       issues: list[Issue]) -> tuple[str, int, str]:
        """Resolve a (node, index, name) triple that a repeat zone header
        reads from the enclosing tree."""
        node_name, index, sock_name = ref
        if index >= 0:
            return ref
        probe = SocketRef(node=node_name, index=index, name=sock_name)
        self._resolve_ref(probe, tree, nodes_by_name, "out", issues)
        return (node_name, probe.index, sock_name)

    def _resolve_links(self, links: list[LinkDef], tree: TreeDef,
                       nodes_by_name: dict[str, NodeDef], issues: list[Issue],
                       pseudo: tuple[str, list[str]] | None = None,
                       zone_items: dict[str, list[str]] | None = None):
        def resolve(ref, side):
            names = None
            if pseudo is not None and ref.node == pseudo[0]:
                names = pseudo[1]
            elif zone_items is not None and ref.node in zone_items:
                # zone outputs read externally via the zone name
                names = zone_items[ref.node]
            if names is not None:
                hits = [i for i, n in enumerate(names) if n == ref.name]
                if len(hits) == 1:
                    ref.index = hits[0]
                else:
                    issues.append(Issue("error", tree.name, ref.node,
                                        f"Cannot resolve zone item {ref.name!r}"))
            else:
                self._resolve_ref(ref, tree, nodes_by_name, side, issues)

        for link in links:
            if link.source.index < 0:
                resolve(link.source, "out")
            if link.target.index < 0:
                resolve(link.target, "in")

    def resolve_tree(self, tree: TreeDef, issues: list[Issue]):
        nodes_by_name = {n.name: n for n in tree.nodes}
        zone_items: dict[str, list[str]] = {}
        for z in tree.zones:
            if isinstance(z, RepeatZoneDef):
                zone_items[z.name] = [it.name for it in z.items]
            else:
                zone_items[z.name] = ["Closure"]
        self._resolve_links(tree.links, tree, nodes_by_name, issues,
                            zone_items=zone_items)
        for node_def in tree.nodes:
            self._resolve_named_values(node_def, tree, nodes_by_name, issues)

        for zone in tree.zones:
            zone_nodes = dict(nodes_by_name)
            zone_nodes.update({n.name: n for n in zone.nodes})
            if isinstance(zone, RepeatZoneDef):
                pseudo = ("repeat", [it.name for it in zone.items])
                # A zone header reads from outside the zone: its iteration
                # count and each item's initial value. Those refs take names
                # like any other, and an unresolved one would link to the
                # wrong socket.
                if isinstance(zone.iterations, tuple):
                    zone.iterations = self._resolve_outer(
                        zone.iterations, tree, nodes_by_name, issues)
                for item in zone.items:
                    if item.initial_connection is not None:
                        item.initial_connection = self._resolve_outer(
                            item.initial_connection, tree, nodes_by_name, issues)
            else:
                pseudo = ("closure", [it.name for it in zone.inputs])
            self._resolve_links(zone.links, tree, zone_nodes, issues, pseudo,
                                zone_items=zone_items)
            for node_def in zone.nodes:
                self._resolve_named_values(node_def, tree, zone_nodes, issues)


def resolve_names(tree_defs: list[TreeDef], registry: SchemaRegistry) -> list[Issue]:
    """Resolve all name-only socket refs to indices, in place. Returns issues
    for anything unresolvable; trees with no name refs are untouched."""
    all_trees = _collect_trees(tree_defs)
    resolver = _NameResolver(all_trees)
    issues: list[Issue] = []
    for td in all_trees.values():
        resolver.resolve_tree(td, issues)
    return issues


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class _TreeValidator:
    def __init__(self, registry: SchemaRegistry, tree_def: TreeDef,
                 all_trees: dict[str, TreeDef], issues: list[Issue]):
        self.registry = registry
        self.tree = tree_def
        self.all_trees = all_trees
        self.issues = issues
        self.node_types = {n.bl_idname: n for n in registry.tree_types.get(tree_def.bl_idname, [])}
        self.global_types = _global_types(registry)
        if not self.node_types:
            self._warn("", f"No schema for tree type '{tree_def.bl_idname}'; "
                           "node-level checks skipped")

    def _error(self, node: str, msg: str):
        self.issues.append(Issue("error", self.tree.name, node, msg))

    def _warn(self, node: str, msg: str):
        self.issues.append(Issue("warning", self.tree.name, node, msg))

    def _socket_counts(self, node_def: NodeDef) -> tuple[int | None, int | None]:
        """(n_inputs, n_outputs) for a node, or None where unknown."""
        if node_def.bl_idname == "NodeReroute":
            return 1, 1
        if node_def.bl_idname == "NodeFrame":
            return 0, 0
        if node_def.bl_idname in ITEM_NODES:
            in_off, out_off = ITEM_NODES[node_def.bl_idname]
            return (in_off + len(node_def.input_items),
                    out_off + len(node_def.output_items))
        if node_def.bl_idname in GROUP_TYPES:
            ref = self.all_trees.get(node_def.node_tree_name or "")
            if ref is None:
                return None, None
            return (len(_interface_names(ref, "INPUT")),
                    len(_interface_names(ref, "OUTPUT")))
        # +1 on the interface side: Blender appends a virtual extension socket.
        if node_def.bl_idname == "NodeGroupInput":
            return 0, len(_interface_names(self.tree, "INPUT")) + 1
        if node_def.bl_idname == "NodeGroupOutput":
            return len(_interface_names(self.tree, "OUTPUT")) + 1, 0
        type_def = self.node_types.get(node_def.bl_idname) \
            or self.global_types.get(node_def.bl_idname)
        if type_def is None:
            return None, None
        variant, _ = _active_variant(node_def, type_def)
        if variant is None:
            return None, None
        return len(variant.inputs), len(variant.outputs)

    def check_node(self, node_def: NodeDef):
        name = node_def.name
        idname = node_def.bl_idname

        if idname in STRUCTURAL_TYPES:
            return

        if idname in ITEM_NODES:
            # Sockets come from the declared items, not the type signature.
            n_in, _ = self._socket_counts(node_def)
            for idx in node_def.input_values:
                if idx >= n_in:
                    self._error(name, f"Input index {idx} out of range on "
                                      f"{idname} (has {n_in} inputs)")
            return

        if idname in GROUP_TYPES:
            if not node_def.node_tree_name:
                self._error(name, "Group node has no node_tree reference")
            elif node_def.node_tree_name not in self.all_trees:
                self._warn(name, f"Group references tree '{node_def.node_tree_name}' "
                                 "outside this document; skipped")
            return

        if idname in INTERFACE_TYPES:
            return

        type_def = self.node_types.get(idname) or self.global_types.get(idname)
        if type_def is None:
            if self.node_types:
                self._error(name, f"Unknown node type '{idname}' for {self.tree.bl_idname}")
            return

        prop_defs = {p.identifier: p for p in type_def.properties}
        for key, val in node_def.properties.items():
            pdef = prop_defs.get(key)
            if pdef is None:
                self._error(name, f"Unknown property '{key}' on {idname}")
                continue
            if pdef.enum_values is not None and val not in pdef.enum_values:
                self._error(name, f"Invalid enum value '{val}' for {idname}.{key}; "
                                  f"valid: {', '.join(pdef.enum_values)}")

        variant, names_reliable = _active_variant(node_def, type_def)
        if variant is None:
            if type_def.variants:
                props = _effective_props(node_def, type_def)
                keys = {k for v in type_def.variants for k in v.variant_key}
                got = {k: props.get(k) for k in keys}
                self._warn(name, f"No socket variant of {idname} matches {got}; "
                                 "socket index checks skipped")
            return

        for idx in node_def.input_values:
            if idx >= len(variant.inputs):
                self._error(name, f"Input index {idx} out of range on {idname} "
                                  f"(has {len(variant.inputs)} inputs)")
            else:
                annotated = node_def.input_names.get(idx, "")
                actual = variant.inputs[idx].name
                if names_reliable and annotated and annotated != actual:
                    self._warn(name, f"Input {idx} is '{actual}', annotated '{annotated}' — "
                                     "likely wrong index")

    def check_link(self, link: LinkDef, nodes_by_name: dict[str, NodeDef]):
        # External reads: a repeat zone exposes its carried items, a closure
        # zone the single Closure socket.
        zone_counts = {z.name: len(z.items) if isinstance(z, RepeatZoneDef)
                       else 1 for z in self.tree.zones}
        for ref, direction in ((link.source, "source"), (link.target, "target")):
            if direction == "source" and ref.node in zone_counts:
                # zone outputs read externally via the zone name
                if ref.index >= zone_counts[ref.node]:
                    self._error(ref.node, f"Link source: zone output index "
                                          f"{ref.index} out of range "
                                          f"(has {zone_counts[ref.node]} items)")
                continue
            node_def = nodes_by_name.get(ref.node)
            if node_def is None:
                self._error(ref.node, f"Link {direction} references unknown node")
                continue
            n_in, n_out = self._socket_counts(node_def)
            count = n_out if direction == "source" else n_in
            if count is not None and ref.index >= count:
                kind = "output" if direction == "source" else "input"
                self._error(ref.node, f"Link {direction}: {kind} index {ref.index} "
                                      f"out of range (has {count})")

    def run(self):
        nodes_by_name = {n.name: n for n in self.tree.nodes}
        seen: set[str] = set()
        for node_def in self.tree.nodes:
            if node_def.name in seen:
                self._error(node_def.name, "Duplicate node name")
            seen.add(node_def.name)
            self.check_node(node_def)

        for link in self.tree.links:
            self.check_link(link, nodes_by_name)

        for zone in self.tree.zones:
            for node_def in zone.nodes:
                self.check_node(node_def)
            items = zone.items if isinstance(zone, RepeatZoneDef) \
                else zone.inputs + zone.outputs
            for item in items:
                if self.registry.socket_types and \
                        item.socket_type not in self.registry.socket_types:
                    self._warn(zone.name, f"Zone item '{item.name}': unknown "
                                          f"socket type '{item.socket_type}'")

        self.check_closure_signatures(nodes_by_name)

    def check_closure_signatures(self, nodes_by_name: dict[str, NodeDef]):
        """Blender matches evaluate items to the closure's items by name at
        evaluation time; a mismatch silently yields the socket default. When
        the closure input is wired from a zone in this document, the check
        runs statically."""
        closures = {z.name: z for z in self.tree.zones
                    if isinstance(z, ClosureZoneDef)}
        if not closures:
            return
        for link in self.tree.links:
            node_def = nodes_by_name.get(link.target.node)
            if node_def is None or node_def.bl_idname != 'NodeEvaluateClosure' \
                    or link.target.index != 0:
                continue
            zone = closures.get(link.source.node)
            if zone is None:
                continue
            for side, declared, zone_side in (
                    ("input", node_def.input_items, zone.inputs),
                    ("output", node_def.output_items, zone.outputs)):
                signature = {it.name: it.socket_type for it in zone_side}
                for item in declared:
                    expected = signature.get(item.name)
                    if expected is None:
                        self._error(node_def.name,
                                    f"Evaluate {side} item '{item.name}' is not "
                                    f"an {side} of closure '{zone.name}' "
                                    f"(has: {', '.join(signature) or 'none'})")
                    elif expected != item.socket_type:
                        self._error(node_def.name,
                                    f"Evaluate {side} item '{item.name}' is "
                                    f"[{item.socket_type}]; closure "
                                    f"'{zone.name}' declares [{expected}]")
            supplied = {it.name for it in node_def.input_items}
            for item_name in (it.name for it in zone.inputs):
                if item_name not in supplied:
                    self._warn(node_def.name,
                               f"Closure input '{item_name}' not supplied; "
                               "it evaluates to its default")


def validate(tree_defs: list[TreeDef], registry: SchemaRegistry) -> list[Issue]:
    """Resolve name-based refs, then validate parsed trees against the
    registry. Returns all issues found; an empty list means the document
    should apply cleanly in Blender."""
    issues = resolve_names(tree_defs, registry)
    all_trees = _collect_trees(tree_defs)
    for td in all_trees.values():
        _TreeValidator(registry, td, all_trees, issues).run()
    return issues
