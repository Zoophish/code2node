# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sam Warren
bl_info = {
    "name": "code2node",
    "description": "Serialise and deserialise Blender node trees to a text DSL.",
    "author": "Sam Warren",
    "version": (0, 1, 0),
    "category": "Node",
    "blender": (5, 0, 0),
    "location": "Node Editor > Toolbar > code2node",
}

from . import core, format, schema
from .core import NodeIOError

try:
    import bpy  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    from .blender_addon import register, unregister

    if __name__ == "__main__":
        register()
