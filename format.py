# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sam Warren
import collections
import re
from typing import Any

from . import core as core
from . import preprocessor
from .core import NodeIOError, SocketRef

SCHEMA_README = """\
# Node Schema

Each subfolder contains the node types for one tree type (e.g. ShaderNodeTree).

## Files

- `index.nodetypes` — summary of every node type: bl_idname, label, socket types, properties.
- `<NodeType>.nodetypes` — full definition with all properties, enum values, default values, \
and per-variant socket signatures.
- `registry.json` — the same universe as one machine-readable blob; the
  `query` and `validate` CLI commands load it. Browse the `.nodetypes` files.

## Workflow

1. `cli.py -- query registry.json -s <pattern>` finds node types;
   `query registry.json <BlIdname>` prints sockets as paste-ready DSL refs.
   The index and detail files carry the same information for reading.
2. Use the bl_idname (the value in brackets, e.g. [ShaderNodeMath]) in your .nodes file.
"""


class ParseError(NodeIOError):
    def __init__(self, message: str, pos: int = 0):
        super().__init__(f"pos {pos}: {message}")
        self.pos = pos


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"""
    (?P<COMMENT>    //[^\n]* | /\*(?s:.*?)\*/)     |
    (?P<STRING>     "[^"]*")                       |
    (?P<TYPE>       \[[A-Za-z_]\w*\])              |
    (?P<TYPEPARAM>  <[A-Za-z_]\w*>)                |
    (?P<TRANSFORM>  @\([^)]+\))                    |
    (?P<ARROW>      ->)                            |
    (?P<LBRACE>     \{)                            |
    (?P<RBRACE>     \})                            |
    (?P<LPAREN>     \()                            |
    (?P<RPAREN>     \))                            |
    (?P<COMMA>      ,)                             |
    (?P<COLON>      :)                             |
    (?P<EQUALS>     =)                             |
    (?P<DOT>        \.)                            |
    (?P<NUMBER>     -?\d+(?:\.\d+)?(?:e[+-]?\d+)?) |
    (?P<WORD>       [A-Za-z_]\w*)                  |
    (?P<SKIP>       \s+)                           |
    (?P<BAD>        .)
""", re.VERBOSE)

KEYWORDS = frozenset({
    'tree', 'node', 'frame', 'reroute', 'repeat', 'closure',
    'interface', 'inputs', 'outputs', 'items', 'children',
    'import', 'expr', 'inline',
    'true', 'false',
})


class Token:
    __slots__ = ('kind', 'value', 'pos')
    def __init__(self, kind: str, value: str, pos: int):
        self.kind = kind
        self.value = value
        self.pos = pos
    def __repr__(self):
        return f"{self.kind}({self.value!r})"


def tokenise(text: str) -> list[Token]:
    tokens = []
    for m in TOKEN_RE.finditer(text):
        kind = m.lastgroup
        value = m.group()
        if kind in ('SKIP', 'COMMENT'):
            continue
        if kind == 'BAD':
            raise ParseError(f"unexpected character {value!r}", m.start())
        if kind == 'WORD' and value in KEYWORDS:
            kind = 'KW'
        elif kind == 'STRING':
            value = value[1:-1]
        elif kind in ('TYPE', 'TYPEPARAM'):
            value = value[1:-1]
        tokens.append(Token(kind, value, m.start()))
    return tokens


# ---------------------------------------------------------------------------
# Token stream
# ---------------------------------------------------------------------------

class Stream:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> Token:
        if self.pos >= len(self.tokens):
            raise ParseError("Unexpected end of input")
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect(self, kind: str, value: str | None = None) -> Token:
        t = self.next()
        if t.kind != kind or (value is not None and t.value != value):
            raise ParseError(f"Expected {kind}({value}), got {t}", t.pos)
        return t

    def at(self, kind: str, value: str | None = None) -> bool:
        t = self.peek()
        return t is not None and t.kind == kind and (value is None or t.value == value)

    def at_kw(self, value: str) -> bool:
        return self.at('KW', value)

    def skip_commas(self):
        while self.at('COMMA'):
            self.next()


# ---------------------------------------------------------------------------
# Parse primitives
# ---------------------------------------------------------------------------

def _p_name(s: Stream) -> str:
    t = s.peek()
    if t and t.kind in ('STRING', 'WORD', 'KW'):
        return s.next().value
    raise ParseError(f"Expected name, got {t}", t.pos if t else -1)


def _p_type(s: Stream) -> str:
    return s.expect('TYPE').value


