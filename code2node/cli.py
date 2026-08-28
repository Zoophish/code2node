#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sam Warren
"""
Usage:
    blender --background --python code2node/cli.py -- <command> [args]

Commands:
    schema <output_dir>
        Generate the full node type schema for all tree types available in
        the Blender instance.

    query <registry.json> (-s <pattern> | <BlIdname> ...)
        Search node types by substring, or print a type's properties and
        socket signatures in ref syntax.

    validate <file.nodes>
        Parse a .nodes file and report any errors.

    roundtrip <file.nodes> [output.nodes]
        Parse a .nodes file, serialise it back, and write the result.
        Useful for checking that a hand-edited or agent-generated file
        is well-formed.
"""
import sys
import os

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "code2node"


def _get_args():
    try:
        idx = sys.argv.index("--")
        return sys.argv[idx + 1:]
    except ValueError:
        return []


def cmd_schema(output_dir):
    import json
    from . import schema, format

    schema_dir = os.path.join(output_dir, "node_schema")
    os.makedirs(schema_dir, exist_ok=True)
    registry = schema.extract_schema()

    with open(os.path.join(schema_dir, "README.md"), 'w', encoding='utf-8') as f:
        f.write(format.SCHEMA_README)

    import bpy
    data = schema.registry_to_dict(registry)
    data["blender_version"] = bpy.app.version_string
    # Compact on purpose: registry.json is tool data (query/validate load
    # it), the .nodetypes files are the browsable representation.
    with open(os.path.join(schema_dir, "registry.json"), 'w', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'))

    total = 0
    for tree_type, node_defs in registry.tree_types.items():
        if not node_defs:
            continue
        type_dir = os.path.join(schema_dir, tree_type)
        os.makedirs(type_dir, exist_ok=True)

        index_text = format.schema_to_index(node_defs, registry.socket_types)
        with open(os.path.join(type_dir, "index.nodetypes"), 'w', encoding='utf-8') as f:
            f.write(index_text)

        for ndef in node_defs:
            detail_text = format.node_type_to_detail(ndef)
            filepath = os.path.join(type_dir, f"{ndef.bl_idname}.nodetypes")
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(detail_text)

        total += len(node_defs)

    print(f"Generated schema: {total} node types -> {schema_dir}")


def cmd_query(registry_path, args):
    import json

    with open(registry_path, encoding='utf-8') as f:
        reg = json.load(f)
    nodes = {}
    for defs in reg["tree_types"].values():
        for d in defs:
            nodes.setdefault(d["bl_idname"], d)

    def show(d):
        print(f'== {d["bl_idname"]}  ({d["bl_label"]})')
        for p in d["properties"]:
            enum = f' enum={p["enum_values"]}' if p.get("enum_values") else ""
            print(f'   prop {p["identifier"]} = {p["default"]!r}{enum}')
        for v in d["variants"]:
            if v["variant_key"]:
                print(f'   -- variant {v["variant_key"]}')
            for i, s in enumerate(v["inputs"]):
                print(f'   in  ({i}: "{s["name"]}") {s["bl_idname"]}'
                      f' = {s["default_value"]!r}')
            for i, s in enumerate(v["outputs"]):
                print(f'   out ({i}: "{s["name"]}") {s["bl_idname"]}')
        print()

    if args and args[0] == "-s":
        pat = args[1].lower()
        for key in sorted(nodes):
            d = nodes[key]
            if pat in key.lower() or pat in d["bl_label"].lower():
                print(f'{key} | {d["bl_label"]}')
    else:
        for name in args:
            d = nodes.get(name)
            if d is None:
                print(f"!! {name} not found", file=sys.stderr)
            else:
                show(d)


def cmd_validate(filepath, schema_path=None):
    from . import format, loader

    try:
        tree_defs = loader.load(filepath)
    except format.ParseError as e:
        print(f"PARSE ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except loader.LoadError as e:
        print(f"IMPORT ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    for td in tree_defs:
        node_count = len(td.nodes)
        link_count = len(td.links)
        group_nodes = sum(1 for n in td.nodes if n.node_tree_name)
        print(f"  tree '{td.name}' ({td.bl_idname}): "
              f"{node_count} nodes, {link_count} links, {group_nodes} group nodes")

    if schema_path:
        from . import schema as schema_mod
        from . import validate as validate_mod

        registry = schema_mod.load_registry(schema_path)
        issues = validate_mod.validate(tree_defs, registry)
        for issue in issues:
            print(f"  {issue}")
        errors = sum(1 for i in issues if i.severity == "error")
        warnings = len(issues) - errors
        if not errors:
            from . import expand
            try:
                expand.expand_inlines(tree_defs)
            except expand.ExpandError as e:
                print(f"  EXPAND ERROR: {e}")
                errors += 1
        if errors:
            print(f"FAILED: {errors} error(s), {warnings} warning(s)")
            sys.exit(1)
        print(f"OK: {len(tree_defs)} tree(s) valid against schema "
              f"({warnings} warning(s))")
    else:
        print(f"OK: {len(tree_defs)} tree(s) parsed successfully")


def cmd_roundtrip(input_path, output_path=None):
    from . import format

    with open(input_path, 'r', encoding='utf-8') as f:
        text = f.read()

    try:
        doc = format.parse_document(text)
    except format.ParseError as e:
        print(f"PARSE ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    output_text = format.serialise_document(doc)

    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(output_text)
        print(f"Roundtripped to {output_path}")
    else:
        print(output_text)


def main():
    args = _get_args()
    if not args:
        print(__doc__)
        sys.exit(1)

    command = args[0]

    if command == "schema":
        if len(args) < 2:
            print("Usage: ... -- schema <output_dir>", file=sys.stderr)
            sys.exit(1)
        cmd_schema(args[1])

    elif command == "query":
        if len(args) < 3:
            print("Usage: ... -- query <registry.json> (-s <pattern> | <BlIdname> ...)",
                  file=sys.stderr)
            sys.exit(1)
        cmd_query(args[1], args[2:])

    elif command == "validate":
        if len(args) < 2:
            print("Usage: ... -- validate <file.nodes> [registry.json]", file=sys.stderr)
            sys.exit(1)
        schema_path = args[2] if len(args) > 2 else None
        cmd_validate(args[1], schema_path)

    elif command == "roundtrip":
        if len(args) < 2:
            print("Usage: ... -- roundtrip <input.nodes> [output.nodes]", file=sys.stderr)
            sys.exit(1)
        output = args[2] if len(args) > 2 else None
        cmd_roundtrip(args[1], output)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
