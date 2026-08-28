# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sam Warren
import os
import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .code2node import core, expand, format, layout, loader, schema, validate
from .code2node.core import NodeIOError


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class CODE2NODE_OT_Export(bpy.types.Operator, ExportHelper):
    """Export the active node tree to a text file"""

    bl_idname = "code2node.export"
    bl_label = "Export Node Tree"
    filename_ext = ".nodes"

    filter_glob: StringProperty(default="*.nodes", options={'HIDDEN'})
    verbose: BoolProperty(
        name="Verbose",
        description="Include all socket values, even defaults",
        default=False,
    )

    def execute(self, context):
        node_tree = self._get_active_tree(context)
        if node_tree is None:
            self.report({'ERROR'}, "No active node tree found")
            return {'CANCELLED'}

        try:
            tree_def = core.tree_to_ir(node_tree, verbose=self.verbose)
            text = format.serialise(tree_def)
        except NodeIOError as e:
            self.report({'ERROR'}, f"Export failed: {e}")
            return {'CANCELLED'}

        with open(self.filepath, 'w', encoding='utf-8') as f:
            f.write(text)

        self.report({'INFO'}, f"Exported '{node_tree.name}' to {self.filepath}")
        return {'FINISHED'}

    def _get_active_tree(self, context):
        if context.space_data and hasattr(context.space_data, 'edit_tree'):
            return context.space_data.edit_tree
        if context.active_object and context.active_object.active_material:
            mat = context.active_object.active_material
            if mat.use_nodes:
                return mat.node_tree
        return None


class CODE2NODE_OT_Import(bpy.types.Operator, ImportHelper):
    """Import a node tree from a text file"""

    bl_idname = "code2node.import_tree"
    bl_label = "Import Node Tree"
    filename_ext = ".nodes"

    filter_glob: StringProperty(default="*.nodes", options={'HIDDEN'})

    def execute(self, context):
        try:
            # Resolves import statements relative to the file: the closure
            # arrives as a flat list in build order, document trees last.
            tree_defs = loader.load(self.filepath)
        except NodeIOError as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            return {'CANCELLED'}

        errors = [i for i in validate.resolve_names(tree_defs, None)
                  if i.severity == "error"]
        if errors:
            for issue in errors:
                self.report({'ERROR'}, str(issue))
            return {'CANCELLED'}

        try:
            tree_defs = expand.expand_inlines(tree_defs)
        except NodeIOError as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            return {'CANCELLED'}
        if not tree_defs:
            self.report({'ERROR'}, "Document contains only inline trees")
            return {'CANCELLED'}

        existing = {nt.name: nt for nt in bpy.data.node_groups}
        all_warnings: list[str] = []

        # The last tree in the file is the root — import it into the active
        # node tree if available, so it replaces the current material rather
        # than creating a detached node group. Only a target of the same
        # tree type can receive it.
        target_tree = self._get_active_tree(context)
        if target_tree is not None and \
                target_tree.bl_idname != tree_defs[-1].bl_idname:
            target_tree = None

        try:
            for tree_def in tree_defs:
                is_root = (tree_def is tree_defs[-1])
                target = target_tree if is_root else None
                _, warnings = core.ir_to_tree(tree_def, existing, target_tree=target)
                all_warnings.extend(warnings)
        except NodeIOError as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            return {'CANCELLED'}

        for w in all_warnings:
            self.report({'WARNING'}, w)

        names = ", ".join(td.name for td in tree_defs)
        self.report({'INFO'}, f"Imported: {names}")
        return {'FINISHED'}

    def _get_active_tree(self, context):
        if context.space_data and hasattr(context.space_data, 'edit_tree'):
            return context.space_data.edit_tree
        if context.active_object and context.active_object.active_material:
            mat = context.active_object.active_material
            if mat.use_nodes:
                return mat.node_tree
        return None


class CODE2NODE_OT_AutoLayout(bpy.types.Operator):
    """Arrange the active node tree: dataflow left to right, frames and
    zones as blocks"""

    bl_idname = "code2node.auto_layout"
    bl_label = "Auto Layout"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        node_tree = getattr(context.space_data, 'edit_tree', None)
        if node_tree is None:
            self.report({'ERROR'}, "No active node tree found")
            return {'CANCELLED'}

        try:
            tree_def = core.tree_to_ir(node_tree)
        except NodeIOError as e:
            self.report({'ERROR'}, f"Layout failed: {e}")
            return {'CANCELLED'}

        # Drawn dimensions include the UI scale; node locations don't.
        ui_scale = context.preferences.system.ui_scale
        sizes = {n.name: (n.dimensions.x / ui_scale, n.dimensions.y / ui_scale)
                 for n in node_tree.nodes
                 if n.bl_idname != 'NodeFrame' and n.dimensions.x > 0}
        layout.layout_tree(tree_def, sizes=sizes)

        def move(node_def):
            node = node_tree.nodes.get(node_def.name)
            if node is not None:
                node.hide = node_def.hide
                node.location = node_def.location

        for node_def in tree_def.nodes:
            move(node_def)
        for zone_def in tree_def.zones:
            for node_def in zone_def.nodes:
                move(node_def)
            input_node = node_tree.nodes.get(zone_def.name)
            if input_node is not None:
                input_node.location = zone_def.location
                paired = getattr(input_node, 'paired_output', None)
                if paired is not None and zone_def.output_location is not None:
                    paired.location = zone_def.output_location
        return {'FINISHED'}


class CODE2NODE_OT_GenerateSchema(bpy.types.Operator):
    """Generate node type schema for all tree types (shader, geometry, compositor)"""

    bl_idname = "code2node.generate_schema"
    bl_label = "Generate Node Schema"

    directory: StringProperty(
        name="Output Directory",
        subtype='DIR_PATH',
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            registry = schema.extract_schema()
        except NodeIOError as e:
            self.report({'ERROR'}, f"Schema generation failed: {e}")
            return {'CANCELLED'}

        schema_dir = os.path.join(self.directory, "node_schema")
        os.makedirs(schema_dir, exist_ok=True)

        with open(os.path.join(schema_dir, "README.md"), 'w', encoding='utf-8') as f:
            f.write(format.SCHEMA_README)

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

        self.report({'INFO'}, f"Generated schema: {total} node types -> {schema_dir}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class CODE2NODE_PT_Panel(bpy.types.Panel):
    """code2node panel in the node editor sidebar"""

    bl_label = "code2node"
    bl_idname = "CODE2NODE_PT_panel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "code2node"

    def draw(self, context):
        layout = self.layout
        layout.operator("code2node.export", icon='EXPORT')
        layout.operator("code2node.import_tree", icon='IMPORT')
        layout.operator("code2node.auto_layout", icon='NODETREE')
        layout.separator()
        layout.operator("code2node.generate_schema", icon='FILE_TEXT')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    CODE2NODE_OT_Export,
    CODE2NODE_OT_Import,
    CODE2NODE_OT_AutoLayout,
    CODE2NODE_OT_GenerateSchema,
    CODE2NODE_PT_Panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