def _p_socket_ref(s: Stream) -> SocketRef:
    s.expect('LPAREN')
    if s.at('STRING'):
        name = s.next().value
        s.expect('RPAREN')
        return SocketRef(node="", index=-1, name=name)
    idx = int(s.expect('NUMBER').value)
    name = ""
    if s.at('COLON'):
        s.next()
        name = _p_name(s)
    s.expect('RPAREN')
    return SocketRef(node="", index=idx, name=name)


def _p_node_socket_ref(s: Stream) -> SocketRef:
    node = _p_name(s)
    ref = _p_socket_ref(s)
    ref.node = node
    return ref


def _p_value(s: Stream) -> Any:
    t = s.peek()
    if t is None:
        raise ParseError("Expected value")
    if t.kind == 'STRING':
        return s.next().value
    if t.kind == 'KW' and t.value == 'true':
        s.next()
        return True
    if t.kind == 'KW' and t.value == 'false':
        s.next()
        return False
    if t.kind == 'NUMBER':
        raw = s.next().value
        return int(raw) if '.' not in raw and 'e' not in raw.lower() else float(raw)
    if t.kind == 'LPAREN':
        s.next()
        values = []
        while not s.at('RPAREN'):
            values.append(_p_value(s))
            s.skip_commas()
        s.expect('RPAREN')
        return tuple(values)
    raise ParseError(f"Expected value, got {t}", t.pos)


def _p_transform(s: Stream) -> tuple[tuple[float, float], float, float]:
    if not s.at('TRANSFORM'):
        return (0.0, 0.0), 0.0, 0.0
    raw = s.next().value[2:-1]  # strip @( and )
    parts = [p.strip() for p in raw.split(",")]
    x = float(parts[0]) if len(parts) > 0 else 0.0
    y = float(parts[1]) if len(parts) > 1 else 0.0
    w = h = 0.0
    for p in parts[2:]:
        if p.startswith("w="):
            w = float(p[2:])
        elif p.startswith("h="):
            h = float(p[2:])
    return (x, y), w, h


def _p_label(s: Stream) -> str:
    if s.at('WORD', 'label') or s.at('KW', 'label'):
        s.next()
        s.expect('EQUALS')
        return _p_name(s)
    return ""


# ---------------------------------------------------------------------------
# Parse: socket entries
# ---------------------------------------------------------------------------

def _p_entries(s: Stream) -> list[tuple]:
    """Parse { entry, entry, ... }. Each entry is one of:
      ("connection", name_or_ref, target_ref)
      ("value", name_or_ref, literal)
      ("typed", name, type, default_or_None)
      ("name_only", name)
    The first element after { determines the form.
    """
    s.expect('LBRACE')
    entries = []
    while not s.at('RBRACE'):
        # Socket ref: (index: "name") or (index)
        if s.at('LPAREN'):
            ref = _p_socket_ref(s)
            if s.at('ARROW'):
                s.next()
                target = _p_node_socket_ref(s)
                entries.append(("connection", ref, target))
            elif s.at('EQUALS'):
                s.next()
                entries.append(("value", ref, _p_value(s)))
            elif s.at('COLON'):
                s.next()
                socket_type = _p_type(s)
                default = None
                if s.at('EQUALS'):
                    s.next()
                    default = _p_value(s)
                entries.append(("typed", ref.name, socket_type, default))
        # Quoted name (for typed entries, children, etc.)
        elif s.at('STRING') or s.at('WORD') or s.at('KW'):
            name = _p_name(s)
            if s.at('ARROW'):
                s.next()
                target = _p_node_socket_ref(s)
                entries.append(("connection", name, target))
            elif s.at('EQUALS'):
                s.next()
                entries.append(("value", name, _p_value(s)))
            elif s.at('COLON'):
                s.next()
                socket_type = _p_type(s)
                if s.at('ARROW'):
                    s.next()
                    target = _p_node_socket_ref(s)
                    entries.append(("typed_connection", name, socket_type, target))
                else:
                    default = None
                    if s.at('EQUALS'):
                        s.next()
                        default = _p_value(s)
                    entries.append(("typed", name, socket_type, default))
            else:
                entries.append(("name_only", name))
        else:
            raise ParseError(f"Unexpected token in block: {s.peek()}", s.peek().pos)
        s.skip_commas()
    s.expect('RBRACE')
    return entries


# ---------------------------------------------------------------------------
# Parse: blocks
# ---------------------------------------------------------------------------

