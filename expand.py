# Copyright (C) 2026, Sam Warren, All rights reserved.
"""
Inline tree expansion: each instance of an `inline tree` becomes a copy of
its body, framed under the instance's name. Runs on resolved IR, before
building.
"""
import copy

from .core import (LinkDef, NodeDef, NodeIOError, RepeatZoneDef, TreeDef)

_INTERFACE = ('NodeGroupInput', 'NodeGroupOutput')
_PSEUDO = ('repeat', 'closure')


class ExpandError(NodeIOError):
    pass


def expand_inlines(trees: list[TreeDef]) -> list[TreeDef]:
    """Expand every instance of an inline tree, in every tree. Returns the
    trees that exist at runtime, in the original order."""
    by_name = {t.name: t for t in trees}
    done: set[str] = set()

    def expand_tree(tree: TreeDef, stack: tuple = ()):
        if tree.name in done:
            return
        if tree.name in stack:
            chain = " -> ".join((*stack, tree.name))
            raise ExpandError(f"inline cycle: {chain}")
        done.add(tree.name)
        for scope_nodes, scope_links, zone in _scopes(tree):
            for inst in [n for n in scope_nodes
                         if _is_inline_instance(n, by_name)]:
                body = by_name[inst.node_tree_name]
                expand_tree(body, (*stack, tree.name))
                _splice(tree, scope_nodes, scope_links, zone, inst, body)
        tree.groups = {name: g for name, g in tree.groups.items()
                       if not (name in by_name and by_name[name].inline)}

    for tree in trees:
        if not tree.inline:
            expand_tree(tree)
    return [t for t in trees if not t.inline]


def _scopes(tree: TreeDef):
    yield tree.nodes, tree.links, None
    for zone in tree.zones:
        yield zone.nodes, zone.links, zone


def _is_inline_instance(node: NodeDef, by_name: dict) -> bool:
    return (node.node_tree_name is not None
            and node.node_tree_name in by_name
            and by_name[node.node_tree_name].inline)


def _rename_ref(ref, mapping):
    if ref.node in mapping:
        ref.node = mapping[ref.node]


def _rename_triple(triple, mapping):
    if isinstance(triple, tuple) and triple[0] in mapping:
        return (mapping[triple[0]], *triple[1:])
    return triple


def _interface_defaults(body: TreeDef) -> list:
    return [s.default for s in body.interface if s.direction == "INPUT"]


def _splice(tree: TreeDef, scope_nodes, scope_links, zone, inst: NodeDef,
            body: TreeDef):
    body = copy.deepcopy(body)
    if body.zones and zone is not None:
        raise ExpandError(
            f"'{inst.name}': inline tree '{body.name}' contains zones and "
            f"cannot expand inside zone '{zone.name}'")

    prefix = f"{inst.name}/"
    mapping = {n.name: prefix + n.name for n in body.nodes
               if n.bl_idname not in _INTERFACE}
    for z in body.zones:
        mapping[z.name] = prefix + z.name
        mapping.update({n.name: prefix + n.name for n in z.nodes})
    for name in _PSEUDO:
        mapping.pop(name, None)

    # External links touching the instance, wherever in the tree they sit.
    in_links: dict[int, list] = {}
    out_links: dict[int, list] = {}
    for nodes_, links_, _ in _scopes(tree):
        for link in list(links_):
            if link.target.node == inst.name:
                in_links.setdefault(link.target.index, []).append(link)
                links_.remove(link)
            elif link.source.node == inst.name:
                out_links.setdefault(link.source.index, []).append(
                    (link, links_))
                links_.remove(link)

    defaults = _interface_defaults(body)

    def input_value(i):
        if i in inst.input_values:
            return inst.input_values[i]
        return defaults[i] if i < len(defaults) else None

    interface_nodes = {n.name: n.bl_idname for n in body.nodes
                       if n.bl_idname in _INTERFACE}
    copied_nodes: list[NodeDef] = []
    for node in body.nodes:
        if node.bl_idname in _INTERFACE:
            continue
        node.name = mapping[node.name]
        node.children = [mapping.get(c, c) for c in node.children]
        copied_nodes.append(node)

    # Sources feeding the body's Group Output, by output index.
    out_sources: dict[int, LinkDef] = {}
    pending_values: list[tuple] = []  # (target ref, value) inside the body

    def route(link: LinkDef, into: list[LinkDef]):
        src_if = interface_nodes.get(link.source.node)
        tgt_if = interface_nodes.get(link.target.node)
        if src_if == 'NodeGroupInput' and tgt_if == 'NodeGroupOutput':
            i, o = link.source.index, link.target.index
            for ext_in in in_links.get(i, []):
                for ext_out, _ in out_links.get(o, []):
                    into.append(LinkDef(
                        source=copy.deepcopy(ext_in.source),
                        target=copy.deepcopy(ext_out.target)))
            if i not in in_links and input_value(i) is not None:
                for ext_out, _ in out_links.get(o, []):
                    pending_values.append((ext_out.target, input_value(i)))
        elif src_if == 'NodeGroupInput':
            _rename_ref(link.target, mapping)
            i = link.source.index
            if i in in_links:
                for ext_in in in_links[i]:
                    into.append(LinkDef(
                        source=copy.deepcopy(ext_in.source),
                        target=copy.deepcopy(link.target)))
            elif input_value(i) is not None:
                pending_values.append((link.target, input_value(i)))
        elif tgt_if == 'NodeGroupOutput':
            _rename_ref(link.source, mapping)
            out_sources[link.target.index] = link
        else:
            _rename_ref(link.source, mapping)
            _rename_ref(link.target, mapping)
            into.append(link)

    kept_links: list[LinkDef] = []
    for link in body.links:
        route(link, kept_links)
    for z in body.zones:
        routed: list[LinkDef] = []
        for link in z.links:
            route(link, routed)
        z.links = routed

    for o, entries in out_links.items():
        src = out_sources.get(o)
        if src is None:
            continue
        for ext_out, links_ in entries:
            links_.append(LinkDef(source=copy.deepcopy(src.source),
                                  target=ext_out.target))

    by_new_name = {n.name: n for n in copied_nodes}
    for z in body.zones:
        by_new_name.update({prefix + n.name: n for n in z.nodes})
    for target, value in pending_values:
        node = by_new_name.get(target.node) or _find_node(tree, target.node)
        if node is not None:
            node.input_values[target.index] = value
            if target.name:
                node.input_names[target.index] = target.name

    for z in body.zones:
        z.name = mapping[z.name]
        for n in z.nodes:
            n.name = mapping[n.name]
            n.children = [mapping.get(c, c) for c in n.children]
        z.output_mappings = {k: _rename_triple(v, mapping)
                             for k, v in z.output_mappings.items()}
        if isinstance(z, RepeatZoneDef):
            z.iterations = _rename_triple(z.iterations, mapping)
            for item in z.items:
                if item.initial_connection is not None:
                    item.initial_connection = _rename_triple(
                        item.initial_connection, mapping)
        tree.zones.append(z)

    framed = {c for n in copied_nodes for c in n.children}
    frame = NodeDef(name=inst.name, bl_idname='NodeFrame', label=body.name,
                    location=inst.location,
                    children=[n.name for n in copied_nodes
                              if n.name not in framed])
    scope_nodes.remove(inst)
    scope_nodes.extend(copied_nodes)
    scope_nodes.append(frame)
    scope_links.extend(kept_links)
    tree.groups.update(body.groups)


def _find_node(tree: TreeDef, name: str) -> NodeDef | None:
    for nodes_, _, _ in _scopes(tree):
        for n in nodes_:
            if n.name == name:
                return n
    return None
