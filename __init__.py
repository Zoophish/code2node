# Copyright (C) 2026, Sam Warren, All rights reserved.
bl_info = {
    "name": "Node IO",
    "description": "Serialise and deserialise Blender node trees to a text DSL.",
    "author": "Sam Warren",
    "version": (0, 1, 0),
    "category": "Node",
    "blender": (5, 0, 0),
    "location": "Node Editor > Toolbar > Node IO",
}

from . import core, format, schema
from .core import NodeIOError

try:
    import bpy  # noqa: F401
except ModuleNotFoundError:
    # Outside Blender: parsing, serialisation, schema loading and validation
    # all work; only the addon UI and bpy round-tripping need Blender.
    pass
else:
    from .blender_addon import register, unregister  # noqa: F401

    if __name__ == "__main__":
        register()