def _p_node(s: Stream) -> tuple[core.NodeDef, list[core.LinkDef]]:
    s.expect('KW', 'node')
    name = _p_name(s)
    node_type = _p_type(s)
    location, width, height = _p_transform(s)
    label = _p_label(s)

    s.expect('LBRACE')
    node_def = core.NodeDef(
        name=name, bl_idname=node_type, label=label,
        location=location, width=width, height=height,
    )
    links = []

    def declare_item(side: str, item_name: str, socket_type: str) -> int:
        offsets = core.ITEM_NODES.get(node_type)
        if offsets is None:
            raise ParseError(
                f"node \"{name}\" [{node_type}]: typed socket entries declare "
                "items, and this node type has no item list")
        items = node_def.input_items if side == "in" else node_def.output_items
        idx = offsets[0 if side == "in" else 1] + len(items)
        items.append(core.NodeItemDef(item_name, socket_type))
        return idx

    while not s.at('RBRACE'):
        if s.at_kw('inputs'):
            s.next()
            for entry in _p_entries(s):
                if entry[0] == "connection":
                    ref, target = entry[1], entry[2]
                    if isinstance(ref, str):  # bare "Name" -> ... entry
                        ref = SocketRef("", -1, ref)
                    target_ref = SocketRef(name, ref.index, ref.name)
                    links.append(core.LinkDef(source=target, target=target_ref))
                elif entry[0] == "value":
                    ref = entry[1]
                    if isinstance(ref, str):  # bare "Name" = value entry
                        ref = SocketRef("", -1, ref)
                    if ref.index < 0:
                        node_def.named_input_values[ref.name] = entry[2]
                    else:
                        node_def.input_values[ref.index] = entry[2]
                        if ref.name:
                            node_def.input_names[ref.index] = ref.name
                elif entry[0] == "typed":
                    idx = declare_item("in", entry[1], entry[2])
                    node_def.input_names[idx] = entry[1]
                    if entry[3] is not None:
                        node_def.input_values[idx] = entry[3]
                elif entry[0] == "typed_connection":
                    idx = declare_item("in", entry[1], entry[2])
                    node_def.input_names[idx] = entry[1]
                    links.append(core.LinkDef(
                        source=entry[3], target=SocketRef(name, idx, entry[1])))
        elif s.at_kw('outputs'):
            s.next()
            for entry in _p_entries(s):
                if entry[0] == "value":
                    ref = entry[1]
                    node_def.output_values[ref.index] = entry[2]
                elif entry[0] == "typed":
                    idx = declare_item("out", entry[1], entry[2])
                    node_def.output_names[idx] = entry[1]
        elif s.at('WORD') or s.at('KW'):
            key = s.next().value
            s.expect('EQUALS')
            val = _p_value(s)
            if key == "node_tree":
                node_def.node_tree_name = val
            elif key == "mute":
                node_def.mute = val
            elif key == "hide":
                node_def.hide = val
            else:
                node_def.properties[key] = val
        else:
            s.next()

    s.expect('RBRACE')
    return node_def, links


def _p_frame(s: Stream) -> core.NodeDef:
    s.expect('KW', 'frame')
    name = _p_name(s)
    location, width, height = _p_transform(s)
    label = _p_label(s)

    s.expect('LBRACE')
    node_def = core.NodeDef(
        name=name, bl_idname='NodeFrame', label=label,
        location=location, width=width, height=height,
    )
    while not s.at('RBRACE'):
        if s.at_kw('children'):
            s.next()
            for entry in _p_entries(s):
                if entry[0] == "name_only":
                    node_def.children.append(entry[1])
        else:
            s.next()
    s.expect('RBRACE')
    return node_def


def _p_reroute(s: Stream) -> tuple[core.NodeDef, core.LinkDef | None]:
    s.expect('KW', 'reroute')
    name = _p_name(s)
    location, _, _ = _p_transform(s)

    node_def = core.NodeDef(name=name, bl_idname='NodeReroute', location=location)
    link = None
    if s.at('ARROW'):
        s.next()
        source = _p_node_socket_ref(s)
        link = core.LinkDef(source=source, target=SocketRef(name, 0, "Input"))
    return node_def, link


def _p_expr(s: Stream) -> tuple[list[core.NodeDef], list[core.LinkDef]]:
    from . import expr as expr_mod

    kw = s.expect('KW', 'expr')
    if not s.at('TYPEPARAM'):
        raise ParseError("expr requires a type: expr<float> or expr<int>",
                         kw.pos)
    tok = s.next()
    dtype = tok.value
    if dtype not in expr_mod.BACKENDS:
        raise ParseError(f"unknown expression type '<{dtype}>'; available: "
                         f"{', '.join(sorted(expr_mod.BACKENDS))}", tok.pos)
    name = _p_name(s)
    location, _, _ = _p_transform(s)
    s.expect('LBRACE')

    formula, formula_pos = None, 0
    bindings: dict = {}
    while not s.at('RBRACE'):
        if s.at('WORD', 'expression'):
            s.next()
            s.expect('EQUALS')
            tok = s.expect('STRING')
            formula, formula_pos = tok.value, tok.pos
        elif s.at_kw('inputs'):
            s.next()
            for entry in _p_entries(s):
                if entry[0] == "connection":
                    ident = entry[1] if isinstance(entry[1], str) else entry[1].name
                    bindings[ident] = entry[2]
                elif entry[0] == "value":
                    ident = entry[1] if isinstance(entry[1], str) else entry[1].name
                    bindings[ident] = entry[2]
        else:
            s.next()
    s.expect('RBRACE')

    if formula is None:
        raise ParseError(f"expr \"{name}\" has no expression entry", formula_pos)
    try:
        return expr_mod.compile_expression(name, formula, bindings, location,
                                           dtype)
    except expr_mod.ExprError as e:
        raise ParseError(
            f"expr \"{name}\": formula col {e.col}: {e.msg}", formula_pos)


def _p_interface(s: Stream) -> list[core.InterfaceSocketDef]:
    s.expect('KW', 'interface')
    s.expect('LBRACE')
    sockets = []
    while not s.at('RBRACE'):
        if s.at_kw('inputs') or s.at_kw('outputs'):
            direction = "INPUT" if s.next().value == "inputs" else "OUTPUT"
            for entry in _p_entries(s):
                if entry[0] == "typed":
                    sockets.append(core.InterfaceSocketDef(
                        name=entry[1], socket_type=entry[2], direction=direction,
                        default=entry[3],
                    ))
        else:
            s.next()
    s.expect('RBRACE')
    return sockets


def _p_body_construct(s: Stream, nodes: list, links: list) -> bool:
    if s.at_kw('node'):
        node_def, node_links = _p_node(s)
        nodes.append(node_def)
        links.extend(node_links)
    elif s.at_kw('frame'):
        nodes.append(_p_frame(s))
    elif s.at_kw('reroute'):
        node_def, link = _p_reroute(s)
        nodes.append(node_def)
        if link:
            links.append(link)
    elif s.at_kw('expr'):
        expr_nodes, expr_links = _p_expr(s)
        nodes.extend(expr_nodes)
        links.extend(expr_links)
    else:
        return False
    return True


def _p_repeat(s: Stream) -> core.RepeatZoneDef:
    s.expect('KW', 'repeat')
    name = _p_name(s)
    location, _, _ = _p_transform(s)

    s.expect('LBRACE')
    zone = core.RepeatZoneDef(name=name, location=location)

    while not s.at('RBRACE'):
        if s.at('WORD') and s.peek().value == 'iterations':
            s.next()
            if s.at('ARROW'):
                s.next()
                ref = _p_node_socket_ref(s)
                zone.iterations = (ref.node, ref.index, ref.name)
            elif s.at('EQUALS'):
                s.next()
                zone.iterations = _p_value(s)
        elif s.at_kw('items'):
            s.next()
            for entry in _p_entries(s):
                if entry[0] == "typed":
                    zone.items.append(core.RepeatItemDef(
                        name=entry[1], socket_type=entry[2], default_value=entry[3],
                    ))
                elif entry[0] == "typed_connection":
                    zone.items.append(core.RepeatItemDef(
                        name=entry[1], socket_type=entry[2],
                        initial_connection=(entry[3].node, entry[3].index, entry[3].name),
                    ))
        elif s.at_kw('outputs'):
            s.next()
            for entry in _p_entries(s):
                if entry[0] == "connection":
                    name_or_ref = entry[1]
                    target = entry[2]
                    key = name_or_ref if isinstance(name_or_ref, str) else name_or_ref.name
                    zone.output_mappings[key] = (target.node, target.index, target.name)
        elif _p_body_construct(s, zone.nodes, zone.links):
            pass
        else:
            s.next()

    s.expect('RBRACE')
    return zone


def _p_closure(s: Stream) -> core.ClosureZoneDef:
    s.expect('KW', 'closure')
    name = _p_name(s)
    location, _, _ = _p_transform(s)

    s.expect('LBRACE')
    zone = core.ClosureZoneDef(name=name, location=location)

    while not s.at('RBRACE'):
        if s.at_kw('inputs'):
            s.next()
            for entry in _p_entries(s):
                if entry[0] == "typed":
                    zone.inputs.append(core.NodeItemDef(entry[1], entry[2]))
        elif s.at_kw('outputs'):
            s.next()
            for entry in _p_entries(s):
                if entry[0] == "typed_connection":
                    zone.outputs.append(core.NodeItemDef(entry[1], entry[2]))
                    zone.output_mappings[entry[1]] = \
                        (entry[3].node, entry[3].index, entry[3].name)
                elif entry[0] == "typed":
                    zone.outputs.append(core.NodeItemDef(entry[1], entry[2]))
        elif _p_body_construct(s, zone.nodes, zone.links):
            pass
        else:
            s.next()

    s.expect('RBRACE')
    return zone


def _p_tree(s: Stream) -> core.TreeDef:
    inline = s.at_kw('inline')
    if inline:
        s.next()
    s.expect('KW', 'tree')
    name = _p_name(s)
    bl_idname = _p_type(s)

    s.expect('LBRACE')
    tree_def = core.TreeDef(name=name, bl_idname=bl_idname, inline=inline)

    while not s.at('RBRACE'):
        if s.at_kw('interface'):
            tree_def.interface = _p_interface(s)
        elif s.at_kw('repeat'):
            tree_def.zones.append(_p_repeat(s))
        elif s.at_kw('closure'):
            tree_def.zones.append(_p_closure(s))
        elif _p_body_construct(s, tree_def.nodes, tree_def.links):
            pass
        else:
            s.next()

    s.expect('RBRACE')
    return tree_def


def _p_module_path(s: Stream) -> str:
    dots = 0
    while s.at('DOT'):
        s.next()
        dots += 1
    segments = [s.expect('WORD').value]
    while s.at('DOT'):
        s.next()
        segments.append(s.expect('WORD').value)
    return '.' * dots + '.'.join(segments)


def _p_import(s: Stream) -> list[core.ImportDef]:
    s.expect('KW', 'import')
    modules = [_p_module_path(s)]
    while s.at('COMMA'):
        s.next()
        modules.append(_p_module_path(s))

    names = []
    if s.at('LBRACE'):
        s.next()
        while not s.at('RBRACE'):
            kind = s.next()
            if not (kind.kind in ('KW', 'WORD') and kind.value == 'tree'):
                raise ParseError(
                    f"import entries are typed; expected `tree \"Name\"`, "
                    f"got {kind}", kind.pos)
            names.append(s.expect('STRING').value)
            s.skip_commas()
        s.expect('RBRACE')
        if len(modules) > 1:
            raise ParseError(
                "a tree selection applies to one module; import the others "
                "separately", s.peek().pos if s.peek() else 0)
    return [core.ImportDef(module=m, names=names) for m in modules]


def parse_document(text: str) -> core.DocumentDef:
    s = Stream(tokenise(preprocessor.preprocess(text)))
    doc = core.DocumentDef()
    while s.peek() is not None:
        if s.at_kw('import'):
            doc.imports.extend(_p_import(s))
        elif s.at_kw('tree') or s.at_kw('inline'):
            doc.trees.append(_p_tree(s))
        else:
            raise ParseError(f"Expected 'tree' or 'import', got {s.peek()}",
                             s.peek().pos)
    if not doc.trees and not doc.imports:
        raise ParseError("No tree definitions found")
    return doc


def deserialise(text: str) -> list[core.TreeDef]:
    return parse_document(text).trees


# ---------------------------------------------------------------------------
# Serialise: IR -> Text
# ---------------------------------------------------------------------------

def _f(name: str) -> str:
    return f'"{name}"'


def _ft(type_id: str) -> str:
    return f'[{type_id}]'


def _fv(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return f'"{val}"'
    if isinstance(val, tuple):
        return f"({', '.join(_fv(v) for v in val)})"
    return repr(val)


def _fs(ref: SocketRef) -> str:
    """Format a socket ref: "node"(index: "name"), "node"(index), or the
    unresolved name-only form "node"("name")."""
    if ref.index < 0:
        return f'{_f(ref.node)}({_f(ref.name)})'
    if ref.name:
        return f'{_f(ref.node)}({ref.index}: {_f(ref.name)})'
    return f'{_f(ref.node)}({ref.index})'


def _fsr(index: int, name: str = "") -> str:
    """Format a local socket ref: (index: "name"), (index), or ("name")."""
    if index < 0:
        return f'({_f(name)})'
    if name:
        return f'({index}: {_f(name)})'
    return f'({index})'


def _ftrans(location, width=0.0, height=0.0) -> str:
    if location == (0.0, 0.0) and not width and not height:
        return ""
    x, y = int(round(location[0])), int(round(location[1]))
    parts = [f"{x}, {y}"]
    if width:
        parts.append(f"w={int(round(width))}")
    if height:
        parts.append(f"h={int(round(height))}")
    return f" @({', '.join(parts)})"


def _emit_node(node: core.NodeDef, links: list[core.LinkDef], indent: str) -> list[str]:
    if node.bl_idname == 'NodeFrame':
        return _emit_frame(node, indent)
    if node.bl_idname == 'NodeReroute':
        return _emit_reroute(node, links, indent)

    header = f'{indent}node {_f(node.name)} {_ft(node.bl_idname)}'
    header += _ftrans(node.location, node.width, node.height)
    if node.label and node.label != node.name:
        header += f' label="{node.label}"'

    lines = [f"{header} {{"]
    inner = indent + "  "

    for key, val in sorted(node.properties.items()):
        lines.append(f"{inner}{key} = {_fv(val)}")
    if node.node_tree_name:
        lines.append(f'{inner}node_tree = "{node.node_tree_name}"')
    if node.mute:
        lines.append(f"{inner}mute = true")
    if node.hide:
        lines.append(f"{inner}hide = true")

    offsets = core.ITEM_NODES.get(node.bl_idname)
    item_in: dict[int, core.NodeItemDef] = {}
    item_out: dict[int, core.NodeItemDef] = {}
    if offsets is not None:
        for pos, item in enumerate(node.input_items):
            item_in[offsets[0] + pos] = item
        for pos, item in enumerate(node.output_items):
            item_out[offsets[1] + pos] = item

    node_links = [l for l in links if l.target.node == node.name]
    linked_indices = {l.target.index for l in node_links}

    input_lines = []
    all_indices = sorted(linked_indices | set(node.input_values.keys())
                         | set(item_in))
    for idx in all_indices:
        matching = [l for l in node_links if l.target.index == idx]
        if idx in item_in:
            item = item_in[idx]
            base = f'{_f(item.name)}: {_ft(item.socket_type)}'
            if matching:
                input_lines.append(f"{inner}  {base} -> {_fs(matching[0].source)},")
            elif idx in node.input_values:
                input_lines.append(f"{inner}  {base} = {_fv(node.input_values[idx])},")
            else:
                input_lines.append(f"{inner}  {base},")
        elif matching:
            for link in matching:
                input_lines.append(
                    f"{inner}  {_fsr(idx, link.target.name)} -> {_fs(link.source)},"
                )
        elif idx in node.input_values:
            name = node.input_names.get(idx, "")
            input_lines.append(f"{inner}  {_fsr(idx, name)} = {_fv(node.input_values[idx])},")

    for sock_name, val in node.named_input_values.items():
        input_lines.append(f"{inner}  {_fsr(-1, sock_name)} = {_fv(val)},")

    if input_lines:
        lines.append(f"{inner}inputs {{")
        lines.extend(input_lines)
        lines.append(f"{inner}}}")

    output_indices = sorted(set(node.output_values.keys()) | set(item_out))
    if output_indices:
        output_lines = []
        for idx in output_indices:
            if idx in item_out:
                item = item_out[idx]
                output_lines.append(f"{inner}  {_f(item.name)}: {_ft(item.socket_type)},")
            else:
                name = node.output_names.get(idx, "")
                output_lines.append(f"{inner}  {_fsr(idx, name)} = {_fv(node.output_values[idx])},")
        lines.append(f"{inner}outputs {{")
        lines.extend(output_lines)
        lines.append(f"{inner}}}")

    lines.append(f"{indent}}}")
    return lines


def _emit_reroute(node: core.NodeDef, links: list[core.LinkDef], indent: str) -> list[str]:
    header = f'{indent}reroute {_f(node.name)}'
    header += _ftrans(node.location)
    matching = [l for l in links if l.target.node == node.name]
    if matching:
        header += f' -> {_fs(matching[0].source)}'
    return [header]


def _emit_frame(node: core.NodeDef, indent: str) -> list[str]:
    header = f'{indent}frame {_f(node.name)}'
    header += _ftrans(node.location, node.width, node.height)
    if node.label and node.label != node.name:
        header += f' label="{node.label}"'
    lines = [f"{header} {{"]
    if node.children:
        inner = indent + "  "
        lines.append(f"{inner}children {{")
        for child in node.children:
            lines.append(f"{inner}  {_f(child)},")
        lines.append(f"{inner}}}")
    lines.append(f"{indent}}}")
    return lines


def _emit_interface(interface: list[core.InterfaceSocketDef], indent: str) -> list[str]:
    if not interface:
        return []
    ins = [s for s in interface if s.direction == 'INPUT']
    outs = [s for s in interface if s.direction == 'OUTPUT']
    lines = [f"{indent}interface {{"]
    inner = indent + "  "
    inner2 = inner + "  "
    def _fmt(s):
        line = f"{inner2}{_f(s.name)}: {_ft(s.socket_type)}"
        if s.default is not None:
            line += f" = {_fv(s.default)}"
        return line + ","

    if ins:
        lines.append(f"{inner}inputs {{")
        for s in ins:
            lines.append(_fmt(s))
        lines.append(f"{inner}}}")
    if outs:
        lines.append(f"{inner}outputs {{")
        for s in outs:
            lines.append(_fmt(s))
        lines.append(f"{inner}}}")
    lines.append(f"{indent}}}")
    return lines


def _emit_repeat(zone: core.RepeatZoneDef, indent: str) -> list[str]:
    header = f'{indent}repeat {_f(zone.name)}'
    header += _ftrans(zone.location)
    lines = [f"{header} {{"]
    inner = indent + "  "

    # Iterations
    if isinstance(zone.iterations, tuple):
        node_name, idx, sock_name = zone.iterations
        lines.append(f'{inner}iterations -> {_fs(SocketRef(node_name, idx, sock_name))}')
    else:
        lines.append(f"{inner}iterations = {zone.iterations}")

    if zone.items:
        lines.append(f"{inner}items {{")
        for item in zone.items:
            line = f'{inner}  {_f(item.name)}: {_ft(item.socket_type)}'
            if item.initial_connection is not None:
                node_name, idx, sock_name = item.initial_connection
                line += f" -> {_fs(SocketRef(node_name, idx, sock_name))}"
            elif item.default_value is not None:
                line += f" = {_fv(item.default_value)}"
            lines.append(f"{line},")
        lines.append(f"{inner}}}")
        lines.append("")

    for node in zone.nodes:
        lines.extend(_emit_node(node, zone.links, inner))
        lines.append("")

    if zone.output_mappings:
        lines.append(f"{inner}outputs {{")
        for item_name, mapping in sorted(zone.output_mappings.items()):
            lines.append(f'{inner}  {_f(item_name)} -> {_fs(SocketRef(*mapping))},')
        lines.append(f"{inner}}}")

    if lines and lines[-1] == "":
        lines.pop()
    lines.append(f"{indent}}}")
    return lines


def _emit_closure(zone: core.ClosureZoneDef, indent: str) -> list[str]:
    header = f'{indent}closure {_f(zone.name)}'
    header += _ftrans(zone.location)
    lines = [f"{header} {{"]
    inner = indent + "  "

    if zone.inputs:
        lines.append(f"{inner}inputs {{")
        for item in zone.inputs:
            lines.append(f'{inner}  {_f(item.name)}: {_ft(item.socket_type)},')
        lines.append(f"{inner}}}")
        lines.append("")

    for node in zone.nodes:
        lines.extend(_emit_node(node, zone.links, inner))
        lines.append("")

    if zone.outputs:
        lines.append(f"{inner}outputs {{")
        for item in zone.outputs:
            line = f'{inner}  {_f(item.name)}: {_ft(item.socket_type)}'
            mapping = zone.output_mappings.get(item.name)
            if mapping is not None:
                line += f' -> {_fs(SocketRef(*mapping))}'
            lines.append(f"{line},")
        lines.append(f"{inner}}}")

    if lines and lines[-1] == "":
        lines.pop()
    lines.append(f"{indent}}}")
    return lines


def _emit_zone(zone: core.ZoneDef, indent: str) -> list[str]:
    if isinstance(zone, core.ClosureZoneDef):
        return _emit_closure(zone, indent)
    return _emit_repeat(zone, indent)


def _emit_tree(tree_def: core.TreeDef, indent: str) -> list[str]:
    kw = 'inline tree' if tree_def.inline else 'tree'
    lines = [f'{indent}{kw} {_f(tree_def.name)} {_ft(tree_def.bl_idname)} {{']
    ti = indent + "  "

    iface = _emit_interface(tree_def.interface, ti)
    if iface:
        lines.extend(iface)
        lines.append("")

    for node in tree_def.nodes:
        lines.extend(_emit_node(node, tree_def.links, ti))
        lines.append("")

    for zone in tree_def.zones:
        lines.extend(_emit_zone(zone, ti))
        lines.append("")

    if lines and lines[-1] == "":
        lines.pop()
    lines.append(f"{indent}}}")
    return lines


def _emit_import(imp: core.ImportDef) -> str:
    if imp.names:
        entries = ", ".join(f'tree {_f(n)}' for n in imp.names)
        return f'import {imp.module} {{ {entries} }}'
    return f'import {imp.module}'


def serialise_document(doc: core.DocumentDef) -> str:
    parts = [_emit_import(i) for i in doc.imports]
    for td in doc.trees:
        if parts:
            parts.append("")
        parts.extend(_emit_tree(td, ""))
    return "\n".join(parts)


def serialise(tree_def: core.TreeDef, indent: str = "") -> str:
    all_trees: list[core.TreeDef] = []
    seen: set[str] = set()
    queue = collections.deque([tree_def])
    while queue:
        current = queue.popleft()
        if current.name in seen:
            continue
        seen.add(current.name)
        all_trees.append(current)
        for child in current.groups.values():
            queue.append(child)
    all_trees.reverse()

    parts: list[str] = []
    for td in all_trees:
        if parts:
            parts.append("")
        parts.extend(_emit_tree(td, indent))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Schema -> Text
# ---------------------------------------------------------------------------

def node_type_to_index_entry(ndef) -> str:
    lines = [f'[{ndef.bl_idname}] "{ndef.bl_label}"']
    # Collect unique socket types across all variants
    in_types = []
    out_types = []
    for v in ndef.variants:
        for s in v.inputs:
            short = s.bl_idname.replace("NodeSocket", "")
            if short not in in_types:
                in_types.append(short)
        for s in v.outputs:
            short = s.bl_idname.replace("NodeSocket", "")
            if short not in out_types:
                out_types.append(short)
    if in_types:
        lines.append(f'  inputs: {", ".join(in_types)}')
    if out_types:
        lines.append(f'  outputs: {", ".join(out_types)}')
    if ndef.properties:
        enum_props = [p for p in ndef.properties if p.enum_values]
        if enum_props:
            lines.append(f'  properties: {", ".join(p.identifier for p in enum_props)}')
    return "\n".join(lines)


def node_type_to_detail(ndef) -> str:
    lines = [f'type {_ft(ndef.bl_idname)} {{']
    lines.append(f'  label = "{ndef.bl_label}"')
    if ndef.description:
        lines.append(f'  description = "{ndef.description}"')
    if ndef.properties:
        lines.append("  properties {")
        for prop in ndef.properties:
            prop_line = f"    {prop.identifier}: {prop.type}"
            if prop.enum_values:
                lines.append(prop_line + " = [")
                for val in prop.enum_values:
                    lines.append(f"      {val},")
                lines.append("    ]")
                continue
            elif prop.default is not None:
                prop_line += f" = {_fv(prop.default)}"
            lines.append(prop_line)
        lines.append("  }")
    for variant in ndef.variants:
        if variant.variant_key:
            key_str = ", ".join(f'{k}="{v}"' for k, v in variant.variant_key.items())
            lines.append(f"  variant ({key_str}) {{")
        else:
            lines.append("  sockets {")
        if variant.inputs:
            lines.append("    inputs {")
            for s in variant.inputs:
                val_str = f" = {_fv(s.default_value)}" if s.default_value is not None else ""
                lines.append(f"      {s.name}: {_ft(s.bl_idname)}{val_str}")
            lines.append("    }")
        if variant.outputs:
            lines.append("    outputs {")
            for s in variant.outputs:
                lines.append(f"      {s.name}: {_ft(s.bl_idname)}")
            lines.append("    }")
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines)


def schema_to_index(node_defs: list, socket_types: list[str] | None = None) -> str:
    lines = []
    lines.append(f"# {len(node_defs)} node types")
    lines.append("#")
    lines.append("# Each entry shows the bl_idname in brackets, the display label in quotes,")
    lines.append("# followed by socket types and configurable properties.")
    lines.append("# For full details, read the matching .nodetypes file in this folder.")
    lines.append("")
    if socket_types:
        lines.append("# Socket types:")
        # Wrap socket types at ~80 chars per line
        chunk = "#   "
        for i, st in enumerate(socket_types):
            addition = st + (", " if i < len(socket_types) - 1 else "")
            if len(chunk) + len(addition) > 80:
                lines.append(chunk.rstrip(", "))
                chunk = "#   " + addition
            else:
                chunk += addition
        lines.append(chunk)
        lines.append("")
    for ndef in node_defs:
        lines.append(node_type_to_index_entry(ndef))
        lines.append("")
    return "\n".join(lines)
