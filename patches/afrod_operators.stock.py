import re
import mathutils
import random
import math
import os
import bmesh
import bpy
from collections import deque
import json
import time
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy.props import StringProperty, BoolProperty, BoolVectorProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty
from mathutils.kdtree import KDTree
from mathutils.bvhtree import BVHTree
from mathutils import Vector
from datetime import datetime
import numpy as np
bl_info = {
    "name": "OPCD Mesh Editing Tools",
    "author": "Matthew P. (aka DPRoberts) and Alex Frodyma (aka afrod_22)",
    "version": (2, 6),
    "blender": (4, 3, 2),
    "location": "Right side panel > OPCD Tools",
    "warning": "",
    "url":  "http://zerosandonesgcd.com",
    "category": "Mesh",
}


# --- OPCD metadata helpers (robust mesh<->blend pairing) ---
# New OPCD pipeline stores Blender custom properties on imported objects:
#   obj["opcd_parent_id"] : stable id used to pair split pieces back to the source
#   obj["opcd_kind"]      : "mesh" or "blend"
#   obj["opcd_shape_id"]  : full id (may include _pieceN/_holeN)
#
# Use these instead of fragile name hacks (e.g. Mesh->Blend string replace),
# which break when blend material tokens change (B_ prefix, split-blend names).


def opcd_find_blends_for_mesh(mesh_obj):
    # Return all blend objects that correspond to mesh_obj (handles split blends).
    pid = None
    try:
        pid = mesh_obj.get('opcd_parent_id')
    except Exception:
        pid = None

    blends = []
    if pid:
        for o in bpy.data.objects:
            if o.type != 'MESH':
                continue
            try:
                if o.get('opcd_kind') == 'blend' and o.get('opcd_parent_id') == pid:
                    blends.append(o)
            except Exception:
                pass
        blends.sort(key=lambda o: o.name)
        return blends

    # Fallback for older .blend files without metadata
    mesh_name = getattr(mesh_obj, 'name', '') or ''
    # Strip Blender numeric suffix like ".001" so name-based matching stays stable.
    if len(mesh_name) > 4 and mesh_name[-4] == '.' and mesh_name[-3:].isdigit():
        mesh_name = mesh_name[:-4]

    if mesh_name.endswith('Mesh'):
        base_name = mesh_name[:-4]
        blend_name_substring = base_name + 'Blend'
        blends = [
            o for o in bpy.data.objects if blend_name_substring in o.name and o != mesh_obj]
        blends.sort(key=lambda o: o.name)
        if blends:
            return blends

        # Extra fallback: _-_Mesh -> _-_Blend
        if mesh_name.endswith('_-_Mesh'):
            cand = mesh_name[:-len('_-_Mesh')] + '_-_Blend'
            if cand in bpy.data.objects:
                return [bpy.data.objects[cand]]

        # Extra fallback for split-blend naming:
        # Newer exporters may insert material tokens between <shape> and "_-_Blend",
        # so "<base>Blend" substring matching fails. We fall back to matching the
        # stable SVG path id (e.g. "path765") if present.
        m = re.search(r'(path\\d+)', mesh_name)
        if m:
            path_id = m.group(1)
            blends = [
                o for o in bpy.data.objects
                if o.type == 'MESH'
                and o != mesh_obj
                and 'Blend' in (getattr(o, 'name', '') or '')
                and path_id in (getattr(o, 'name', '') or '')
            ]
            blends.sort(key=lambda o: o.name)
            if blends:
                return blends

    return []


version = '01132026'


class opcdtoolsSettings(PropertyGroup):

    bunkerdepth: FloatProperty(
        name="Bunker Lower Depth",
        default=-0.3,
        min=-10,
        max=10,
        description="Depth of edge on Bunkers (if present) - rec -0.03")

    potbunkerdepth: FloatProperty(
        name="Depth (m)",
        default=-0.5,
        min=-2.0,
        max=0.0,
        description="Maximum depth of Pot Bunker (if present) - rec -1.0")

    terrainsmooth: FloatProperty(
        name="Terrain Smooth",
        default=1.5,
        min=0.0,
        max=4.0,
        description="Level of smoothing to Terrain - default 1.5")

    wateroutset: FloatProperty(
        name="Outset (m)",
        default=2.0,
        min=0.0,
        max=10.0,
        description="Water Plane Outset (m)")

    wallheight: FloatProperty(
        name="Height (m)",
        default=0.5,
        min=0.0,
        max=10.0,
        description="Raised Bed Height (m)")

    wallwidth: FloatProperty(
        name="Width (m)",
        default=0.20,
        min=0.0,
        max=2.0,
        description="Raised Bed Width (m)")

    inner_terrain_size: FloatProperty(
        name="Terrain Size (m)",
        default=2000,
        min=0.0,
        max=15000,
        description="Edge Length of Inner Terrain (m)")

    outer_terrain_size: FloatProperty(
        name="Terrain Size (m)",
        default=4000,
        min=0.0,
        max=15000,
        description="Edge Length of Outer Terrain (m)")

    ripple_height: IntProperty(
        name="Height (cm)",
        default=2,
        min=0,
        max=50,
        description="Randomly Raise Vertices (cm)")

    ripple_inset: IntProperty(
        name="Inset",
        default=1,
        min=1,
        max=10,
        description="Inset Vertex Rows to Apply Ripple Effect")

    ripple_smooth: FloatProperty(
        name="Smooth Factor",
        default=0.2,
        min=0,
        max=1,
        description="Smoothing Factor")

    random_amt: FloatProperty(
        name="Percent of Fill",
        default=1,
        min=0,
        max=1,
        precision=4,
        description="Percentage of Vertices that will be Selected")

    blend_inset_value: FloatProperty(
        name="Blend Inset Value",
        default=0.12,
        min=0,
        max=1,
        description="Value of Inset Width")

    stake_offset_value: FloatProperty(
        name="Planting Offset Value",
        default=0.12,
        min=-1,
        max=1,
        description="Value of Offset Width")

    export_folder: StringProperty(
        name="Export Folder",
        description="Choose a folder for exporting files",
        default="",
        subtype='DIR_PATH'
    )

    intColorItems = (
        ('Red', 'Red', ''),
        ('Yellow', 'Yellow', ''),
        ('White', 'White', ''),
        ('Blue', 'Blue', '')
    )

    stake_color: EnumProperty(
        items=intColorItems,
        name="Stake Colors",
        description="Plant selected color hazard stake",
        default='Red'
    )

    stake_spacing: FloatProperty(
        name="Spacing (m)",
        default=8.0,
        min=0.0,
        max=100.0,
        description="Spacing of stakes (m)")

    intStairsItems = (
        ('Wood Type 1', 'Wood Type 1', ''),
        ('Stone Type 1', 'Stone Type 1', '')
    )

    stairs_type: EnumProperty(
        items=intStairsItems,
        name="Stairs Types",
        description="Different Stairs Types to Plant",
        default='Wood Type 1'
    )

    intBridgeItems = (
        ('Wood Type 1', 'Wood Type 1', ''),
        ('Wood Type 2', 'Wood Type 2', ''),
        ('Stone Type 1', 'Stone Type 1', ''),
        ('Stone Type 2', 'Stone Type 2', '')
    )

    bridge_type: EnumProperty(
        items=intBridgeItems,
        name="Bridge Types",
        description="Different Bridge Types to Plant",
        default='Wood Type 1'
    )

    intBulkheadItems = (
        ('Wood_Wall_Inner', 'Wood Wall', ''),
        ('Stone_Wall_Inner', 'Stone Wall', ''),
        ('Rail_Tie_Inner', 'Rail Tie', '')
    )

    bulkhead_type: EnumProperty(
        items=intBulkheadItems,
        name="Bulkhead Types",
        description="Different Bulkhead Types to Plant",
        default='Wood_Wall_Inner'
    )

    retopo: FloatProperty(
        name="Retopo Edge Setting",
        default=0.9,
        min=0.0,
        max=4.0,
        description="Boundary Align Remesh Edge Length Setting")

    autogen_terrain: BoolProperty(
        name="Auto Generate Outer Terrain",
        description="Checking box will Automatically generate Outer Terrain",
        default=False)

    # bunker_concave : BoolProperty(
    #     name= "Concave",
    #     description= "Checking box will create a concave shape to Inner faces of bunker",
    #     default=False)

    intMaterialTypes = (
        ('Tee', 'Tee', ''),
        ('Fairway', 'Fairway', ''),
        ('Semi', 'Semi', ''),
        ('Green', 'Green', ''),
        ('Rough', 'Rough', ''),
        ('Deep', 'Deep', ''),
        ('Pinestraw', 'Pinestraw', ''),
        ('Bunker', 'Bunker', ''),
        ('Concrete', 'Concrete', ''),
        ('Water_Base_Lake', 'Water_Base_Lake', ''),
        ('Water_Base_Creek', 'Water_Base_Creek', ''),
        ('Custom1', 'Custom1', ''),
        ('Custom2', 'Custom2', ''),
        ('Custom3', 'Custom3', ''),
        ('Custom4', 'Custom4', ''),
        # ('Hole99', 'Hole99',''),
    )

    mat_selection: EnumProperty(
        items=intMaterialTypes,
        name="",
        description="Select Meshes Based on Material Name",
        default='Tee'
    )

    blend_selection: EnumProperty(
        items=intMaterialTypes,
        name="",
        description="Select Blends Based on Material Name",
        default='Tee'
    )

    separate_mat_selection: EnumProperty(
        items=intMaterialTypes,
        name="",
        description="Assign Separating Mesh Name and Materials to Dropdown",
        default='Tee'
    )

    mat_change: EnumProperty(
        items=intMaterialTypes,
        name="",
        description="Change Mesh Name and Materials",
        default='Tee'
    )

    vtx_group_name: EnumProperty(
        items=intMaterialTypes,
        name="",
        description="Assign Group Name",
        default='Rough'
    )

    autogen_mat: EnumProperty(
        items=intMaterialTypes,
        name="",
        description="Different Material Types to assign to Outer Terrain",
        default='Rough'
    )

    intSelectionItems = (
        ('All', 'All', ''),
        ('Selected', 'Selected', '')
    )

    intTerrainItems = (
        ('Terrain', 'Terrain', ''),
        ('Outer', 'Outer', '')
    )

    terrain_selection_type: EnumProperty(
        items=intTerrainItems,
        name="Import Selection",
        description=" ",
        default='Terrain'
    )

    conform_selection_type: EnumProperty(
        items=intSelectionItems,
        name="Selection",
        description=" ",
        default='Selected'
    )

    bunker_selection_type: EnumProperty(
        items=intSelectionItems,
        name="Selection",
        description=" ",
        default='Selected'
    )

    bunker_from_conformed: BoolProperty(
        name="Conform Before Dig",
        description="Conform the bunker mesh before performing the dig",
        default=True
    )

    curbs_selection_type: EnumProperty(
        items=intSelectionItems,
        name="Selection",
        description=" ",
        default='Selected'
    )

    tees_selection_type: EnumProperty(
        items=intSelectionItems,
        name="Selection",
        description=" ",
        default='Selected'
    )

    intPaintItems = (
        ('clean', 'Clean', ''),
        ('wet', 'Wet', ''),
        ('pot', 'Deep/Pot', ''),
    )

    bunker_paint_type: EnumProperty(
        items=intPaintItems,
        name="Paint Style for Bunker",
        description=" ",
        default='clean'
    )

    intWaterPaintItems = (
        ('clean', 'Clean', ''),
        ('wet', 'Wet', ''),
        ('hazard', 'Hazard Line 3DG', ''),
    )

    water_paint_type: EnumProperty(
        items=intWaterPaintItems,
        name="Paint Style for Bunker",
        description=" ",
        default='clean'
    )

    bunker_lip_depth: FloatProperty(
        name="Lip Depth",
        description="Lower Bunker Lip to set depth",
        default=0.05,
        precision=4)

    bunker_inner_depth: FloatProperty(
        name="Inner Depth",
        description="Lower Inner portion of Bunker set depth",
        default=0.04,
        precision=4)

    bunker_dig_inset: IntProperty(
        name="Dig Inset",
        description="Lower Inner portion of Bunker set depth",
        min=0,
        default=2)

    bunker_dig_shape: FloatProperty(
        name="Bunker dig steepness",
        description="Steeper or shallower interior bunker dig",
        min=-100.0,
        max=100.0,
        default=1.0)

    bunker_dig_depth: FloatProperty(
        name="Bunker interior dig depth",
        description="Depth to dig interior of bunkers",
        min=-100.0,
        max=100.0,
        default=1.0)

    bunker_grass_depth: FloatProperty(
        name="Grass Blend Depth",
        description="Lower Grass Blend portion of Bunker set depth",
        default=0.02,
        precision=4)

    bunker_xy_shift: FloatProperty(
        name="Widen Bunker Loop",
        description="Widen X and Y of Bunker Interior",
        default=0.0,
        precision=4)

    pot_inset: FloatProperty(
        name="Step Inset for Potwall",
        description="Degree of Potwall incline",
        default=75)

    intBunkerLipType = (
        ('concave', 'Concave', ''),
        ('convex', 'Convex', ''),
    )

    bunkerlip_type: EnumProperty(
        items=intBunkerLipType,
        name="Edge Profile",
        description="Edge Profile",
        default='concave'
    )

    intWaterLipType = (
        ('convex', 'Convex', ''),
        ('concave', 'Concave', ''),
    )

    waterlip_type: EnumProperty(
        items=intWaterLipType,
        name="Edge Profile",
        description="Edge Profile",
        default='convex'
    )

    waterplane_selection_type: EnumProperty(
        items=intSelectionItems,
        name="Selection",
        description=" ",
        default='Selected'
    )

    water_selection_type: EnumProperty(
        items=intSelectionItems,
        name="Selection",
        description=" ",
        default='Selected'
    )

    water_lip_depth: FloatProperty(
        name="Lip Depth",
        description="Lower Water Base Lip to set depth",
        default=0.05)

    water_inner_depth: FloatProperty(
        name="Inner Depth",
        description="Lower Inner portion of Water Base to set depth",
        default=2)

    water_dig_shape: FloatProperty(
        name="Water dig steepness",
        description="Steeper or shallower interior bunker dig",
        min=-100.0,
        max=100.0,
        default=1.0)

    water_dig_depth: FloatProperty(
        name="Water interior dig depth",
        description="Depth to dig interior of bunkers",
        min=-100.0,
        max=100.0,
        default=1.0)

    intVertPaintItems = (
        ('red', 'Red', ''),
        ('green', 'Green', ''),
        ('blue', 'Blue', ''),
        ('black', 'Black', ''),
    )

    slope_min: FloatProperty(
        name="Minimum slope value (degrees)",
        description="Will paint vertices greater than this value the selected color",
        default=40)

    slope_max: FloatProperty(
        name="Maximum slope value (degrees)",
        description="Will paint vertices less than this value the selected color",
        default=80)

    paint_strength: FloatProperty(
        name="Paint Strength",
        description="Will paint based on strength/opacity value (0 to 1)",
        min=0,
        max=1,
        precision=2,
        default=1)

    vertex_paint_type: EnumProperty(
        items=intVertPaintItems,
        name="Vertex Paint Selected Vertices",
        description=" ",
        default='red'
    )

    vertex_paint_type_from: EnumProperty(
        items=intVertPaintItems,
        name="Vertex Paint Selected Vertices From",
        description=" ",
        default='red'
    )

    vertex_paint_type_to: EnumProperty(
        items=intVertPaintItems,
        name="Vertex Paint Selected Vertices To",
        description=" ",
        default='black'
    )

    paint_loop_inset: IntProperty(
        name="Paint Loop Inset",
        default=2,
        min=0,
        max=100,
        description=" ")

    skip_longest_loop: BoolProperty(
        name="Skip Largest Loop",
        description="Skip Largest Loop Paint",
        default=False
    )

    grow_repeat: IntProperty(
        name="Grow Repetition",
        default=1,
        min=1,
        description=" ")

    grow_strict: BoolProperty(
        name="Grow only pure color",
        description="Grow the paint only on the vertices that have no color mixing",
        default=False
    )

    grow_mode: bpy.props.EnumProperty(
        name="Grow Mode",
        description="Choose the grow mode",
        items=[
            ('NORMAL', "Normal", "Normal"),
            ('LINEAR', "Linear", "Linear"),
            ('EDGEONLY', "Edge Only", "Edge Only"),
        ],
        default='NORMAL'
    )

    tees_flat_inset: IntProperty(
        name="Flatten Tees Inset",
        default=2,
        min=0,
        max=10,
        description=" - rec 2")

    tees_flat_outset: FloatProperty(
        name="Outside Smooth Distance",
        default=0.0,
        min=0.0,
        description=" ")

    smooth_path_distance: FloatProperty(
        name="Outside Smooth Distance",
        default=3.0,
        min=0.0,
        description=" ")

    smooth_path_amt: IntProperty(
        name="Smooth Repeat",
        default=20,
        min=1,
        description=" ")

    cart_cut_steps: bpy.props.IntProperty(
        name="Cart Path Cut Step Size",
        description="Number of vertices to step along the boundary loop for each cart path cut",
        default=5,
        min=1
    )

    cart_angle_tolerance: bpy.props.FloatProperty(
        name="Angle Tolerance",
        description="Maximum angle deviation from perpendicular (degrees)",
        default=25.0,
        min=0.0,
        max=45.0,
        step=100,
        precision=1
    )

    cart_max_distance: bpy.props.FloatProperty(
        name="Max Cut Distance",
        description="Maximum allowed cut distance",
        default=7.0,
        min=0.1,
        max=50.0,
        step=10,
        precision=2
    )

    cart_preferred_distance: bpy.props.FloatProperty(
        name="Preferred Cut Distance",
        description="Preferred cut distance (shorter is better)",
        default=5.0,
        min=0.1,
        max=50.0,
        step=10,
        precision=2
    )

    cart_min_distance: bpy.props.FloatProperty(
        name="Min Cut Distance",
        description="Minimum candidate distance",
        default=1.0,
        min=0.01,
        max=10.0,
        step=10,
        precision=2
    )

    smooth_interior: FloatProperty(
        name="Smoothness",
        default=1.0,
        min=-10.0,
        max=10.0,
        description=" - rec 1.0")

    smooth_interior_repeat: IntProperty(
        name="Repeat",
        default=5,
        min=1,
        max=1000,
        description=" - rec 5")

    smooth_interior_inset: IntProperty(
        name="Inset",
        default=1,
        min=1,
        description=" ")

    smooth_interior_3d: BoolProperty(
        name="Smooth X, Y, and Z coords",
        description="Smooth X, Y, and Z coordinates",
        default=False
    )

    subdivide_inset: IntProperty(
        name="Inset",
        default=1,
        min=1,
        description=" ")

    z_shift: FloatProperty(
        name="Vertically shift mesh",
        default=0.05,
        description=" ")

    zshiftloop_z_shift: FloatProperty(
        name="Vertically shift loop",
        default=0.05,
        description=" ")

    zshiftloop_inset: IntProperty(
        name="Inset",
        default=1,
        min=0,
        description=" ")

    zshiftloop_onlylongest: BoolProperty(
        name="Z shift only the longest loop",
        default=True)

    loopslide_random: FloatProperty(
        name="Randomness of Loop Slide",
        default=0.5,
        min=0.0,
        max=2.0,
        description=" ")

    random_amt_topo: FloatProperty(
        name="Randomnly select vertices",
        default=1.0,
        min=0.0,
        max=1.0,
        description=" ")

    dig_z_threshold: FloatProperty(
        name="Min height to angle dig",
        default=0.0,
        min=0.0,
        max=1.0,
        description=" ")

    dig_flatness: FloatProperty(
        name="Flatness for angle dig",
        default=0.0,
        min=0.0,
        max=1.0,
        description=" ")

    loopslide_amount: FloatProperty(
        name="Size of Loop Slide",
        default=0.0,
        min=-2.0,
        max=2.0,
        description=" ")

    loopslide_inset: IntProperty(
        name="Loop Slide Inset",
        default=1,
        min=1,
        max=1000,
        description=" ")

    loopcut_inset: IntProperty(
        name="Loop Cut Inset",
        default=0,
        min=0,
        max=1000,
        description=" ")

    inset_distance: FloatProperty(
        name="Distance of Inset Loop",
        default=0.12,
        min=0,
        description=" ")

    saving_operations: BoolProperty(
        name="Save Operations to File",
        description="Save Operations to File",
        default=False
    )

    mesh_blend_flags: BoolVectorProperty(
        name="Mesh Blend Flags",
        description="Toggle blend for each mesh type",
        size=15,  # Number of material types
        default=[True, True, True, True, False, False, True,
                 True, True, True, True, True, True, True, False]
    )

    mesh_internal_blend_flags: BoolVectorProperty(
        name="Mesh Internal Blend Flags",
        description="Toggle blend for each mesh type on internal loops",
        size=15,  # Number of material types
        default=[True, False, False, True, False, False, True,
                 True, True, True, True, True, True, True, False]
    )

    init_selection_type: EnumProperty(
        items=intSelectionItems,
        name="Selection",
        description=" ",
        default='All'
    )


# property group containing all properties for the gui in the panel
class menutoolsSettings(PropertyGroup):
    """
    Fake module like class
    bpy.context.window_manager.edittools
    """

    # general display properties
    display_terrain: BoolProperty(
        name="Terrain Import",
        description="Settings of the Terrain Import tool",
        default=False
    )
    display_meshes: BoolProperty(
        name="Conform Meshes",
        description="Conform OPCD Meshes",
        default=False
    )
    display_bunker: BoolProperty(
        name="Bunker settings",
        description="Settings for modifying Bunkers",
        default=False
    )
    display_tees: BoolProperty(
        name="Tees settings",
        description="Settings for modifying Tees",
        default=False
    )
    display_cart_paths: BoolProperty(
        name="Cart Paths settings",
        description="Settings for modifying Cart Paths",
        default=False
    )
    display_objects: BoolProperty(
        name="Object Placement",
        description="Various Objects to place on Meshes",
        default=False
    )
    display_waterplane: BoolProperty(
        name="Waterplane Generator",
        description="Settings for adding Waterplanes",
        default=False
    )
    display_waterbase: BoolProperty(
        name="Water Base settings",
        description="Settings for modifying Water Bases",
        default=False
    )
    display_cartpath: BoolProperty(
        name="Cart Path Edits",
        description="Settings for adding Curbs",
        default=False
    )
    display_bulkhead: BoolProperty(
        name="Bulkhead Generator",
        description="Settings for adding Bulkheads",
        default=False
    )
    display_bridge: BoolProperty(
        name="Hazard Stakes Generator",
        description="Settings for adding Hazard Stakes",
        default=False
    )
    display_stakes: BoolProperty(
        name="Hazard Stakes Generator",
        description="Settings for adding Hazard Stakes",
        default=False
    )
    display_stakesandropes: BoolProperty(
        name="Stakes and Ropes Generator",
        description="Settings for adding Stakes and Ropes",
        default=False
    )
    display_stairs: BoolProperty(
        name="Stairs Generator",
        description="Settings for adding Stairs",
        default=False
    )
    display_raisedbed: BoolProperty(
        name="Raised Planter Generator",
        description="Settings for adding Raised Planter",
        default=False
    )
    display_createouter: BoolProperty(
        name="Create Outer Terrain",
        description="Settings for creating an Outer Surrounding Terrain",
        default=False
    )
    display_export: BoolProperty(
        name="Finalize settings",
        description="Display settings of the Finalize tab",
        default=False
    )
    display_blends: BoolProperty(
        name="Blend Management",
        description="Options for altering Mesh Blends",
        default=False
    )

    display_vertexpaint: BoolProperty(
        name="Custom Vertex Paint",
        description="Custom Vertex Paint Menu",
        default=False
    )

    display_topoedit: BoolProperty(
        name="Topology Editor Menu",
        description="Edit Topology of Select Meshes",
        default=False
    )

    display_matselection: BoolProperty(
        name="Selection",
        description="Edit Topology of Select Meshes",
        default=False
    )

    display_advanced: BoolProperty(
        name="Advanced Mesh Tools",
        description="Advanced Mesh Tools - could irreversibly alter meshes",
        default=False
    )

    display_recipes: BoolProperty(
        name="Apply Recipes",
        description="Read Operations from Configuration File - could irreversibly alter meshes",
        default=False
    )

    display_separation: BoolProperty(
        name="Mesh Separator Tool",
        description="Mesh Separator - could irreversibly alter meshes",
        default=False
    )

    display_matchange: BoolProperty(
        name="Mesh Name and Material Change",
        description="Name and Material Change to Dropdown",
        default=False
    )

    display_normalsdatatransfer: BoolProperty(
        name="Data Transfer Normals to Active Object",
        description="Allows Transfer of Face and Corner Normals to the Active Object (for Bunker Normals)",
        default=False
    )

    display_debug: BoolProperty(
        name="Debug Menu",
        description="Debug Menu for SVG Cut Failure",
        default=False
    )


class VIEW3D_PT_header(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Community - Afrod Mod'
    bl_label = 'Community - Afrod Mod - v4 - '+version

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        menu = context.window_manager.menutools

        col = layout.column(align=True)
        box = col.box()

        # # Terrain ##### - first line
        # if menu.display_terrain:
        #     box.prop(menu, "display_terrain",
        #              text="Terrain Manager", icon='CLIPUV_DEHLT')
        # else:
        #     box.prop(menu, "display_terrain",
        #              text="Terrain Manager", icon='CLIPUV_HLT')

        # # Terrain - settings

        # if menu.display_terrain:
        #     box = col.box()

        #     # box.prop(opcdtools, "terrain_selection_type")
        #     myrow = box.row(align=False)
        #     myrow.prop(opcdtools, "terrainsmooth")
        #     myrow.operator("wm.convertterrain")

        #     box = col.box()

        #     # Create Outer ##### - first line
        #     if menu.display_createouter:
        #         box.prop(menu, "display_createouter",
        #                  text="Add Outerplot Mesh", icon='DOWNARROW_HLT')
        #         box = col.box()
        #         myrow = box.row(align=False)
        #         myrow.prop(opcdtools, "outer_terrain_size", text="Outer Size")
        #         myrow.prop(opcdtools, "inner_terrain_size",
        #                    text="Terrain Size")
        #         box.operator("wm.importouter")
        #         box.operator("wm.createouter", text="Create Outerplot Mesh")

        #     else:
        #         box.prop(menu, "display_createouter",
        #                  text="Add Outerplot Mesh", icon='RIGHTARROW')

        #     # box = col.box()

        #     col.separator()
        #     col.separator()

        # box = col.box()

        # Meshes ##### - first line
        if menu.display_meshes:
            box.prop(menu, "display_meshes",
                     text="Conform Meshes", icon='CLIPUV_DEHLT')
        else:
            box.prop(menu, "display_meshes",
                     text="Conform Meshes", icon='CLIPUV_HLT')

        # Meshes - settings
        if menu.display_meshes:
            box = col.box()

            box.prop(opcdtools, "conform_selection_type")
            box.operator("wm.projectmesh", text="Conform to Terrain")
            box.operator("wm.projectmesh_edit",
                         text="Conform to Terrain (Edit Mode)")

            col.separator()
            col.separator()

        box = col.box()

        # Bunkers ##### - first line
        if menu.display_bunker:
            box.prop(menu, "display_bunker",
                     text="Bunkers", icon='CLIPUV_DEHLT')
        else:
            box.prop(menu, "display_bunker", text="Bunkers", icon='CLIPUV_HLT')

        # settings
        if menu.display_bunker:
            box = col.box()
            box.label(text="**Dig Out Bunker**")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "bunker_selection_type",
                       text="Selection", expand=True)
            myrow.prop(opcdtools, "bunker_from_conformed",
                       text="Conform Before Dig", expand=True)

#            box.label(text="Grass Blend:")
#            myrow = box.row(align=False)
#            myrow.prop(opcdtools, "bunker_grass_depth")

#            box.label(text="Inner Bunker:")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "bunkerlip_type")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "bunker_lip_depth")
            myrow.prop(opcdtools, "bunker_inner_depth")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "bunker_dig_shape")
            myrow.prop(opcdtools, "bunker_dig_depth")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "bunker_dig_inset")

            box.operator("wm.bunkeredit", text="Apply Bunker Depth Settings")

            myrow = box.row(align=True)

            box.label(text="**Dig Lip Only**")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "bunker_lip_depth")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "bunker_dig_inset")
            box.operator("wm.bunkereditlip", text="Dig Lip Depth")

            myrow = box.row(align=True)

            box.label(text="**Angle Dig**")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "dig_z_threshold")
            myrow.prop(opcdtools, "dig_flatness")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "bunker_dig_inset")
            box.operator("wm.bunkereditangle", text="Angle Dig")

            myrow = box.row(align=True)

            box.label(text="**Other Bunker Operations**")
            myrow = box.row(align=False)
            myrow.operator("wm.widenbunkerinterior", text="Widen Bunker Loop")
            myrow.prop(opcdtools, "bunker_xy_shift", text="Shift")

            myrow = box.row(align=False)
            myrow.operator("wm.addpotwall", text="Add Pot Wall")
            myrow.prop(opcdtools, "pot_inset", text="Angle of Wall (deg)")
            box.operator("wm.flattenbunker", text="Flatten Base")

            box.label(text="Vertex Paint Options:")

            myrow = box.row(align=False)
            myrow.prop(opcdtools, "bunker_paint_type", expand=True)
            box.operator("wm.paintbunker", text="Apply Bunker Vertex Paint")

            col.separator()
            col.separator()

        box = col.box()

        # Tees ##### - first line
        if menu.display_tees:
            box.prop(menu, "display_tees", text="Tees", icon='CLIPUV_DEHLT')
        else:
            box.prop(menu, "display_tees", text="Tees", icon='CLIPUV_HLT')

        # settings
        if menu.display_tees:
            box = col.box()
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "tees_selection_type",
                       text="Selection", expand=True)

            box.label(text="Edit Tees Options:")

            myrow = box.row(align=False)
            myrow.prop(opcdtools, "tees_flat_inset", expand=True)
            myrow.prop(opcdtools, "tees_flat_outset", expand=True)
            box.operator("wm.flattentees", text="Flatten Tees")

            myrow = box.row(align=False)
            myrow.prop(opcdtools, "z_shift", expand=True)
            box.operator("wm.zshiftmesh", text="Z Shift Selected")

            col.separator()
            col.separator()

        box = col.box()

        # Cart Paths ##### - first line
        if menu.display_cart_paths:
            box.prop(menu, "display_cart_paths",
                     text="Cart Paths", icon='CLIPUV_DEHLT')
        else:
            box.prop(menu, "display_cart_paths",
                     text="Cart Paths", icon='CLIPUV_HLT')

        # settings
        if menu.display_cart_paths:
            box = col.box()

            box.label(text="Smooth Cart Paths:")

            myrow = box.row(align=False)
            myrow.prop(opcdtools, "smooth_path_distance", expand=True)
            myrow.prop(opcdtools, "smooth_path_amt", expand=True)
            box.operator("wm.smoothcartpaths", text="Smooth Cart Paths")

            box = layout.box()
            box.label(text="Cart Path Cutting")

            col = box.column(align=True)
            col.prop(opcdtools, "cart_cut_steps")

            col.separator()
            col.label(text="Cut Constraints:")
            col.prop(opcdtools, "cart_max_distance")
            col.prop(opcdtools, "cart_preferred_distance")
            col.prop(opcdtools, "cart_min_distance")
            col.prop(opcdtools, "cart_angle_tolerance")

            col.separator()
            col.operator("wm.cutcartpaths", text="Cut Cart Paths")

            col.separator()
            col.separator()

        box = col.box()

        # Water Base ##### - first line
        if menu.display_waterbase:
            box.prop(menu, "display_waterbase",
                     text="Water Bases", icon='CLIPUV_DEHLT')
        else:
            box.prop(menu, "display_waterbase",
                     text="Water Bases", icon='CLIPUV_HLT')

        # settings
        if menu.display_waterbase:
            box = col.box()

            myrow = box.row(align=False)
            myrow.prop(opcdtools, "water_selection_type",
                       text="Selection", expand=True)
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "waterlip_type")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "water_lip_depth")
            myrow.prop(opcdtools, "water_inner_depth")
            myrow = box.row(align=False)
            myrow.prop(opcdtools, "water_dig_shape")
            myrow.prop(opcdtools, "water_dig_depth")

            box.operator("wm.wateredit", text="Apply Water Depth Settings")
            box.operator("wm.flattenwaterbase", text="Flatten Base")

            box.label(text="Vertex Paint Options:")

            myrow = box.row(align=False)
            myrow.prop(opcdtools, "water_paint_type", expand=True)
            box.operator("wm.paintwater", text="Apply Water Vertex Paint")

            col.separator()
            col.separator()

        box = col.box()

        # # Waterplanes ##### - first line
        # if menu.display_waterplane:
        #     box.prop(menu, "display_waterplane",
        #              text="Waterplanes", icon='CLIPUV_DEHLT')
        # else:
        #     box.prop(menu, "display_waterplane",
        #              text="Waterplanes", icon='CLIPUV_HLT')

        # # settings
        # if menu.display_waterplane:
        #     box = col.box()
        #     box.prop(opcdtools, "waterplane_selection_type")
        #     box.prop(opcdtools, "wateroutset")
        #     box.operator("wm.addwaterplane", text="Add Water Plane")

        #     col.separator()
        #     col.separator()

        # box = col.box()

        # # Object Placement ##### - first line
        # if menu.display_objects:
        #     box.prop(menu, "display_objects",
        #              text="Object Placement", icon='CLIPUV_DEHLT')
        # else:
        #     box.prop(menu, "display_objects",
        #              text="Object Placement", icon='CLIPUV_HLT')

        # # Object Placement - settings
        # if menu.display_objects:
        #     box = col.box()

        #     # BULKHEADS
        #     if menu.display_bulkhead:

        #         box.prop(menu, "display_bulkhead",
        #                  text="Bulkheads", icon='DOWNARROW_HLT')
        #         box.prop(context.scene.opcdtools,
        #                  "bulkhead_type", text="Bulkhead Type")
        #         box.operator("wm.addbulkheads_inner")
        #         box.operator("wm.flipdirection")

        #         col.separator()
        #         col.separator()

        #     else:
        #         box.prop(menu, "display_bulkhead",
        #                  text="Bulkheads", icon='RIGHTARROW')

        #     box = col.box()

        #     # BRIDGES
        #     if menu.display_bridge:
        #         box.prop(menu, "display_bridge",
        #                  text="Bridges", icon='DOWNARROW_HLT')
        #         box.prop(context.scene.opcdtools,
        #                  "bridge_type", text="Bridge Type")
        #         box.operator(
        #             "wm.addbridge", text='Add Bridge   (in Edit Mode)', icon='COLORSET_03_VEC')
        #         box.operator("wm.removesupports")
        #         box.operator("wm.bridgenarrow")
        #         box.operator("wm.bridgewiden")

        #         col.separator()
        #         col.separator()

        #     else:
        #         box.prop(menu, "display_bridge",
        #                  text="Bridges", icon='RIGHTARROW')

        #     box = col.box()

        #     # CURBS
        #     if menu.display_cartpath:
        #         box.prop(menu, "display_cartpath",
        #                  text="Curbs", icon='DOWNARROW_HLT')
        #         box.prop(opcdtools, "curbs_selection_type")
        #         # box.label(text="  CART PATHS")
        #         box.operator("wm.addcurbs", text='Add Curbs')

        #         col.separator()
        #         col.separator()

        #     else:
        #         box.prop(menu, "display_cartpath",
        #                  text="Curbs", icon='RIGHTARROW')

        #     box = col.box()

        #     # HAZARD STAKES
        #     if menu.display_stakes:
        #         box.prop(menu, "display_stakes",
        #                  text="Hazard Stakes", icon='DOWNARROW_HLT')
        #         box.prop(context.scene.opcdtools,
        #                  "stake_color", text="Stake Color")
        #         myrow = box.row(align=True)
        #         myrow.prop(context.scene.opcdtools, "stake_spacing")
        #         myrow.prop(context.scene.opcdtools, "stake_offset_value")
        #         box.operator("wm.addhazardstake")

        #         col.separator()
        #         col.separator()

        #     else:
        #         box.prop(menu, "display_stakes",
        #                  text="Hazard Stakes", icon='RIGHTARROW')

        #     box = col.box()

        #     # STAKES and ROPES
        #     if menu.display_stakesandropes:
        #         box.prop(menu, "display_stakesandropes",
        #                  text="Stakes and Ropes", icon='DOWNARROW_HLT')
        #         box.operator("wm.addstakesandropes")
        #         box.operator("wm.applymod")

        #         col.separator()
        #         col.separator()

        #     else:
        #         box.prop(menu, "display_stakesandropes",
        #                  text="Stakes and Ropes", icon='RIGHTARROW')

        #     box = col.box()

        #     # STAIRS
        #     if menu.display_stairs:
        #         box.prop(menu, "display_stairs",
        #                  text="Stairs", icon='DOWNARROW_HLT')
        #         box.prop(context.scene.opcdtools,
        #                  "stairs_type", text="Stairs Type")
        #         box.operator(
        #             "wm.addstairs", text='Add Stairs   (in Edit Mode)', icon='COLORSET_03_VEC')
        #         box.operator("wm.rotatestairs")
        #         box.operator("wm.stairsnarrow")
        #         box.operator("wm.stairswiden")

        #         col.separator()
        #         col.separator()

        #     else:
        #         box.prop(menu, "display_stairs",
        #                  text="Stairs", icon='RIGHTARROW')

        #     box = col.box()

        #     # RAISED BED
        #     if menu.display_raisedbed:
        #         box.prop(menu, "display_raisedbed",
        #                  text="Raised Bed", icon='DOWNARROW_HLT')
        #         box = col.box()
        #         box.label(text="  RAISED BED")

        #         myrow = box.row(align=True)
        #         myrow.prop(opcdtools, "wallwidth", text='Wall Width')
        #         myrow.prop(opcdtools, "wallheight")
        #         box.operator("wm.addbed", icon='COLORSET_03_VEC')

        #         # col.separator()
        #         # col.separator()

        #     else:
        #         box.prop(menu, "display_raisedbed",
        #                  text="Raised Bed", icon='RIGHTARROW')

        #     # box = col.box()

        #     col.separator()
        #     col.separator()

        # box = col.box()

        # Advanced Mesh Tools ##### - first line
        if menu.display_advanced:
            box.prop(menu, "display_advanced",
                     text="Advanced Mesh Tools", icon='CLIPUV_DEHLT')
        else:
            box.prop(menu, "display_advanced",
                     text="Advanced Mesh Tools", icon='CLIPUV_HLT')

        if menu.display_advanced:
            box = col.box()

            myrow = box.row(align=True)
            myrow.label(text="*** Recommend Storing Original Mesh")
            myrow.label(text="- Could Irreversibly Alter Meshes ***")
            myrow = box.row(align=False)
            myrow.operator("wm.storemesh", text="Backup Selected Meshes")
            myrow.operator("wm.restoremesh", text="Restore Selected Meshes")

            col.separator()
            col.separator()

            box = col.box()

            # Custom Vertex Paint ##### - first line
            if menu.display_vertexpaint:
                box.prop(menu, "display_vertexpaint",
                         text="Custom Vertex Paint", icon='DOWNARROW_HLT')

            else:
                box.prop(menu, "display_vertexpaint",
                         text="Custom Vertex Paint", icon='RIGHTARROW')

            # Custom Vertex Paint - settings
            if menu.display_vertexpaint:

                myrow = box.row(align=False)
                myrow.prop(opcdtools, "vertex_paint_type", expand=True)

                myrow = box.row(align=False)
                myrow.prop(opcdtools, "paint_loop_inset", expand=True)
                myrow.prop(opcdtools, "random_amt", expand=True)
                myrow = box.row(align=False)
                myrow.prop(opcdtools, "paint_strength", expand=True)

                myrow = box.row(align=False)
                myrow.operator("wm.fillvertexpaint",
                               text="Flood Fill Interior (Object Mode)")

                myrow = box.row(align=False)
                operator_column = myrow.column(align=True)
                operator_column.column(align=True)
                operator_column.operator(
                    "wm.randomvertexpaintloop", text="Paint Loop Vertices (Object Mode)")
                operator_column.scale_x = 2.0  # Give more space to the operator
                property_column = myrow.column(align=True)
                property_column.prop(
                    opcdtools, "skip_longest_loop", text="Skip Longest Loop")

                myrow = box.row(align=False)
                myrow.operator("wm.randomvertexpaint_editmode",
                               text="Paint Selected Vertices (Edit Mode)")

                box.label(text="**Grow Paint**")
                myrow = box.row(align=False)
                operator_column = myrow.column(align=True)
                operator_column.prop(
                    opcdtools, "grow_repeat", text="Repeat", expand=True)
                prop_column = myrow.column(align=True)
                prop_column.column(align=True)
                prop_subrow = prop_column.row(align=False)
                prop_subrow.prop(opcdtools, "grow_mode", expand=True)
                prop_column.scale_x = 0.5  # Give less space to the property.
                operator_column = myrow.column(align=True)
                operator_column.prop(
                    opcdtools, "grow_strict", text="Grow Only Pure Color")
                myrow = box.row(align=False)
                myrow.operator("wm.growcolor", text="Grow Paint")

                box.label(text="Change Vertex Colors:")
                myrow = box.row(align=False)
                myrow.prop(opcdtools, "vertex_paint_type_from", expand=True)
                myrow.prop(opcdtools, "vertex_paint_type_to", expand=True)
                box.operator("wm.changecolors", text="Change From/To")
                box.operator("wm.swapcolors", text="Swap Colors")

                box.label(text="Slope Based Vertex Paint:")
                myrow = box.row(align=False)

                myrow.prop(opcdtools, "slope_min", expand=True)
                myrow.prop(opcdtools, "slope_max", expand=True)
                box.operator("wm.slopevertexpaint",
                             text="Slope Paint Vertices")

                box.label(
                    text="Separate blends for different destination mesh types:")
                box.operator("wm.separateblend", text="Adapt Blend Edges")

                col.separator()
                col.separator()

            box = col.box()

            # Topology Editor Menu ##### - first line
            if menu.display_topoedit:
                box.prop(menu, "display_topoedit",
                         text="Topology Editor", icon='DOWNARROW_HLT')

            else:
                box.prop(menu, "display_topoedit",
                         text="Topology Editor", icon='RIGHTARROW')

            # Topology Editor - settings
            if menu.display_topoedit:

                box = col.box()
                box.label(text="Smooth Mesh Interior")
                myrow = box.row(align=False)
                myrow.prop(opcdtools, "smooth_interior", expand=True)
                myrow.prop(opcdtools, "smooth_interior_repeat", expand=True)
                myrow = box.row(align=False)
                myrow.prop(opcdtools, "smooth_interior_inset", expand=True)
                myrow.prop(opcdtools, "smooth_interior_3d", expand=True)
                box.operator("wm.smoothmesh", text="Smooth Selected")

                box = col.box()
                box.label(text="Slide Loop Vertices")
                myrow = box.row(align=False)
                myrow.prop(opcdtools, "loopslide_inset", expand=True)
                myrow = box.row(align=False)
                myrow.operator("wm.randomslideloop",
                               text="Randomly Slide Loop")
                myrow.prop(opcdtools, "loopslide_random", expand=True)
                myrow = box.row(align=False)
                myrow.operator("wm.spaceloops", text="Shift Entire Loop")
                myrow.prop(opcdtools, "loopslide_amount", expand=True)
                myrow = box.row(align=False)
                myrow.operator("wm.straightenloops", text="Straighten Loop")

                box = col.box()
                box.label(text="Loop Cut All Selected")
                myrow = box.row(align=False)
                myrow.operator("wm.loopcut", text="Loop Cut")
                myrow.prop(opcdtools, "loopcut_inset", expand=True)

                box = col.box()
                box.label(text="Subdivide Mesh Interior")
                myrow = box.row(align=False)
                myrow.prop(opcdtools, "subdivide_inset", expand=True)
                box.operator("wm.subdividemesh", text="Subdivide Selected")

                box = col.box()
                box.label(text="Level Mesh")
                box.operator("wm.levelmesh", text="Level Selected Mesh")
                box.label(text="Ripple Effect")

                myrow = box.row(align=False)
                myrow.prop(opcdtools, "ripple_height")
                myrow.prop(opcdtools, "ripple_inset")
                myrow.prop(opcdtools, "ripple_smooth")
                box.operator(
                    "wm.topoedit", text="Apply Ripple Effect to Selected")

                col.separator()
                col.separator()

            box = col.box()

            # Blend Management ##### - first line
            if menu.display_blends:

                box.prop(menu, "display_blends",
                         text="Blend Management", icon='DOWNARROW_HLT')

                box.label(text="For a Mesh Section only:")
                box.operator("wm.meshblendjoin", text="Join Mesh and Blend")
                box.operator("wm.meshblendseparate",
                             text="Separate Mesh and Blend")
                box.operator("wm.meshblendjoinpermanent",
                             text="Permanently Join Mesh and Blend")

                box.label(text="For a Blend Section only:")
                box.operator("wm.removeblend",
                             text="Remove Blend from Selected")
                box.operator("wm.addblend", text="Add Blend to Selected")

                box = col.box()
                box.label(text="For a non Blend Mesh Island Section only:")
                box.operator("wm.addblendinset",
                             text="Add Blend Section to Selected")

                col.separator()
                col.separator()

            else:
                box.prop(menu, "display_blends",
                         text="Blend Management", icon='RIGHTARROW')

            box = col.box()

            # Mesh Separator ##### - first line
            if menu.display_separation:
                box.prop(menu, "display_separation",
                         text="Mesh Separator - Edit Mode", icon='DOWNARROW_HLT')

                box.operator("wm.invertselection", text="Invert Selection")
                myrow = box.row(align=False)

                myrow.prop(opcdtools, "separate_mat_selection", expand=False)
                myrow.operator("wm.separatemesh", text="Separate Mesh")

                col.separator()
                col.separator()

            else:
                box.prop(menu, "display_separation",
                         text="Mesh Separator - Edit Mode", icon='RIGHTARROW')

            box = col.box()

            # Mesh Name and Material change ##### - first line
            if menu.display_matchange:
                box.prop(menu, "display_matchange",
                         text="Mesh Name and Material Editor", icon='DOWNARROW_HLT')

                myrow = box.row(align=False)
                myrow.prop(opcdtools, "mat_change", expand=False)
                myrow.operator(
                    "wm.matchange", text="Change Mesh Name and Material")

                col.separator()
                col.separator()

            else:
                box.prop(menu, "display_matchange",
                         text="Mesh Name and Material Editor", icon='RIGHTARROW')

            box = col.box()

            # Data Transfer of Normals ##### - first line
            if menu.display_normalsdatatransfer:

                box.prop(menu, "display_normalsdatatransfer",
                         text="Normals Data Transfer", icon='DOWNARROW_HLT')

                box.label(
                    text="AUTO - Must Select 2 Objects - Will Generate Vertex Group")
                # box.label(text=" - Modifier Transfers Data from second to first")

                myrow = box.row(align=False)
                myrow.operator(
                    "wm.normalsdatatransfer", text="Auto Transfer Boundary Normals from Active Object")

                box.label(text="")
                box.label(
                    text="MANUAL - Select Vertex and Assign Vertex Group First in Edit Mode")

                box.prop(opcdtools, "vtx_group_name")
                myrow = box.row(align=False)
                myrow.operator("wm.vertexgroupassign",
                               text="Assign Vertex Group to Selected Vertices")
                myrow.operator("wm.normaltransfervertexgroup",
                               text="Transfer Normals to Selected Vertex Group")

                col.separator()
                col.separator()

            else:
                box.prop(menu, "display_normalsdatatransfer",
                         text="Normals Data Transfer", icon='RIGHTARROW')

        # Recipe Handling ##### - first line
        if menu.display_recipes:
            box.prop(menu, "display_recipes",
                     text="Apply Recipes", icon='CLIPUV_DEHLT')
        else:
            box.prop(menu, "display_recipes",
                     text="Apply Recipes", icon='CLIPUV_HLT')

        # settings
        if menu.display_recipes:
            box = col.box()
            # box.prop(opcdtools, "active_recipe_file")
            box.operator("wm.readoperations", text="Apply Recipe")

            col.separator()
            col.separator()

        col.separator()
        col.separator()
        col.separator()
        col.separator()
        col.separator()
        box = col.box()

        ##### Quick Selection Menu #####
        myrow = box.row(align=False)
        myrow.label(text="Bulk Selection Menu:")
        myrow.prop(opcdtools, "mat_selection")
        box.operator("wm.matselection")

        # myrow = box.row(align=False)
        # myrow.label(text="Blend Selection Menu:")
        # myrow = box.row(align=False)
        # myrow.prop(opcdtools, "blend_selection")
        box.operator("wm.blendselection")

        col.separator()
        col.separator()
        col.separator()
        col.separator()
        col.separator()
        # box = col.box()

        # # EXPORT ##### - first line
        # if menu.display_export:
        #     box.prop(menu, "display_export",
        #              text="EXPORT", icon='CLIPUV_DEHLT')
        # else:
        #     box.prop(menu, "display_export", text="EXPORT", icon='CLIPUV_HLT')

        # # settings
        # if menu.display_export:
        #     box = col.box()
        #     box.prop(opcdtools, "export_folder", text="Export Folder")
        #     box.operator("object.batch_fbx_export")
        #     box.operator("object.selected_fbx_export")

        #     col.separator()
        #     col.separator()

        # col.separator()
        # col.separator()
        # col.separator()
        # col.separator()
        # col.separator()
        # col.separator()
        # col.separator()
        # col.separator()
        # col.separator()
        # box = col.box()

        # box.operator("wm.saveandclearcache")

#         ##### DEBUG ##### - first line
#        if menu.display_debug:
#            box.prop(menu, "display_debug", text="DEBUG", icon='DOWNARROW_HLT')
#        else:
#            box.prop(menu, "display_debug", text="DEBUG", icon='RIGHTARROW')
#
#        # settings
#        if menu.display_debug:
#            box = col.box()
#            box.label(text="**  Window > Toggle System Console to view Errors  **")
#            box.operator("wm.areadebug")
#            box.operator("wm.meshcut")
#
#            col.separator()
#            col.separator()


##############################################
##### General functions and Definitions ######
##############################################

def mode_set_bm_on(mesh):
    current_mode = bpy.context.object.mode

    if current_mode == 'EDIT':
        bm = bmesh.from_edit_mesh(mesh)

    else:
        bm = bmesh.new()
        bm.from_mesh(mesh)

    return bm


def mode_set_bm_off(mesh):
    current_mode = bpy.context.object.mode

    if current_mode == 'EDIT':
        bmesh.update_edit_mesh(mesh)

    else:
        bm.to_mesh(mesh)
        me.update()

    return bm


def ensure_color_attribute(obj):
    """Ensure we have the 'Col' color attribute which should already exist from import"""
    if not obj or obj.type != 'MESH':
        return None

    mesh = obj.data

    if not hasattr(mesh, 'color_attributes'):
        print(f"WARNING: {obj.name} doesn't support color_attributes")
        return None

    # Col should already exist from import_course.py
    # Just get it and verify it's valid
    col_attr = mesh.color_attributes.get("Col")

    if col_attr:
        if col_attr.data_type in ['FLOAT_COLOR', 'BYTE_COLOR'] and col_attr.domain == 'CORNER':
            # Col exists and is valid - perfect!
            mesh.color_attributes.active_color = col_attr

            # Quick data validation
            if not hasattr(col_attr, 'data') or len(col_attr.data) == 0:
                print(f"WARNING: 'Col' exists but has no data for {obj.name}")
                mesh.update()  # Try to force allocation

            return col_attr

        else:
            # Col exists but wrong type/domain - need to fix it
            print(
                f"WARNING: 'Col' has wrong type ({col_attr.data_type}) or domain ({col_attr.domain})")

            # Preserve the existing colors if possible
            existing_colors = None
            if col_attr.domain == 'POINT' and hasattr(col_attr, 'data') and len(col_attr.data) > 0:
                # Save POINT domain colors (per-vertex)
                existing_colors = []
                for i in range(len(col_attr.data)):
                    existing_colors.append(col_attr.data[i].color[:])
                print(
                    f"  Preserving {len(existing_colors)} vertex colors from POINT domain")

            # Remove the incorrect Col
            try:
                mesh.color_attributes.remove(col_attr)
                print(f"  Removed incorrect 'Col' attribute")
            except:
                pass

            # Create new Col with correct domain
            try:
                col_attr = mesh.color_attributes.new(
                    name="Col",
                    domain='CORNER',
                    type='BYTE_COLOR'
                )
                mesh.update()
                print(f"  Created new 'Col' with CORNER domain")

                # If we had POINT colors, apply them to CORNER domain
                if existing_colors and len(existing_colors) == len(mesh.vertices):
                    print(f"  Copying colors from POINT to CORNER domain...")
                    for poly in mesh.polygons:
                        for loop_idx in poly.loop_indices:
                            loop = mesh.loops[loop_idx]
                            if loop.vertex_index < len(existing_colors):
                                col_attr.data[loop_idx].color = existing_colors[loop.vertex_index] + (1.0,) if len(
                                    existing_colors[loop.vertex_index]) == 3 else existing_colors[loop.vertex_index]
                    print(f"  Transferred colors successfully")

                mesh.color_attributes.active_color = col_attr
                return col_attr

            except Exception as e:
                print(f"  ERROR: Could not create correct 'Col': {e}")
                return None
    else:
        # Col doesn't exist - this is unexpected if mesh was imported properly
        print(f"WARNING: No 'Col' attribute found for {obj.name}")

        # List what attributes DO exist for debugging
        if len(mesh.color_attributes) > 0:
            print(
                f"  Available attributes: {[a.name for a in mesh.color_attributes]}")

        # Since Col should exist from import, create it as a fallback
        try:
            col_attr = mesh.color_attributes.new(
                name="Col",
                domain='CORNER',
                type='BYTE_COLOR'
            )
            mesh.update()
            print(f"  Created missing 'Col' attribute")
            mesh.color_attributes.active_color = col_attr
            return col_attr
        except Exception as e:
            print(f"  ERROR: Could not create 'Col': {e}")
            return None


def color_to_vertices(paint_color, paint_strength):
    """Modern version using color_attributes instead of legacy vertex_colors"""

    if paint_color == 'red':
        color = (1, 0, 0, 1)
    elif paint_color == 'green':
        color = (0, 1, 0, 1)
    elif paint_color == 'blue':
        color = (0, 0, 1, 1)
    elif paint_color == 'black':
        color = (0, 0, 0, 1)
    elif paint_color == 'white':
        color = (1, 1, 1, 1)
    else:
        color = (1, 0, 0, 1)  # Default to red

    # Check if there's an active object
    obj = bpy.context.active_object
    if not obj or obj.type != 'MESH':
        print("No active mesh object selected for vertex painting.")
        return

    mesh = obj.data
    current_mode = bpy.context.object.mode

    # CRITICAL: Switch to OBJECT mode FIRST before accessing any mesh data
    # In EDIT mode, color attributes and vertex selection aren't properly accessible
    if current_mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
        # Re-get mesh data after mode switch
        mesh = obj.data

    # NOW get or create color attribute (in OBJECT mode)
    color_attr = ensure_color_attribute(obj)
    if not color_attr:
        print("Failed to create color attribute")
        # Switch back to original mode before returning
        if current_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        return

    # Read selected vertices (now that we're in OBJECT mode)
    sel_vindexs = set(
        v.index for v in mesh.vertices if v.select
    )

    if not sel_vindexs:
        print("No vertices selected - make sure to select vertices first!")
        # Switch back to original mode before returning
        if current_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        return

    # Modern way: directly access color attribute data
    # For CORNER domain, we need to paint all corners (loops) that touch selected vertices
    # print(f"Painting {len(sel_vindexs)} vertices with {paint_color} color (strength: {paint_strength})")
    # print(f"Using color attribute: '{color_attr.name}' ({color_attr.domain}, {color_attr.data_type})")

    painted_count = 0
    error_count = 0

    for poly in mesh.polygons:
        for loop_idx in poly.loop_indices:
            loop = mesh.loops[loop_idx]
            if loop.vertex_index in sel_vindexs:
                # Get existing color
                try:
                    existing_color = color_attr.data[loop_idx].color
                    # Blend with new color based on paint strength
                    new_color = [
                        existing_color[i] * (1 - paint_strength) +
                        color[i] * paint_strength
                        for i in range(3)
                    ] + [1]
                    # Set the new color
                    color_attr.data[loop_idx].color = new_color
                    painted_count += 1
                except Exception as e:
                    error_count += 1
                    if error_count <= 5:  # Only print first 5 errors
                        print(
                            f"ERROR accessing color data at loop {loop_idx}: {e}")
                    continue

    if error_count > 5:
        print(f"... and {error_count - 5} more errors")

    # print(f"Successfully painted {painted_count} loop corners")

    # Ensure the color attribute is set as the active one for rendering
    if mesh.color_attributes.active_color != color_attr:
        mesh.color_attributes.active_color = color_attr

    # Update the mesh
    mesh.update()

    # Switch back to original mode
    if current_mode == 'EDIT':
        bpy.ops.object.mode_set(mode='EDIT')

# Lower Z coordinates of selected vertices


def verts_lower_z_angle(obj, dig_z_threshold, dig_flatness):
    me = obj.data
    current_mode = bpy.context.object.mode

    if current_mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)

    else:
        bm = bmesh.new()
        bm.from_mesh(me)

    verts = [v for v in bm.verts if v.select]

    min_z = verts[0].co.z
    max_z = verts[0].co.z
    for v in verts[1:]:
        z = v.co.z
        if z < min_z:
            min_z = z
        if z > max_z:
            max_z = z

    for v in verts:
        rangeZ = max_z - min_z
        percentile = (v.co.z - min_z)/rangeZ
        threshold_z = (dig_z_threshold * rangeZ) + min_z
        if percentile > dig_z_threshold:
            lower_value = dig_flatness * (v.co.z - threshold_z)
            v.co.z -= lower_value

    if current_mode == 'EDIT':
        bmesh.update_edit_mesh(me)

    else:
        bm.to_mesh(me)
        me.update()

# Lower Z coordinates of selected vertices


def verts_lower_z(obj, lower_value):
    me = obj.data
    current_mode = bpy.context.object.mode

    if current_mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)

    else:
        bm = bmesh.new()
        bm.from_mesh(me)

    verts = [v for v in bm.verts if v.select]
    for v in verts:
        v.co.z -= lower_value

    if current_mode == 'EDIT':
        bmesh.update_edit_mesh(me)

    else:
        bm.to_mesh(me)
        me.update()

# Lower projected mesh to below Terrain - typical prior to reproject


def flatten_mesh(obj):  # OBJECT mode

    me = obj.data

    current_mode = bpy.context.object.mode

    if current_mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)

    else:
        bm = bmesh.new()
        bm.from_mesh(me)

    for v in bm.verts:
        v.co.z = -100

    if current_mode == 'EDIT':
        bmesh.update_edit_mesh(me)

    else:
        bm.to_mesh(me)
        me.update()


def fix_failed_projections(obj, target_name, base_z=-100):
    """
    Fix vertices that failed to project (still at base_z).
    Fixed version that handles vertex group creation properly.

    Args:
        obj: Mesh object
        target_name: Name of target object for projection
        base_z: The Z value that indicates unprojected vertices
    """
    # print(f"Checking for failed projections on {obj.name}")

    # Find vertices still at base_z
    failed_verts = []
    for i, vert in enumerate(obj.data.vertices):
        world_z = (obj.matrix_world @ vert.co).z
        if abs(world_z - base_z) < 0.01:  # Within tolerance of -100
            failed_verts.append(i)

    if not failed_verts:
        # print(f"  All vertices projected successfully!")
        return

    print(f"  Found {len(failed_verts)} failed vertices, fixing...")

    # Move only failed vertices randomly
    for idx in failed_verts:
        vert = obj.data.vertices[idx]
        vert.co.x += random.uniform(-0.01, 0.01)
        vert.co.y += random.uniform(-0.01, 0.01)

    # CRITICAL: Update the mesh data after modifying vertices
    obj.data.update()

    # Create vertex group for failed vertices
    vg_name = "TempFix"

    # Remove old vertex group if it exists
    if vg_name in obj.vertex_groups:
        obj.vertex_groups.remove(obj.vertex_groups[vg_name])

    # Create new vertex group
    vg = obj.vertex_groups.new(name=vg_name)

    # Add failed vertices to the group
    vg.add(failed_verts, 1.0, 'ADD')

    # CRITICAL FIX: Ensure the object is active and in object mode
    # This ensures the vertex group is properly registered
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # Make sure we're in object mode
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    # Update the mesh to ensure vertex group is registered
    obj.data.update()
    bpy.context.view_layer.update()

    # Re-project only those vertices using a new modifier
    project = obj.modifiers.new("ReProject", 'SHRINKWRAP')
    project.wrap_method = 'PROJECT'
    project.vertex_group = vg_name  # Limit to vertex group
    project.project_limit = 0
    project.subsurf_levels = 0
    project.cull_face = 'OFF'
    project.offset = 0.00
    project.use_project_z = True
    project.use_negative_direction = False
    project.use_positive_direction = True
    project.target = bpy.data.objects[target_name]

    # Apply the modifier
    try:
        bpy.ops.object.modifier_apply(modifier=project.name)
        print(f"  Successfully re-projected {len(failed_verts)} vertices")
    except RuntimeError as e:
        print(f"  Error applying modifier: {e}")
        # Fallback: Apply without vertex group restriction
        # print(f"  Attempting fallback projection without vertex group...")

        # # Remove the failed modifier
        # obj.modifiers.remove(project)

        # # Apply a simple projection to all vertices
        # project2 = obj.modifiers.new("ReProjectAll", 'SHRINKWRAP')
        # project2.wrap_method = 'PROJECT'
        # project2.project_limit = 0
        # project2.use_project_z = True
        # project2.use_positive_direction = True
        # project2.target = bpy.data.objects[target_name]
        # bpy.ops.object.modifier_apply(modifier=project2.name)

    # Clean up vertex group
    if vg_name in obj.vertex_groups:
        obj.vertex_groups.remove(obj.vertex_groups[vg_name])

# Project Meshes


def project_mesh(obj, text):
    project = obj.modifiers.new("Project", 'SHRINKWRAP')
    project.wrap_method = 'PROJECT'
    project.project_limit = 0
    project.subsurf_levels = 0
    project.cull_face = 'OFF'
    project.offset = 0.00
    project.use_project_z = True
    project.use_negative_direction = False
    project.use_positive_direction = True
    project.target = bpy.data.objects[text]
    bpy.ops.object.shade_smooth()


# Flatten Mesh
def flatten_base(obj):  # OBJECT mode

    me = obj.data
    wm = obj.matrix_world      # Active object's world matrix

    bpy.ops.object.mode_set(mode='EDIT')

    bpy.ops.mesh.select_all(action='SELECT')

    bm = bmesh.from_edit_mesh(me)

    # get the minimum z-value of all vertices after converting to global transform
    lowest = min([(obj.matrix_world @ v.co).z for v in obj.data.vertices])

    # lower selection - exclude the lip
    # bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.region_to_loop()
    bpy.ops.mesh.select_all(action='INVERT')
    bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='FACE')
    bpy.ops.mesh.select_less(use_face_step=False)

    for v in bm.verts:
        if v.select:
            v.co.z = lowest

    bmesh.update_edit_mesh(me, loop_triangles=True)

    bpy.ops.object.mode_set(mode='OBJECT')

# used for Pot Bunker Wall Creation


def inset_lower(counter, inset):

    while counter > 0:

        if int(counter % 2) == 0:
            color_to_vertices('green', 1.0)

        else:
            color_to_vertices('red', 1.0)

        slope = np.radians(inset)

        bpy.ops.mesh.offset_edges(geometry_mode='extrude', width=-0.05, angle=slope, follow_face=False,
                                  mirror_modifier=False, edge_rail=False, threshold=0.000872665, caches_valid=False)

        counter = counter-1

# Collections management


def traverse_tree(t):
    yield t
    for child in t.children:
        yield from traverse_tree(child)


def parent_lookup(coll):
    parent_lookup = {}
    for coll in traverse_tree(coll):
        for c in coll.children.keys():
            parent_lookup.setdefault(c, coll)
    return parent_lookup


def move_collections_obj(target, obj):
    # Set target collection to a known collection
    coll_target = bpy.context.scene.collection.children.get(target)

    if coll_target and obj:
        # Loop through all collections the obj is linked to
        for coll in obj.users_collection:
            coll.objects.unlink(obj)

        # Link each object to the target collection
        coll_target.objects.link(obj)


def vert_distance(a, b):
    return np.sqrt((a[0] - b[0])*(a[0] - b[0]) + (a[1] - b[1])*(a[1] - b[1]) + (a[2] - b[2])*(a[2] - b[2]))

# Function to remove numerical extensions from the name


def remove_numerical_extension(name):
    if name[-3:].isnumeric():
        return name[:-4]
    else:
        return name


def append_json(filepath, new_data):
    """
    Appends data to an existing JSON file or creates a new one.
    """
    try:
        # Try to read existing data
        if os.path.exists(filepath):
            with open(filepath, 'r') as json_file:
                data = json.load(json_file)
        else:
            data = {}  # Initialize as empty if file doesn't exist

        # Append or update the data
        data.update(new_data)

        # Write the updated data back to the file
        with open(filepath, 'w') as json_file:
            json.dump(data, json_file, indent=4)

        return f"Appended to JSON file: {filepath}"
    except Exception as e:
        return f"Error appending to JSON file: {e}"


def delete_json(filepath):
    """
    Deletes the specified JSON file.
    """
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            return f"Deleted file: {filepath}"
        else:
            return f"File does not exist: {filepath}"
    except Exception as e:
        return f"Error deleting file: {e}"


def calculate_loop_area(loop_verts):
    """
    Calculate the area of a loop using the shoelace formula.
    Works for any closed polygon in 3D by projecting to 2D.
    """
    if len(loop_verts) < 3:
        return 0.0

    normal = Vector((0, 0, 1))  # z up projection is good

    # Create a 2D projection basis
    if abs(normal.z) < 0.999:
        right = Vector((0, 0, 1)).cross(normal).normalized()
    else:
        right = Vector((1, 0, 0))

    up = normal.cross(right).normalized()

    # Project vertices to 2D
    points_2d = []
    for vert in loop_verts:
        p = vert.co
        x = p.dot(right)
        y = p.dot(up)
        points_2d.append((x, y))

    # Calculate area using shoelace formula
    # Coolest math I have seen in a while
    area = 0.0
    n = len(points_2d)
    for i in range(n):
        j = (i + 1) % n
        area += points_2d[i][0] * points_2d[j][1]
        area -= points_2d[j][0] * points_2d[i][1]

    return abs(area) / 2.0


def get_boundary_loops(bm):
    """
    Find all boundary loops in the mesh.
    Returns a list of lists, where each inner list contains ordered BMVerts.
    """
    boundary_edges = [e for e in bm.edges if e.is_boundary]
    if not boundary_edges:
        return []

    # Build adjacency for boundary edges only
    boundary_verts = set()
    adjacency = {}

    for edge in boundary_edges:
        v1, v2 = edge.verts
        boundary_verts.add(v1)
        boundary_verts.add(v2)

        if v1 not in adjacency:
            adjacency[v1] = []
        if v2 not in adjacency:
            adjacency[v2] = []

        adjacency[v1].append(v2)
        adjacency[v2].append(v1)

    loops = []
    visited = set()

    for start_vert in boundary_verts:
        if start_vert in visited:
            continue

        # Walk the loop
        loop = []
        current = start_vert
        prev = None

        while True:
            loop.append(current)
            visited.add(current)

            # Find next vertex
            next_vert = None
            for neighbor in adjacency.get(current, []):
                if neighbor != prev:
                    next_vert = neighbor
                    break

            if not next_vert or next_vert == start_vert:
                break

            prev = current
            current = next_vert

        if len(loop) > 2:  # Valid loop
            loops.append(loop)

    return loops


def get_selected_loops(bm):
    """
    Get vertices from pre-selected edge loops in edit mode.
    Returns a list of lists, where each inner list contains ordered BMVerts
    for each separate selected loop.
    """
    # Get selected edges
    selected_edges = [e for e in bm.edges if e.select]
    if not selected_edges:
        return []

    # Build adjacency for selected edges only
    selected_verts = set()
    adjacency = {}

    for edge in selected_edges:
        v1, v2 = edge.verts
        selected_verts.add(v1)
        selected_verts.add(v2)

        if v1 not in adjacency:
            adjacency[v1] = []
        if v2 not in adjacency:
            adjacency[v2] = []

        adjacency[v1].append(v2)
        adjacency[v2].append(v1)

    loops = []
    visited = set()

    for start_vert in selected_verts:
        if start_vert in visited:
            continue

        # Walk the loop
        loop = []
        current = start_vert
        prev = None

        while True:
            loop.append(current)
            visited.add(current)

            # Find next vertex
            next_vert = None
            for neighbor in adjacency.get(current, []):
                if neighbor != prev and neighbor not in visited:
                    next_vert = neighbor
                    break

            if not next_vert:
                # Check if we've completed a loop back to start
                if start_vert in adjacency.get(current, []) and len(loop) > 2:
                    # We've found a closed loop
                    pass
                break

            prev = current
            current = next_vert

        if len(loop) > 1:  # Add any valid connected selection
            loops.append(loop)

    return loops


def select_external_boundary_simple(invert=False):
    """
    Simplified version that uses only area calculation.

    Args:
        invert: If True, select all loops except the largest area one
    """
    obj = bpy.context.edit_object
    me = obj.data
    bm = bmesh.from_edit_mesh(me)

    loops = get_selected_loops(bm)

    if not loops:
        return

    # Find loop with largest area
    best_loop = None
    best_area = -1

    for loop in loops:
        area = calculate_loop_area(loop)
        print(area)
        print(len(loop))
        if area > best_area:
            best_area = area
            best_loop = loop

    # Determine which loops to select
    if invert:
        print("INVERTING")
        loops_to_select = [loop for loop in loops if loop != best_loop]
    else:
        print("NOT INVERTING")
        loops_to_select = [best_loop]

    # Deselect all
    for v in bm.verts:
        v.select = False
    for e in bm.edges:
        e.select = False

    # Select the appropriate loops
    for loop in loops_to_select:
        for v in loop:
            v.select = True

    for e in bm.edges:
        if e.verts[0].select and e.verts[1].select:
            e.select = True

    bm.select_flush_mode()
    bmesh.update_edit_mesh(me)
    bpy.ops.mesh.select_mode(type='EDGE')


# Convenience functions for clarity
def select_external_boundary():
    """Select only the external boundary."""
    select_external_boundary_simple(invert=False)


def select_internal_boundaries():
    """Select all internal boundaries (holes)."""
    select_external_boundary_simple(invert=True)


def create_course_plane(size_square, name="Course_Plane"):
    """
    Creates a square flat mesh with corners at specified coordinates.

    Args:
        size_square: The size of the square (width and height)
        name: Name for the mesh object (default: "SquareMesh")

    Returns:
        The created mesh object
    """

    objects_to_check = bpy.context.scene.objects

    # delete all Terrains in scene - cleanup if re-importing Terrain
    for ob in objects_to_check:
        if ob.name == name:
            ob.select_set(True)
            bpy.data.objects.remove(ob)

    # Define vertices (x, y, z)
    vertices = [
        (0, 0, -10),
        (-size_square, 0, -10),
        (-size_square, -size_square, -10),
        (0, -size_square, -10)
    ]

    # Define edges (connecting vertex indices)
    edges = [
        (0, 1),  # Bottom edge
        (1, 2),  # Right edge
        (2, 3),  # Top edge
        (3, 0)   # Left edge
    ]

    # Define face (single quad using all 4 vertices)
    faces = [(0, 1, 2, 3)]

    # Create mesh data
    mesh = bpy.data.meshes.new(name=name)

    # Create mesh from vertices, edges, and faces
    mesh.from_pydata(vertices, edges, faces)

    # Update mesh with new data
    mesh.update()

    # Create object from mesh
    obj = bpy.data.objects.new(name, mesh)

    # Link object to scene collection
    bpy.context.collection.objects.link(obj)

########################################
##### End of General functions #########
########################################


class WM_OT_importTerrain(Operator, ImportHelper):
    """Import Terrain.obj and remove any other Terrain"""
    bl_label = "Import Terrain OBJ"
    bl_idname = "wm.convertterrain"

    filter_glob: StringProperty(
        default='*.obj',
        options={'HIDDEN'}
    )

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        terrainsmooth = opcdtools.terrainsmooth
        terrain_type = opcdtools.terrain_selection_type

        bpy.context.scene.cursor.location = (0, 0, 0)

        selected = bpy.context.scene.objects

        # delete all Terrains in scene - cleanup if re-importing Terrain
        for ob in selected:
            if ob.name == 'Terrain':
                ob.select_set(True)
                bpy.data.objects.remove(ob)

        fp = bpy.path.abspath(self.filepath)

        # bpy.ops.import_scene.obj(filepath = fp)
        bpy.ops.wm.obj_import(filepath=fp)
#        bpy.ops.wm.obj_import(clamp_size=0.0,forward_axis='NEGATIVE_Z',up_axis='Y',filepath = fp)

        terrain = bpy.context.selected_objects[0]
        bpy.context.view_layer.objects.active = terrain
        bpy.ops.object.move_to_collection(collection_index=0)

        terrain.name = "Terrain"
        terrain.data.name = "Terrain"

        bpy.ops.object.modifier_add(type='SMOOTH')
        terrain.modifiers["Smooth"].use_y = True
        terrain.modifiers["Smooth"].use_x = False
        terrain.modifiers["Smooth"].use_z = False
        terrain.modifiers["Smooth"].factor = terrainsmooth

        bpy.ops.object.convert(target='MESH')

        terrain.hide_set(True)

        print('Selected file:', self.filepath)

        return {'FINISHED'}


class WM_OT_importOuter(Operator, ImportHelper):
    """Import Outer.obj and remove any other Outer Terrain"""
    bl_label = "Import Outer OBJ"
    bl_idname = "wm.importouter"

    filter_glob: StringProperty(
        default='*.obj',
        options={'HIDDEN'}
    )

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        terrainsmooth = opcdtools.terrainsmooth
        terrain_type = opcdtools.terrain_selection_type
        terrain_size = opcdtools.inner_terrain_size

        bpy.context.scene.cursor.location = (0, 0, 0)

        selected = bpy.context.scene.objects

        # delete all Terrains in scene - cleanup if re-importing Terrain
        for ob in selected:
            if ob.name == 'Outer':
                ob.select_set(True)
                bpy.data.objects.remove(ob)

        fp = bpy.path.abspath(self.filepath)
        # bpy.ops.import_scene.obj(filepath = fp)
        bpy.ops.wm.obj_import(filepath=fp)

        terrain = bpy.context.selected_objects[0]
        bpy.context.view_layer.objects.active = terrain
        bpy.ops.object.move_to_collection(collection_index=0)

        terrain.name = "Outer"
        terrain.data.name = "Outer"

        bpy.ops.object.modifier_add(type='SMOOTH')
        terrain.modifiers["Smooth"].use_y = True
        terrain.modifiers["Smooth"].use_x = False
        terrain.modifiers["Smooth"].use_z = False
        terrain.modifiers["Smooth"].factor = terrainsmooth

        bpy.ops.object.convert(target='MESH')

        terrain.hide_set(True)

        print('Selected file:', self.filepath)

        return {'FINISHED'}


class WM_OT_projectMesh(bpy.types.Operator):
    """Conform Meshes to Terrain"""
    bl_label = "Conform All Meshes"
    bl_idname = "wm.projectmesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.conform_selection_type

        # timer
        start = time.time()
        now = datetime.now()  # current date and time
        starttime = now.strftime("%H:%M:%S")

        if "All" in selection_type:
            print("Conforming All Meshes to Terrain")
            selected = [
                o for o in bpy.context.visible_objects if 'Spline' or 'Outerplot' in o.name and not 'WaterPlane' in o.name]
            # bunker = [o for o in bpy.context.visible_objects if ('Spline' in o.name and ('Bunker' in o.name in o.name) and o.type == 'MESH')]

        if "Selected" in selection_type:
            print("Conforming Selected Meshes to Terrain")
            selected = [
                o for o in bpy.context.selected_objects if 'Spline' or 'Outerplot' in o.name and not 'WaterPlane' in o.name]
            # bunker = [o for o in bpy.context.selected_objects if ('Spline' in o.name and ('Bunker' in o.name) and o.type == 'MESH')]

        counter = len(selected)
        marker = 1
        for o in selected:
            bpy.ops.object.select_all(action='DESELECT')
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            print("Conforming Mesh - ", o.name, " - ", marker, " of ", counter)

            if o.modifiers:
                o.modifiers.clear()

            if 'Outerplot' in o.name:
                flatten_mesh(o)
                project_mesh(o, "Outer")

            if 'Spline' in o.name:
                flatten_mesh(o)
                project_mesh(o, "Terrain")

            # apply modifiers
            bpy.ops.object.convert(target='MESH')

            if 'Outerplot' in o.name:
                fix_failed_projections(o, "Terrain")

            if 'Spline' in o.name:
                fix_failed_projections(o, "Terrain")

            # manage renaming Pot Bunkers - essentially "reset" of the Bunker on Conform - Potwall should be manually removed
            if 'Pot' in o.name:
                if 'Blend' not in o.name and not 'Potwall' in o.name:

                    blends = opcd_find_blends_for_mesh(o)
                    if blends:
                        for b in blends:
                            b.select_set(True)
                    else:
                        blend = o.name[:-4] + 'Blend'
                        if blend in bpy.data.objects:
                            bpy.data.objects[blend].select_set(True)

                    meshes = bpy.context.selected_objects

                    for m in meshes:
                        bpy.ops.object.select_all(action='DESELECT')
                        m.select_set(True)
                        bpy.context.view_layer.objects.active = m

                        if 'Pot' in m.name:
                            if 'Blend' in m.name:
                                if 'Blend_-_mod' in m.name:
                                    m.name = m.name[:-17] + 'Blend'
                                else:
                                    m.name = m.name[:-11] + 'Blend'

                            if 'Mesh' in m.name:
                                m.name = m.name[:-10] + 'Mesh'

            marker += 1

        # Reset selection back to Selected so no accidental "All" choices
        opcdtools.conform_selection_type = 'Selected'

        print("\nLength of time to Project -",
              round((time.time() - start)/60, 5), "minutes")
        now = datetime.now()  # current date and time
        endtime = now.strftime("%H:%M:%S")
        print(" Start Projection Time:", starttime)
        print(" End Projection Time:", endtime)

        return {'FINISHED'}


class WM_OT_projectMeshEdit(bpy.types.Operator):
    """Conform Meshes to Terrain"""
    bl_label = "Conform All Meshes"
    bl_idname = "wm.projectmesh_edit"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.conform_selection_type

        # timer
        start = time.time()
        now = datetime.now()  # current date and time
        starttime = now.strftime("%H:%M:%S")

        o = bpy.context.object

        if o.modifiers:
            o.modifiers.clear()

        if 'Outerplot' in o.name:
            project_selected_vertices(o, "Outer")

        if 'Spline' in o.name:
            project_selected_vertices(o, "Terrain")

        bpy.ops.object.convert(target='MESH')

        print("\nLength of time to Project -",
              round((time.time() - start)/60, 5), "minutes")
        now = datetime.now()  # current date and time
        endtime = now.strftime("%H:%M:%S")
        print(" Start Projection Time:", starttime)
        print(" End Projection Time:", endtime)

        return {'FINISHED'}


def project_selected_vertices(obj, target_obj_name):
    # Set the object to be active and enter Edit Mode
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')

    # Gather selected vertices indices
    selected_verts = [v.index for v in obj.data.vertices if v.select]

    # Switch to Object Mode to add vertices to the vertex group
    bpy.ops.object.mode_set(mode='OBJECT')

    if selected_verts:
        # Create a new vertex group and add selected vertices
        vertex_group = obj.vertex_groups.new(name="ShrinkwrapGroup")
        vertex_group.add(selected_verts, 1.0, 'ADD')

        # Create and configure the Shrinkwrap modifier
        project = obj.modifiers.new("Project", 'SHRINKWRAP')
        project.wrap_method = 'PROJECT'
        project.project_limit = 0
        project.subsurf_levels = 0
        project.cull_face = 'OFF'
        project.offset = 0.00
        project.use_project_z = True
        project.use_negative_direction = True
        project.use_positive_direction = True
        project.target = bpy.data.objects[target_obj_name]

        # Assign the vertex group to the Shrinkwrap modifier
        project.vertex_group = vertex_group.name

        # Optionally smooth shade the object
        bpy.ops.object.shade_smooth()
    else:
        print("No vertices selected for projection.")


class WM_OT_bunkerEdit(bpy.types.Operator):
    """Edit Bunker lip and depth"""
    bl_label = "Adjust Bunker"
    bl_idname = "wm.bunkeredit"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.bunker_selection_type
        from_conformed = opcdtools.bunker_from_conformed
        lip_depth = opcdtools.bunker_lip_depth
        inner_depth = opcdtools.bunker_inner_depth
        bunkerlip_type = opcdtools.bunkerlip_type
        bunker_dig_depth = opcdtools.bunker_dig_depth
        bunker_dig_shape = opcdtools.bunker_dig_shape
        bunker_dig_inset = opcdtools.bunker_dig_inset

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if ('Spline' in o.name) and (
                'Bunker' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        counter = 1
        total = len(selected)

        # Dig the bunkers
        for ob in selected:

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Digging -", ob.name, counter, " of ", total)

            # Check specifically for the "Project" modifier and ensure it's enabled
            for mod in ob.modifiers:
                if mod.name == "Project":
                    # Ensure the modifier is enabled before applying
                    mod.show_viewport = True
                    mod.show_render = True
                    try:
                        bpy.ops.object.modifier_apply(modifier="Project")
                    except:
                        print(
                            f"Warning: Could not apply Project modifier to {ob.name}")
                    break

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            if from_conformed:
                flatten_mesh(ob)
                bpy.ops.object.mode_set(mode='OBJECT')
                project_mesh(ob, "Terrain")
                bpy.ops.object.convert(target='MESH')

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            # take away the lip, then count how many face loops are there
            bpy.ops.mesh.select_less()
            bpy.ops.mesh.select_less()
            num_face_loops = 0
            item_count = bpy.context.active_object.data.count_selected_items()
            while (sum(item_count) > 0):
                bpy.ops.mesh.select_less()
                item_count = bpy.context.active_object.data.count_selected_items()
                num_face_loops = num_face_loops + 1

            # scale up due to wider spacing between edges
            bunker_dig_depth_scaled = bunker_dig_depth * 4.0
            scaleX = 10/num_face_loops  # normalize to the bunker size
            scaleX = scaleX / bunker_dig_shape  # apply shaping divider

            print(num_face_loops)

            # reselect mesh minus the boundary
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')
            if bunkerlip_type == 'concave':
                inner_depth_a = 0.6*inner_depth
                inner_depth_b = 0.4*inner_depth

                verts_lower_z(ob, lip_depth)
                bpy.ops.mesh.select_less()
                verts_lower_z(ob, inner_depth_a)
                for i in range(bunker_dig_inset+1):
                    bpy.ops.mesh.select_less()
                    verts_lower_z(ob, inner_depth_b)
                for i in range(num_face_loops-bunker_dig_inset):
                    bpy.ops.mesh.select_less()
                    # x and y scaled inverse function with nominal y intercept at inner_depth_b
                    inner_depth_c = (
                        inner_depth_b * bunker_dig_depth_scaled) / ((scaleX * i) + 1)
                    verts_lower_z(ob, inner_depth_c)

            else:
                inner_depth_a = 0.4*inner_depth
                inner_depth_b = 0.6*inner_depth

                verts_lower_z(ob, lip_depth)
                bpy.ops.mesh.select_less()
                verts_lower_z(ob, inner_depth_a)
                for i in range(bunker_dig_inset+1):
                    bpy.ops.mesh.select_less()
                    verts_lower_z(ob, inner_depth_b)
                for i in range(num_face_loops-bunker_dig_inset):
                    bpy.ops.mesh.select_less()
                    # x and y scaled inverse function with nominal y intercept at inner_depth_b
                    inner_depth_c = (
                        inner_depth_b * bunker_dig_depth_scaled) / ((scaleX * i) + 1)
                    verts_lower_z(ob, inner_depth_c)

            bpy.ops.mesh.select_all(action='DESELECT')

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        bpy.context.space_data.clip_end = 10000

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_bunkerEditLip(bpy.types.Operator):
    """Edit Bunker lip and depth"""
    bl_label = "Adjust Bunker Lip"
    bl_idname = "wm.bunkereditlip"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.bunker_selection_type
        lip_depth = opcdtools.bunker_lip_depth
        bunker_dig_inset = opcdtools.bunker_dig_inset
        random_amt_topo = opcdtools.random_amt_topo

        if (bunker_dig_inset < 1):
            print("For bunker dig lip, inset must be greater than zero")
            self.report(
                {'ERROR'}, "For bunker dig lip, inset must be greater than zero")
            return {'CANCELLED'}

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if ('Spline' in o.name) and (
                'Bunker' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        counter = 1
        total = len(selected)

        # Grass Blend Lowering
        for ob in selected:

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Lowering -", ob.name, counter, " of ", total)

            # Check specifically for the "Project" modifier and ensure it's enabled
            for mod in ob.modifiers:
                if mod.name == "Project":
                    # Ensure the modifier is enabled before applying
                    mod.show_viewport = True
                    mod.show_render = True
                    try:
                        bpy.ops.object.modifier_apply(modifier="Project")
                    except:
                        print(
                            f"Warning: Could not apply Project modifier to {ob.name}")
                    break

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            # reselect mesh minus the boundary
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')

            for i in range(bunker_dig_inset-1):
                bpy.ops.mesh.select_less()

            bpy.ops.mesh.select_random(
                ratio=(1.0-random_amt_topo), seed=random.randint(1, 100), action='DESELECT')

            verts_lower_z(ob, lip_depth)

            bpy.ops.mesh.select_all(action='DESELECT')

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        bpy.context.space_data.clip_end = 10000

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_bunkerEditAngle(bpy.types.Operator):
    """Edit Bunker Angle"""
    bl_label = "Adjust Bunker Angle"
    bl_idname = "wm.bunkereditangle"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.bunker_selection_type
        lip_depth = opcdtools.bunker_lip_depth
        bunker_dig_inset = opcdtools.bunker_dig_inset
        random_amt_topo = opcdtools.random_amt_topo
        dig_z_threshold = opcdtools.dig_z_threshold
        dig_flatness = opcdtools.dig_flatness

        if (bunker_dig_inset < 1):
            print("For bunker dig lip, inset must be greater than zero")
            self.report(
                {'ERROR'}, "For bunker dig lip, inset must be greater than zero")
            return {'CANCELLED'}

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if ('Spline' in o.name) and (
                'Bunker' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        counter = 1
        total = len(selected)

        # Grass Blend Lowering
        for ob in selected:

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Lowering -", ob.name, counter, " of ", total)

            # Check specifically for the "Project" modifier and ensure it's enabled
            for mod in ob.modifiers:
                if mod.name == "Project":
                    # Ensure the modifier is enabled before applying
                    mod.show_viewport = True
                    mod.show_render = True
                    try:
                        bpy.ops.object.modifier_apply(modifier="Project")
                    except:
                        print(
                            f"Warning: Could not apply Project modifier to {ob.name}")
                    break

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            # reselect mesh minus the boundary
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')

            for i in range(bunker_dig_inset-1):
                bpy.ops.mesh.select_less()

            bpy.ops.mesh.select_random(
                ratio=(1.0-random_amt_topo), seed=random.randint(1, 100), action='DESELECT')

            verts_lower_z_angle(ob, dig_z_threshold, dig_flatness)

            bpy.ops.mesh.select_all(action='DESELECT')

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        bpy.context.space_data.clip_end = 10000

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_widenBunkerInterior(bpy.types.Operator):
    """Widen Bunker Loop"""
    bl_label = "Widen Bunker Loop"
    bl_idname = "wm.widenbunkerinterior"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.bunker_selection_type
        bunker_dig_inset = opcdtools.bunker_dig_inset
        bunker_xy_shift = opcdtools.bunker_xy_shift

        if (bunker_dig_inset < 1):
            print("For widen bunker loop, inset must be greater than zero")
            self.report(
                {'ERROR'}, "For widen bunker loop, inset must be greater than zero")
            return {'CANCELLED'}

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if ('Spline' in o.name) and (
                'Bunker' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        counter = 1
        total = len(selected)

        for ob in selected:

            bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            # Check specifically for the "Project" modifier and ensure it's enabled
            for mod in ob.modifiers:
                if mod.name == "Project":
                    # Ensure the modifier is enabled before applying
                    mod.show_viewport = True
                    mod.show_render = True
                    try:
                        bpy.ops.object.modifier_apply(modifier="Project")
                    except:
                        print(
                            f"Warning: Could not apply Project modifier to {ob.name}")
                    break

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            # reselect mesh minus the boundary
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')

            for i in range(bunker_dig_inset-1):
                bpy.ops.mesh.select_less()
            bpy.ops.mesh.region_to_loop()

            mesh = ob.data
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()
            selected_target_verts_list = [v for v in bm.verts if v.select]
            list_of_loops_bmverts = separate_vertices_into_loops(
                selected_target_verts_list)
            list_of_loops_indices = [[v.index for v in loop_bmverts]
                                     for loop_bmverts in list_of_loops_bmverts]

            # find longest loop
            longest_loop_index = -1
            max_vertex_count = -1
            if list_of_loops_indices:
                for idx, loop_indices in enumerate(list_of_loops_indices):
                    current_len = len(loop_indices)
                    if current_len > max_vertex_count:
                        max_vertex_count = current_len
                        longest_loop_index = idx

            for loop, current_loop_indices in enumerate(list_of_loops_indices):
                print(f"  Loop {loop+1}: {len(current_loop_indices)} indices")

                bpy.ops.mesh.select_all(action='DESELECT')
                bpy.ops.mesh.select_mode(type='EDGE')

                bm = bmesh.from_edit_mesh(mesh)
                bm.verts.ensure_lookup_table()
                bm.edges.ensure_lookup_table()

                current_loop_indices_set = set(current_loop_indices)

                selected_edge_count = 0
                for edge in bm.edges:
                    # Check if BOTH vertices of the edge belong to the current loop's indices
                    v0_in_loop = edge.verts[0].index in current_loop_indices_set
                    v1_in_loop = edge.verts[1].index in current_loop_indices_set

                    if v0_in_loop and v1_in_loop:
                        edge.select_set(True)
                        selected_edge_count += 1
                    else:
                        edge.select_set(False)

                print(f"    Selected {selected_edge_count} edges in bmesh.")

                bmesh.update_edit_mesh(mesh)
                bm.free()

                if loop == longest_loop_index:
                    bpy.ops.mesh.offset_edges(geometry_mode='move', width=bunker_xy_shift,
                                              depth_mode='angle', angle=0, follow_face=False, caches_valid=False)
                else:
                    bpy.ops.mesh.offset_edges(geometry_mode='move', width=-bunker_xy_shift,
                                              depth_mode='angle', angle=0, follow_face=False, caches_valid=False)

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        bpy.context.space_data.clip_end = 10000

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_waterEdit(bpy.types.Operator):
    """Edit Water Base lip and depth"""
    bl_label = "Adjust Water Base"
    bl_idname = "wm.wateredit"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.water_selection_type
        lip_depth = opcdtools.water_lip_depth
        inner_depth = opcdtools.water_inner_depth
        waterlip_type = opcdtools.waterlip_type
        water_dig_depth = opcdtools.water_dig_depth
        water_dig_shape = opcdtools.water_dig_shape

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if (
                'Spline' in o.name) and ('Lake' in o.name) and not ('Blend' in o.name)]

        # don't force the selected to be a bunker
        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and ('Lake' in o.name) and not ('Blend' in o.name)]

        counter = 1
        total = len(selected)

        # Grass Blend Lowering
        for ob in selected:

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Lowering -", ob.name, counter, " of ", total)

            # Check specifically for the "Project" modifier and ensure it's enabled
            for mod in ob.modifiers:
                if mod.name == "Project":
                    # Ensure the modifier is enabled before applying
                    mod.show_viewport = True
                    mod.show_render = True
                    try:
                        bpy.ops.object.modifier_apply(modifier="Project")
                    except:
                        print(
                            f"Warning: Could not apply Project modifier to {ob.name}")
                    break

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            flatten_mesh(ob)

            bpy.ops.object.mode_set(mode='OBJECT')

            project_mesh(ob, "Terrain")

            bpy.ops.object.convert(target='MESH')

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            # take away the lip, then count how many face loops are there
            # bpy.ops.mesh.select_less()
            # bpy.ops.mesh.select_less()
            num_face_loops = 0
            item_count = bpy.context.active_object.data.count_selected_items()
            while (sum(item_count) > 0):
                bpy.ops.mesh.select_less()
                item_count = bpy.context.active_object.data.count_selected_items()
                num_face_loops = num_face_loops + 1

            # scale up due to wider spacing between edges
            water_dig_depth_scaled = water_dig_depth * 4.0
            scaleX = 10.0/num_face_loops  # normalize to the water size
            scaleX = scaleX / water_dig_shape  # apply shaping divider

            # reselect mesh minus the boundary
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')
            if waterlip_type == 'concave':
                inner_depth_a = 0.6*inner_depth
                inner_depth_b = 0.4*inner_depth

                verts_lower_z(ob, lip_depth)
                bpy.ops.mesh.select_less()
                verts_lower_z(ob, inner_depth_a)
                bpy.ops.mesh.select_less()
                verts_lower_z(ob, inner_depth_b)
                for i in range(num_face_loops):
                    bpy.ops.mesh.select_less()
                    # x and y scaled inverse function with nominal y intercept at inner_depth_b
                    inner_depth_c = (
                        inner_depth_b * water_dig_depth_scaled) / ((scaleX * i) + 1)
                    verts_lower_z(ob, inner_depth_c)

            else:
                inner_depth_a = 0.4*inner_depth
                inner_depth_b = 0.6*inner_depth

                verts_lower_z(ob, lip_depth)
                bpy.ops.mesh.select_less()
                verts_lower_z(ob, inner_depth_a)
                bpy.ops.mesh.select_less()
                verts_lower_z(ob, inner_depth_b)
                for i in range(num_face_loops):
                    bpy.ops.mesh.select_less()
                    # x and y scaled inverse function with nominal y intercept at inner_depth_b
                    inner_depth_c = (
                        inner_depth_b * water_dig_depth_scaled) / ((scaleX * i) + 1)
                    verts_lower_z(ob, inner_depth_c)

            bpy.ops.mesh.select_all(action='DESELECT')

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        # Reset selection back to Selected so no accidental "All" choices
        opcdtools.water_selection_type = 'Selected'

        bpy.context.space_data.clip_end = 10000

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_paintbunker(bpy.types.Operator):
    """Edit Bunker Vertex Painting"""
    bl_label = "Adjust Bunker Vertex Painting"
    bl_idname = "wm.paintbunker"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.bunker_selection_type
        bunker_paint_type = opcdtools.bunker_paint_type

        bpy.context.scene.tool_settings.transform_pivot_point = 'INDIVIDUAL_ORIGINS'

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if ('Spline' in o.name) and (
                'Bunker' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if ('Spline' in o.name) and (
                'Bunker' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        counter = 1
        total = len(selected)

        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Painting -", ob.name, "-", counter, " of ", total)

            bpy.ops.object.mode_set(mode='EDIT')

            bpy.ops.mesh.select_all(action='SELECT')

            # Red
            bpy.ops.mesh.select_all(action='SELECT')
            color_to_vertices('red', 1.0)

            # Green
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='FACE')
            color_to_vertices('green', 1.0)

            # Blue
            bpy.ops.mesh.select_less()

            if bunker_paint_type == 'pot':
                bpy.ops.mesh.select_less()

            elif bunker_paint_type == 'wet':
                bpy.ops.mesh.select_less()
                bpy.ops.mesh.select_less()
                bpy.ops.mesh.select_less()

            color_to_vertices('blue', 1.0)

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_paintwater(bpy.types.Operator):
    """Edit Water Vertex Painting"""
    bl_label = "Adjust Water Vertex Painting"
    bl_idname = "wm.paintwater"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.water_selection_type
        water_paint_type = opcdtools.water_paint_type

        bpy.context.scene.tool_settings.transform_pivot_point = 'INDIVIDUAL_ORIGINS'

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if (
                'Spline' in o.name) and ('Lake' in o.name) and not ('Blend' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and ('Lake' in o.name) and not ('Blend' in o.name)]

        counter = 1
        total = len(selected)

        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Painting -", ob.name, "-", counter, " of ", total)

            bpy.ops.object.mode_set(mode='EDIT')

            bpy.ops.mesh.select_all(action='SELECT')

            # Red
            if water_paint_type != 'hazard':
                bpy.ops.mesh.select_all(action='SELECT')
                color_to_vertices('red', 1.0)

                # Green
                bpy.ops.mesh.region_to_loop()
                bpy.ops.mesh.select_all(action='INVERT')
                bpy.ops.mesh.select_mode(
                    use_extend=False, use_expand=False, type='FACE')
                color_to_vertices('green', 1.0)

            # Custom per type
            if water_paint_type == 'clean':
                bpy.ops.mesh.select_less()
                color_to_vertices('blue', 1.0)

            elif water_paint_type == 'wet':
                bpy.ops.mesh.select_less()
                bpy.ops.mesh.select_less()
                # bpy.ops.mesh.select_less()
                color_to_vertices('blue', 1.0)

            elif water_paint_type == 'hazard':
                bpy.ops.mesh.select_all(action='SELECT')
                color_to_vertices('green', 1.0)
                bpy.ops.mesh.region_to_loop()
                bpy.ops.mesh.select_all(action='INVERT')
                bpy.ops.mesh.select_mode(
                    use_extend=False, use_expand=False, type='FACE')

                bpy.ops.mesh.select_less()
                color_to_vertices('red', 1.0)
                bpy.ops.mesh.select_less()
                color_to_vertices('blue', 1.0)
                bpy.ops.mesh.select_less()
                bpy.ops.mesh.select_less()
                color_to_vertices('black', 1.0)

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        # Reset selection back to Selected so no accidental "All" choices
        opcdtools.water_selection_type = 'Selected'

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_flattenbunker(bpy.types.Operator):
    """Edit Bunker lip and depth"""
    bl_label = "Adjust Bunker"
    bl_idname = "wm.flattenbunker"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.bunker_selection_type

        bpy.context.scene.tool_settings.transform_pivot_point = 'INDIVIDUAL_ORIGINS'
        if "All" in selection_type:
            selected = [o for o in context.visible_objects if ('Spline' in o.name) and (
                'Bunker' in o.name) and not ('Potwall' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and ('Bunker' in o.name) and not ('Potwall' in o.name)]

        counter = 1
        total = len(selected)

        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Flattening -", ob.name, "-", counter, " of ", total)

            flatten_base(ob)

            counter += 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_addpotwall(bpy.types.Operator):
    """Add a Pot Wall to Selected Bunker"""
    bl_label = "Add Pot Wall"
    bl_idname = "wm.addpotwall"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.bunker_selection_type
        pot_inset = opcdtools.pot_inset

        if 'Pot_Wall' not in bpy.data.materials:
            bpy.data.materials.new(name="Pot_Wall")

            for mat in bpy.data.materials:
                if "Pot_Wall" in mat.name:
                    mat.diffuse_color = (0.8, 0.8, 0, 1)

        bpy.context.scene.tool_settings.transform_pivot_point = 'INDIVIDUAL_ORIGINS'

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if ('Spline' in o.name) and (
                'Bunker' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if ('Spline' in o.name) and (
                'Bunker' in o.name) and not ('Blend' in o.name) and not ('Potwall' in o.name)]

        for o in selected:

            if '_-_mod' in o.name:
                bpy.context.window_manager.popup_menu(lambda self, context: self.layout.label(
                    text="Cannot add Potwall to a 'mod' mesh"), title="Potwall Warning", icon='INFO')

            else:
                bpy.ops.object.select_all(action='DESELECT')
                o.select_set(True)
                bpy.context.view_layer.objects.active = o

                bpy.ops.object.mode_set(mode='EDIT')

                lowest = min(
                    [(o.matrix_world @ v.co).z for v in o.data.vertices])
                highest = max(
                    [(o.matrix_world @ v.co).z for v in o.data.vertices])
                depth = (highest - lowest)*25
                print("depth -", depth)

                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.region_to_loop()
                bpy.ops.mesh.duplicate()

                bpy.ops.mesh.separate(type='SELECTED')

                bpy.ops.object.mode_set(mode='OBJECT')

                temp_select = bpy.context.selected_objects

                print("Building Pot Wall for -", temp_select[0].name)

                bpy.ops.object.select_all(action='DESELECT')
                temp_select[1].select_set(True)
                bpy.context.view_layer.objects.active = temp_select[1]

                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')

                bpy.ops.mesh.offset_edges(geometry_mode='extrude', width=-0.05, angle=0, follow_face=True,
                                          mirror_modifier=False, edge_rail=False, threshold=0.000872665, caches_valid=False, angle_presets='0°')

                color_to_vertices('green', 1.0)

                inset_lower(depth, pot_inset)

                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.region_to_loop()
                color_to_vertices('red', 1.0)

                bpy.ops.object.mode_set(mode='OBJECT')

                bpy.ops.object.shade_smooth()

                bpy.context.object.active_material_index = 0
                bpy.ops.object.material_slot_remove()
                mat = bpy.data.materials.get("Pot_Wall")

                # assign new Material
                temp_select[1].data.materials.append(mat)

                # rename all connected Parts - Blend, Mesh and Wall to have Pot in name
                blend = temp_select[1].name[:-8]+'Blend'
                mesh = temp_select[1].name[:-8]+'Mesh'

                if not 'Pot' in bpy.data.objects[blend].name:
                    bpy.data.objects[blend].name = bpy.data.objects[blend].name[:-
                                                                                5] + 'Pot - Blend'

                if not 'Pot' in bpy.data.objects[mesh].name:
                    bpy.data.objects[mesh].name = bpy.data.objects[mesh].name[:-4] + 'Pot - Mesh'

                if 'Pot' in temp_select[1].name:
                    temp_select[1].name = temp_select[1].name[:-
                                                              14] + 'Potwall - Mesh'

                else:
                    temp_select[1].name = temp_select[1].name[:-
                                                              8] + 'Potwall - Mesh'

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_flattenwaterbase(bpy.types.Operator):
    """Flatten Water Base"""
    bl_label = "Flatten Water Base"
    bl_idname = "wm.flattenwaterbase"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.water_selection_type

        bpy.context.scene.tool_settings.transform_pivot_point = 'INDIVIDUAL_ORIGINS'

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if (
                'Spline' in o.name) and ('Water_Base_Lake' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and ('Water_Base_Lake' in o.name)]

        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            flatten_base(ob)

        # Reset selection back to Selected so no accidental "All" choices
        opcdtools.water_selection_type = 'Selected'

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_addwaterplane(bpy.types.Operator):
    """Add Water Plane to Selected Meshes"""
    bl_label = "Add Water Plane to Selected"
    bl_idname = "wm.addwaterplane"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.waterplane_selection_type

        wateroutset = opcdtools.wateroutset

        bpy.context.scene.tool_settings.transform_pivot_point = 'INDIVIDUAL_ORIGINS'

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if ('Spline' in o.name) and (
                'Water_Base_Lake' in o.name) and not ('Blend' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and not ('Blend' in o.name)]

        if 'WaterPlane' not in bpy.data.materials:
            bpy.data.materials.new(name="WaterPlane")

            for mat in bpy.data.materials:
                if "WaterPlane" in mat.name:
                    mat.diffuse_color = (0, 0, 0.5, 1)

        # Create Water Plane
        for o in selected:
            bpy.ops.object.select_all(action='DESELECT')
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            bpy.ops.object.duplicate(linked=False)

            o = bpy.context.active_object
            me = o.data
            wm = o.matrix_world     # Active object's world matrix

            bm = bmesh.new()

            bpy.ops.object.mode_set(mode='EDIT')

            bpy.ops.mesh.select_all(action='SELECT')
            color_to_vertices('red', 1.0)
            bpy.ops.mesh.region_to_loop()

            bm = bmesh.from_edit_mesh(me)

            vertices = [v for v in bm.verts if v.select]
            minZ = 999999.8
            for v in vertices:
                world = wm @ v.co
                if (world[2] < minZ):
                    minZ = world[2]
                    lowest = (v.co.z-0.05)

            for v in bm.verts:
                v.co.z = lowest

            for v in bm.verts:
                v.co.z = lowest

            # offset edges 2m
            bpy.ops.mesh.offset_edges(
                geometry_mode='extrude', width=wateroutset, angle=0, follow_face=True, caches_valid=False)

            bmesh.update_edit_mesh(me, loop_triangles=True)
            bpy.ops.object.mode_set(mode='OBJECT')

            # rename waterplane and assign material
            pre = o.data.name[:-4]
            # post = o.data.name[15:]

            o.name = "Spline " + pre + ' WaterPlane'
            o.data.name = o.name

            bpy.context.object.active_material_index = 0
            bpy.ops.object.material_slot_remove()

            mat = bpy.data.materials.get("WaterPlane")

            # assign new Material
            o.data.materials.append(mat)

        # Reset selection back to Selected so no accidental "All" choices
        opcdtools.waterplane_selection_type = 'Selected'

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_flattenTees(bpy.types.Operator):
    """Edit Tees to Flatten"""
    bl_label = "Flatten Tees"
    bl_idname = "wm.flattentees"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.tees_selection_type
        inset = opcdtools.tees_flat_inset
        outset = opcdtools.tees_flat_outset

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if (
                'Spline' in o.name) and ('Tee' in o.name) and not ('Blend' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and not ('Blend' in o.name)]

        counter = 1
        total = len(selected)

        # Tee Flattening
        for ob in selected:

            bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            for obj in bpy.data.objects:
                if "Spline" in obj.name:
                    obj.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Flattening -", ob.name, counter, " of ", total)

            # Check specifically for the "Project" modifier and ensure it's enabled
            for mod in ob.modifiers:
                if mod.name == "Project":
                    # Ensure the modifier is enabled before applying
                    mod.show_viewport = True
                    mod.show_render = True
                    try:
                        bpy.ops.object.modifier_apply(modifier="Project")
                    except:
                        print(
                            f"Warning: Could not apply Project modifier to {ob.name}")
                    break

            # find the boundary loop vertices of mesh
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(type='VERT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bm = bmesh.from_edit_mesh(ob.data)
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')
            for v in bm.verts:
                v.select = True
            for edge in bm.edges:
                edge.select = True
            for face in bm.faces:
                face.select = True
            bmesh.update_edit_mesh(ob.data)
            bpy.ops.mesh.region_to_loop()
            meshBoundaryVerts = [v.co for v in bm.verts if v.select]

            # find boundary loop vertices of selected
            # Invert the selection only for this object's vertices
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='EDGE')
            for edge in bm.edges:
                if (edge.verts[0].select and edge.verts[1].select):
                    edge.select = True
            for edge in bm.edges:
                edge.select = not edge.select
            for face in bm.faces:
                if (face.edges[0].select and face.edges[1].select and face.edges[2].select):
                    face.select = True
            bmesh.update_edit_mesh(ob.data)

            # inset and find boundary loop of inner region
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='FACE')
            if inset > 0:
                bpy.ops.mesh.select_less()
                bpy.ops.mesh.select_more()
                for i in range(inset-1):
                    bpy.ops.mesh.select_less()
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='EDGE')
            bpy.ops.mesh.region_to_loop()
            for v in bm.verts:
                v.select = False
            for edge in bm.edges:
                if edge.select:
                    edge.verts[0].select = True
                    edge.verts[1].select = True
            selectedBoundaryVerts = [v.co for v in bm.verts if v.select]

            # reselect the inner region
            # select all in mesh
            for v in bm.verts:
                v.select = True
            for edge in bm.edges:
                edge.select = True
            for face in bm.faces:
                face.select = True
            bmesh.update_edit_mesh(ob.data)

            bpy.ops.mesh.region_to_loop()

            # Invert the selection only for this object's vertices
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='EDGE')
            for edge in bm.edges:
                if (edge.verts[0].select and edge.verts[1].select):
                    edge.select = True
            for edge in bm.edges:
                edge.select = not edge.select
            for face in bm.faces:
                if (face.edges[0].select and face.edges[1].select and face.edges[2].select):
                    face.select = True
            bmesh.update_edit_mesh(ob.data)

            # inset
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='FACE')
            if inset > 0:
                bpy.ops.mesh.select_less()
                bpy.ops.mesh.select_more()
                for i in range(inset-1):
                    bpy.ops.mesh.select_less()
            for v in bm.verts:
                v.select = False
            for edge in bm.edges:
                if edge.select:
                    edge.verts[0].select = True
                    edge.verts[1].select = True
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            # for each vertex in boundary loop of selected
            # find the nearest vertex of the boundary of the mesh
            # the minimum of all distances minus 0.05 is the smooth_distance
            minDist = float('inf')
            for co in meshBoundaryVerts:
                for co1 in selectedBoundaryVerts:
                    distCheck = vert_distance(co, co1)
                    if distCheck < minDist:
                        minDist = distCheck

            if inset > 0:
                # smooth to edge of tee box without touching the edge if outset is zero
                smooth_distance = (minDist - 0.05)
            else:
                bpy.ops.mesh.select_mode(type='VERT')
                bpy.ops.mesh.select_all(action='DESELECT')
                bpy.ops.mesh.select_mode(
                    use_extend=False, use_expand=False, type='VERT')
                for v in bm.verts:
                    v.select = True
                for edge in bm.edges:
                    edge.select = True
                for face in bm.faces:
                    face.select = True
                bmesh.update_edit_mesh(ob.data)
                smooth_distance = 0
            smooth_distance += outset
            bpy.ops.transform.resize(value=(1.0, 1.0, 0.0),
                                     mouse_dir_constraint=(0.0, 0.0, 0.0),
                                     orient_type='GLOBAL',
                                     orient_matrix=(
                                         (1, 0, 0), (0, 1, 0), (0, 0, 1)),
                                     orient_matrix_type='GLOBAL',
                                     constraint_axis=(False, False, True),
                                     mirror=False,
                                     use_proportional_edit=True,
                                     proportional_edit_falloff='SMOOTH',
                                     proportional_size=smooth_distance,
                                     use_proportional_connected=False,
                                     use_proportional_projected=False
                                     )

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        # Reset selection back to Selected so no accidental "All" choices
        opcdtools.tees_selection_type = 'Selected'

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_ZShiftMesh(bpy.types.Operator):
    """Z Shift Mesh"""
    bl_label = "Z Shift Mesh"
    bl_idname = "wm.zshiftmesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.tees_selection_type
        inset = opcdtools.tees_flat_inset
        outset = opcdtools.tees_flat_outset
        z_shift = opcdtools.z_shift

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if (
                'Spline' in o.name) and ('Tee' in o.name) and not ('Blend' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in bpy.context.selected_objects if (
                'Spline' in o.name) and not ('Blend' in o.name)]

        counter = 1
        total = len(selected)

        # Tee Raising
        for ob in selected:

            bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            for obj in bpy.data.objects:
                if "Spline" in obj.name:
                    obj.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Z shifting -", ob.name, counter, " of ", total)

            # Check specifically for the "Project" modifier and ensure it's enabled
            for mod in ob.modifiers:
                if mod.name == "Project":
                    # Ensure the modifier is enabled before applying
                    mod.show_viewport = True
                    mod.show_render = True
                    try:
                        bpy.ops.object.modifier_apply(modifier="Project")
                    except:
                        print(
                            f"Warning: Could not apply Project modifier to {ob.name}")
                    break

            # find the boundary loop vertices of mesh
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(type='VERT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bm = bmesh.from_edit_mesh(ob.data)
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')
            for v in bm.verts:
                v.select = True
            for edge in bm.edges:
                edge.select = True
            for face in bm.faces:
                face.select = True
            bmesh.update_edit_mesh(ob.data)
            bpy.ops.mesh.region_to_loop()
            meshBoundaryVerts = [v.co for v in bm.verts if v.select]

            # find boundary loop vertices of selected
            # Invert the selection only for this object's vertices
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='EDGE')
            for edge in bm.edges:
                if (edge.verts[0].select and edge.verts[1].select):
                    edge.select = True
            for edge in bm.edges:
                edge.select = not edge.select
            for face in bm.faces:
                if (face.edges[0].select and face.edges[1].select and face.edges[2].select):
                    face.select = True
            bmesh.update_edit_mesh(ob.data)

            # inset and find boundary loop of inner region
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='FACE')
            if inset > 0:
                bpy.ops.mesh.select_less()
                bpy.ops.mesh.select_more()
                for i in range(inset-1):
                    bpy.ops.mesh.select_less()
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='EDGE')
            bpy.ops.mesh.region_to_loop()
            for v in bm.verts:
                v.select = False
            for edge in bm.edges:
                if edge.select:
                    edge.verts[0].select = True
                    edge.verts[1].select = True
            selectedBoundaryVerts = [v.co for v in bm.verts if v.select]

            # reselect the inner region
            # select all in mesh
            for v in bm.verts:
                v.select = True
            for edge in bm.edges:
                edge.select = True
            for face in bm.faces:
                face.select = True
            bmesh.update_edit_mesh(ob.data)

            bpy.ops.mesh.region_to_loop()

            # Invert the selection only for this object's vertices
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='EDGE')
            for edge in bm.edges:
                if (edge.verts[0].select and edge.verts[1].select):
                    edge.select = True
            for edge in bm.edges:
                edge.select = not edge.select
            for face in bm.faces:
                if (face.edges[0].select and face.edges[1].select and face.edges[2].select):
                    face.select = True
            bmesh.update_edit_mesh(ob.data)

            # inset
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='FACE')
            if inset > 0:
                bpy.ops.mesh.select_less()
                bpy.ops.mesh.select_more()
                for i in range(inset-1):
                    bpy.ops.mesh.select_less()
            for v in bm.verts:
                v.select = False
            for edge in bm.edges:
                if edge.select:
                    edge.verts[0].select = True
                    edge.verts[1].select = True
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            # for each vertex in boundary loop of selected
            # find the nearest vertex of the boundary of the mesh
            # the minimum of all distances minus 0.05 is the smooth_distance
            minDist = float('inf')
            for co in meshBoundaryVerts:
                for co1 in selectedBoundaryVerts:
                    distCheck = vert_distance(co, co1)
                    if distCheck < minDist:
                        minDist = distCheck

            if inset > 0:
                # smooth to edge of tee box without touching the edge if outset is zero
                smooth_distance = (minDist - 0.05)
            else:
                bpy.ops.mesh.select_mode(type='VERT')
                bpy.ops.mesh.select_all(action='DESELECT')
                bpy.ops.mesh.select_mode(
                    use_extend=False, use_expand=False, type='VERT')
                for v in bm.verts:
                    v.select = True
                for edge in bm.edges:
                    edge.select = True
                for face in bm.faces:
                    face.select = True
                bmesh.update_edit_mesh(ob.data)
                smooth_distance = 0
            smooth_distance += outset
            bpy.ops.transform.translate(
                value=(0.0, 0.0, z_shift),
                orient_type='GLOBAL',
                orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                orient_matrix_type='GLOBAL',
                constraint_axis=(False, False, True),
                mirror=False,
                use_proportional_edit=True,
                proportional_edit_falloff='SMOOTH',
                proportional_size=smooth_distance,
                use_proportional_connected=False,
                use_proportional_projected=False
            )

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        # Reset selection back to Selected so no accidental "All" choices
        opcdtools.tees_selection_type = 'Selected'

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


# class WM_OT_removeFace(bpy.types.Operator):
#    """Select a Face in EDIT MODE"""
#    bl_label = "Remove Selected Face"
#    bl_idname = "wm.removeface"
#
#    def execute(self, context):
#        layout = self.layout
#        scene = context.scene
#        opcdtools = scene.opcdtools
#
#        #bpy.ops.object.vertex_group_select()
#        bpy.ops.mesh.separate(type='SELECTED')
#        bpy.ops.object.editmode_toggle()
#        select = bpy.context.selected_objects

#        solidify = select[1].modifiers.new("Solidify", 'SOLIDIFY')
#        solidify.offset = 0.0
#        solidify.thickness = 2
#        solidify.thickness_clamp = 0.0
#        solidify.use_rim = True

#        obj_modifier = select[0].modifiers.new('cutterModifier', 'BOOLEAN')
#        obj_modifier.object = select[1]
#        obj_modifier.operation = 'DIFFERENCE'
#
#        bpy.ops.object.convert(target='MESH')

#        bpy.ops.object.select_all(action='DESELECT')
#        select[1].select_set(True)
#        bpy.context.view_layer.objects.active = select[1]
#        bpy.ops.object.delete(use_global=False)
#
#        return {'FINISHED'}


class WM_OT_addCurbs(bpy.types.Operator):
    """Add Curbs to Selected Meshes"""
    bl_label = "Add Curbs to Selected"
    bl_idname = "wm.addcurbs"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selection_type = opcdtools.curbs_selection_type

        if "All" in selection_type:
            selected = [o for o in context.visible_objects if (
                'Spline' in o.name) and ('Blend' in o.name) and ('Concrete' in o.name)]

        if "Selected" in selection_type:
            selected = [o for o in context.selected_objects if (
                'Spline' in o.name) and ('Blend' in o.name)]

        if 'Curb' not in bpy.data.materials:
            bpy.data.materials.new(name="Curb")

        # Create Curb for Selected Meshes
        for ob in selected:
            if ob.type == 'MESH':
                bpy.ops.object.select_all(action='DESELECT')
                ob.select_set(True)
                bpy.context.view_layer.objects.active = ob

                bpy.ops.object.editmode_toggle()
                bpy.ops.mesh.select_mode(type="EDGE")
                bpy.ops.mesh.select_all(action='SELECT')

                bpy.ops.mesh.duplicate()

                bpy.ops.mesh.separate(type='SELECTED')
                bpy.ops.object.editmode_toggle()

                select = bpy.context.selected_objects

                bpy.ops.object.select_all(action='DESELECT')
                select[1].select_set(True)
                bpy.context.view_layer.objects.active = select[1]

                bpy.ops.object.editmode_toggle()
                bpy.ops.mesh.select_all(action='SELECT')

                bpy.ops.mesh.extrude_region_move(MESH_OT_extrude_region={"use_normal_flip": False, "mirror": False}, TRANSFORM_OT_translate={"value": (0, 0, 0.15), "orient_type": 'NORMAL', "orient_matrix": ((0, -1, 0), (1, 0, -0), (0, 0, 1)), "orient_matrix_type": 'NORMAL', "constraint_axis": (False, False, True), "mirror": False, "use_proportional_edit": False, "proportional_edit_falloff": 'SMOOTH',
                                                 "proportional_size": 1, "use_proportional_connected": False, "use_proportional_projected": False, "snap": False, "snap_target": 'CLOSEST', "snap_point": (0, 0, 0), "snap_align": False, "snap_normal": (0, 0, 0), "gpencil_strokes": False, "cursor_transform": False, "texture_space": False, "remove_on_cancel": False, "release_confirm": False, "use_accurate": False})

                bpy.ops.mesh.select_all(action='SELECT')

                bpy.ops.transform.translate(value=(0, 0, -0.13), orient_type='GLOBAL', orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), orient_matrix_type='GLOBAL', constraint_axis=(
                    False, False, True), mirror=True, use_proportional_edit=False, proportional_edit_falloff='SMOOTH', proportional_size=1, use_proportional_connected=True, use_proportional_projected=True)

                # bpy.ops.mesh.bevel(offset_type='OFFSET', offset=0.01, profile_type='SUPERELLIPSE', offset_pct=0, segments=1, profile=0.5, affect='EDGES', clamp_overlap=False, loop_slide=True, mark_seam=False, mark_sharp=True, material=-1, harden_normals=False)
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.normals_make_consistent(inside=False)

                bpy.ops.object.editmode_toggle()

                bpy.ops.object.shade_flat()

                curb = bpy.context.view_layer.objects.active

                pre = ob.data.name[:4]
                post = ob.data.name[15:]

                curb.name = "Spline " + pre + post + " Curb"

                bpy.ops.object.material_slot_remove()
                mat = bpy.data.materials.get("Curb")
                curb.data.materials.append(mat)

                bpy.ops.object.select_all(action='DESELECT')

        # Reset selection back to Selected so no accidental "All" choices
        opcdtools.curbs_selection_type = 'Selected'

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_addbulkheads_inner(bpy.types.Operator):
    """Add Bulkheads to Selected Meshes"""
    bl_label = "Add Bulkhead to Selected"
    bl_idname = "wm.addbulkheads_inner"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        bulkhead_type = opcdtools.bulkhead_type

        bulkhead_name = bulkhead_type

        selected = context.selected_objects

        bpy.data.objects[bulkhead_name].hide_set(False)

        # Create Bulkhead
        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob
            bpy.ops.object.duplicate_move()

            bpy.ops.object.convert(target='CURVE')
            curve = bpy.context.view_layer.objects.active

            # Get the curve data
            curve_data = curve.data

            # Iterate through all spline points on the curve
            for spline in curve_data.splines:
                for point in spline.points:
                    print(point.tilt)
                    # Set the tilt values of the point
                    # point.tilt = 0  # 1.5708 radians is equivalent to 90 degrees, matching the up vector [0, 0, 1]

            bpy.ops.object.select_all(action='DESELECT')

            bpy.data.objects[bulkhead_name].select_set(True)
            bpy.ops.object.duplicate_move()
            bulkhead = bpy.context.selected_objects
            bpy.context.view_layer.objects.active = bulkhead[0]

            array = bulkhead[0].modifiers.new("Array", 'ARRAY')

            if "Wood" in bulkhead_name:
                array.use_relative_offset = False
                array.use_constant_offset = True
                array.constant_offset_displace[0] = 2
                array.constant_offset_displace[1] = 0
                array.constant_offset_displace[2] = 0

            else:
                array.use_relative_offset = True
                array.use_constant_offset = False
                array.constant_offset_displace[0] = 1
                array.constant_offset_displace[1] = 0
                array.constant_offset_displace[2] = 0

            array.curve = curve
            array.fit_type = 'FIT_CURVE'

            curve_mod = bulkhead[0].modifiers.new("Curve", 'CURVE')
            curve_mod.object = curve
            curve_mod.deform_axis = 'POS_X'

            bpy.ops.object.select_all(action='DESELECT')

            pre = selected[0].data.name[6:]

            # rename curve
            curve.name = "Planted " + pre + " - Bulkhead - Curve"
            curve.data.name = curve.name

            bpy.ops.object.select_all(action='DESELECT')
            curve.select_set(True)
            bpy.context.view_layer.objects.active = curve
            selected = bpy.context.selected_objects

            move_collections_obj('Planted Objects', curve)

            # rename bulkhead
            bulkhead[0].name = "Planted " + pre + \
                " - Bulkhead - " + bulkhead_name
            bulkhead[0].data.name = bulkhead[0].name

            bpy.ops.object.select_all(action='DESELECT')
            bpy.context.view_layer.objects.active = bulkhead[0]
            bulkhead[0].select_set(True)

            move_collections_obj('Planted Objects', bulkhead[0])

            bpy.context.view_layer.objects.active = bulkhead[0]

            bpy.data.objects[bulkhead_name].hide_set(True)

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_flipdirection(bpy.types.Operator):
    """Flip Facing Direction of Selected Bulkhead"""
    bl_label = "Flip Direction of Selected"
    bl_idname = "wm.flipdirection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects
        ob = selected[0]
        ob_name = selected[0].name

        if 'Inner' in ob.name:
            if 'Wood_Wall_Inner' in ob.name:
                wallname = "Wood_Wall_Outer"
            elif 'Stone_Wall_Inner' in ob.name:
                wallname = "Stone_Wall_Outer"
            elif 'Rail_Tie_Inner' in ob.name:
                wallname = "Rail_Tie_Outer"

        elif 'Outer' in ob.name:
            if 'Wood_Wall_Outer' in ob.name:
                wallname = "Wood_Wall_Inner"
            elif 'Stone_Wall_Outer' in ob.name:
                wallname = "Stone_Wall_Inner"
            elif 'Rail_Tie_Outer' in ob.name:
                wallname = "Rail_Tie_Inner"

        bpy.ops.object.select_all(action='DESELECT')
        bpy.data.objects[wallname].hide_set(False)
        bpy.data.objects[wallname].select_set(True)

        bpy.ops.object.duplicate_move()

        obtarget = bpy.context.selected_objects

        bpy.context.view_layer.objects.active = ob
        ob.select_set(True)

        bpy.ops.object.make_links_data(type='MODIFIERS')

        bpy.ops.object.select_all(action='DESELECT')

        bpy.context.view_layer.objects.active = ob
        ob.select_set(True)

        bpy.ops.object.delete(use_global=False)

        obtarget[0].name = ob_name[:-15] + wallname
        obtarget[0].data.name = obtarget[0].name

        # move collections
        move_collections_obj('Planted Objects', obtarget[0])

        bpy.data.objects[wallname].hide_set(True)

        return {'FINISHED'}


class WM_OT_stakeandropes(bpy.types.Operator):
    """Add Stakes and Ropes Object to Selected Meshes"""
    bl_label = "Add Stakes and Ropes to Selected"
    bl_idname = "wm.addstakesandropes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        bpy.data.objects['Stakes_Ropes'].hide_set(False)

        # Create Stakes and Ropes
        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob
            bpy.ops.object.duplicate_move()

            # remove extraneous vertices to properly select curve
            bpy.ops.object.editmode_toggle()
            bpy.ops.mesh.select_mode(type="VERT")
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')
            bpy.ops.mesh.delete(type='VERT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.edge_face_add()

            bpy.ops.mesh.offset_edges(geometry_mode='extrude', width=0.1,
                                      depth_mode='angle', angle=0, follow_face=True, caches_valid=False)

            bpy.ops.mesh.select_all(action='INVERT')
            bpy.ops.mesh.select_mode(type="FACE")
            bpy.ops.mesh.delete(type='VERT')
            bpy.ops.object.editmode_toggle()

            bpy.ops.object.convert(target='CURVE')
            curve = bpy.context.view_layer.objects.active

            bpy.ops.object.select_all(action='DESELECT')

            bpy.data.objects['Stakes_Ropes'].select_set(True)
            bpy.ops.object.duplicate_move()
            bulkhead = bpy.context.selected_objects
            bpy.context.view_layer.objects.active = bulkhead[0]

            array = bulkhead[0].modifiers.new("Array", 'ARRAY')
            array.use_relative_offset = False
            array.use_constant_offset = True
            array.constant_offset_displace[0] = 3
            array.constant_offset_displace[1] = 0
            array.constant_offset_displace[2] = 0

            array.curve = curve
            array.fit_type = 'FIT_CURVE'

            curve_mod = bulkhead[0].modifiers.new("Curve", 'CURVE')
            curve_mod.object = curve
            curve_mod.deform_axis = 'POS_X'

            bpy.ops.object.select_all(action='DESELECT')

            pre = ob.data.name[15:]

            # rename curve
            curve.name = "Planted " + pre + " - Stakes and Ropes - Curve"
            curve.data.name = curve.name

            # move collections for Curves
            move_collections_obj('Planted Objects', curve)

            # rename objects
            bulkhead[0].name = "Planted " + pre + " - Stakes and Ropes"
            bulkhead[0].data.name = bulkhead[0].name

            # move collections for objects
            move_collections_obj('Planted Objects', bulkhead[0])

            bpy.data.objects['Stakes_Ropes'].hide_set(True)

        return {'FINISHED'}


class WM_OT_hazardstake(bpy.types.Operator):
    """Add Hazard Stakes to Selected Meshes"""
    bl_label = "Add Hazard Stakes to Selected"
    bl_idname = "wm.addhazardstake"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        stake_color = opcdtools.stake_color
        stake_spacing = opcdtools.stake_spacing
        stake_offset = opcdtools.stake_offset_value

        selected = bpy.context.selected_objects
        ob = selected[0]
        ob_name = selected[0].data.name

        stake_name = 'Hazard Stake ' + stake_color

        bpy.data.objects[stake_name].hide_set(False)

        # Create Hazard Stake
        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            bpy.ops.object.duplicate_move()

            bpy.ops.object.editmode_toggle()
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()

            bpy.ops.mesh.offset_edges(geometry_mode='extrude', width=stake_offset, angle=0, follow_face=False,
                                      mirror_modifier=False, edge_rail=False, threshold=0.000872665, caches_valid=False, angle_presets='0°')
            bpy.ops.mesh.select_all(action='INVERT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='FACE')
            bpy.ops.mesh.delete(type='VERT')

            bpy.ops.object.editmode_toggle()

            bpy.ops.object.convert(target='CURVE')
            curve = bpy.context.view_layer.objects.active

            bpy.ops.object.select_all(action='DESELECT')

            bpy.data.objects[stake_name].select_set(True)
            bpy.ops.object.duplicate_move()
            newobj = bpy.context.selected_objects
            bpy.context.view_layer.objects.active = newobj[0]

            array = newobj[0].modifiers.new("Array", 'ARRAY')
            array.use_relative_offset = False
            array.use_constant_offset = True

            array.constant_offset_displace[0] = stake_spacing
            array.constant_offset_displace[1] = 0
            array.constant_offset_displace[2] = 0

            array.relative_offset_displace[0] = 0
            array.relative_offset_displace[1] = 0
            array.relative_offset_displace[2] = 0

            array.curve = curve
            array.fit_type = 'FIT_CURVE'

            curve_mod = newobj[0].modifiers.new("Curve", 'CURVE')
            curve_mod.object = curve
            curve_mod.deform_axis = 'POS_X'

            bpy.ops.object.convert(target='MESH')

            # rename spline
            post = ob_name[15:]
            newobj[0].name = "Planted " + post + " - " + stake_name
            newobj[0].data.name = newobj[0].name

            # move collections
            move_collections_obj('Planted Objects', newobj[0])

            bpy.ops.object.select_all(action='DESELECT')

            bpy.context.view_layer.objects.active = curve
            curve.select_set(True)

            bpy.ops.object.delete(use_global=False)

            bpy.data.objects[stake_name].hide_set(True)

        return {'FINISHED'}


class WM_OT_addbridge(bpy.types.Operator):
    """Must select 2 vertices in EDIT MODE"""
    bl_label = "Add Bridge"
    bl_idname = "wm.addbridge"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        bridge_type = opcdtools.bridge_type

        bridge_name = 'Bridge - ' + bridge_type

        selected = bpy.context.selected_objects
        ob = selected[0]
        ob_name = selected[0].data.name

        bpy.ops.mesh.duplicate_move()
        bpy.ops.mesh.edge_face_add()
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.editmode_toggle()
        temp_select = bpy.context.selected_objects
        bpy.ops.object.select_all(action='DESELECT')
        temp_select[1].select_set(True)
        bpy.context.view_layer.objects.active = temp_select[1]
        active = bpy.context.view_layer.objects.active
        bpy.ops.object.transform_apply(
            location=True, rotation=True, scale=True)
        bpy.ops.object.convert(target='CURVE')
        bpy.ops.object.editmode_toggle()
        bpy.ops.curve.spline_type_set(type='BEZIER')
        bpy.ops.object.editmode_toggle()

        curve = bpy.context.selected_objects

        # move collections
        move_collections_obj('Planted Objects', curve[0])

        post = ob_name[15:]

        # rename curve
        curve[0].name = "Planted " + post + \
            " - Bridge - " + bridge_type + " - Curve"
        curve[0].data.name = curve[0].name

        bpy.data.objects[bridge_name].hide_set(False)

        bpy.ops.object.select_all(action='DESELECT')

        bpy.data.objects[bridge_name].select_set(True)

        bpy.ops.object.duplicate_move()

        bridge = bpy.context.selected_objects
        bpy.context.view_layer.objects.active = bridge[0]

        # move collections
        move_collections_obj('Planted Objects', bridge[0])

        array = bridge[0].modifiers.new("Array", 'ARRAY')
        array.use_relative_offset = True
        array.use_constant_offset = False
        array.constant_offset_displace[0] = 1
        array.constant_offset_displace[1] = 0
        array.constant_offset_displace[2] = 0

        array.curve = curve[0]
        array.fit_type = 'FIT_CURVE'

        curve_mod = bridge[0].modifiers.new("Curve", 'CURVE')
        curve_mod.object = curve[0]
        curve_mod.deform_axis = 'POS_X'

        bpy.ops.object.select_all(action='DESELECT')

        # rename bridge
        bridge[0].name = "Planted " + post + " - Bridge - " + bridge_type
        bridge[0].data.name = bridge[0].name

        bpy.data.objects[bridge_name].hide_set(True)

        return {'FINISHED'}


class WM_OT_bridgenarrow(bpy.types.Operator):
    """Narrow Selected Bridge"""
    bl_label = "Narrow Selected"
    bl_idname = "wm.bridgenarrow"

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        bpy.context.scene.tool_settings.transform_pivot_point = 'MEDIAN_POINT'
        bpy.context.scene.tool_settings.use_transform_correct_face_attributes = False

        for ob in selected:
            if 'Bridge' in ob.name:
                bpy.ops.object.editmode_toggle()
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.transform.resize(value=(1, 0.8, 1), orient_type='GLOBAL', orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), orient_matrix_type='GLOBAL', constraint_axis=(
                    False, True, False), mirror=True, use_proportional_edit=False, proportional_edit_falloff='SPHERE', proportional_size=1, use_proportional_connected=True, use_proportional_projected=False)
                bpy.ops.object.editmode_toggle()

        return {'FINISHED'}


class WM_OT_bridgewiden(bpy.types.Operator):
    """Widen Selected Bridge"""
    bl_label = "Widen Selected"
    bl_idname = "wm.bridgewiden"

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        bpy.context.scene.tool_settings.transform_pivot_point = 'MEDIAN_POINT'
        bpy.context.scene.tool_settings.use_transform_correct_face_attributes = False

        for ob in selected:
            if 'Bridge' in ob.name:
                bpy.ops.object.editmode_toggle()
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.transform.resize(value=(1, 1.2, 1), orient_type='GLOBAL', orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), orient_matrix_type='GLOBAL', constraint_axis=(
                    False, True, False), mirror=True, use_proportional_edit=False, proportional_edit_falloff='SPHERE', proportional_size=1, use_proportional_connected=True, use_proportional_projected=False)
                bpy.ops.object.editmode_toggle()

        return {'FINISHED'}


class WM_OT_removesupports(bpy.types.Operator):
    """Must select a Bridge Object"""
    bl_label = "Remove Bridge Supports"
    bl_idname = "wm.removesupports"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        for o in selected:
            if 'Bridge' in o.name:
                bpy.ops.object.select_all(action='DESELECT')
                o.select_set(True)
                bpy.context.view_layer.objects.active = o

                bpy.ops.object.editmode_toggle()
                bpy.ops.mesh.select_all(action='DESELECT')
                bpy.ops.object.vertex_group_select()
                bpy.ops.mesh.delete(type='FACE')
                bpy.ops.object.editmode_toggle()

        return {'FINISHED'}


class WM_OT_addstairs(bpy.types.Operator):
    """Must select 2 vertices in EDIT MODE"""
    bl_label = "Add Stairs"
    bl_idname = "wm.addstairs"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        stairs_type = opcdtools.stairs_type
        stairs_name = 'Stairs - ' + stairs_type

        selected = bpy.context.selected_objects
        ob = selected[0]
        ob_name = selected[0].data.name

        bpy.ops.mesh.duplicate_move()
        bpy.ops.mesh.edge_face_add()
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.editmode_toggle()
        temp_select = bpy.context.selected_objects
        bpy.ops.object.select_all(action='DESELECT')
        temp_select[1].select_set(True)
        bpy.context.view_layer.objects.active = temp_select[1]
        active = bpy.context.view_layer.objects.active
        bpy.ops.object.transform_apply(
            location=True, rotation=True, scale=True)
        bpy.ops.object.convert(target='CURVE')
        bpy.ops.object.editmode_toggle()
        bpy.ops.curve.spline_type_set(type='BEZIER')
        bpy.ops.object.editmode_toggle()

        curve = bpy.context.selected_objects

        # rename curve
        post = ob_name[15:]

        # move collections
        move_collections_obj('Planted Objects', curve[0])

        curve[0].name = "Planted " + post + \
            " - Stairs - " + stairs_type + " - Curve"
        curve[0].data.name = curve[0].name

        bpy.data.objects[stairs_name].hide_set(False)

        bpy.ops.object.select_all(action='DESELECT')

        bpy.data.objects[stairs_name].select_set(True)

        bpy.context.view_layer.objects.active = bpy.data.objects[stairs_name]

        bpy.ops.object.duplicate_move()

        stairs = bpy.context.selected_objects

        bpy.ops.object.select_all(action='DESELECT')

        for o in stairs:
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            # move collections
            move_collections_obj('Planted Objects', o)

            array = o.modifiers.new("Array", 'ARRAY')
            array.use_relative_offset = False
            array.use_constant_offset = True

            # if 'Stone Type 1 - Steps' in o.name:
            if 'Stone Type 1' in o.name:
                array.constant_offset_displace[0] = 1.96
            else:
                array.constant_offset_displace[0] = 2

            array.constant_offset_displace[1] = 0
            array.constant_offset_displace[2] = 0

            array.curve = curve[0]
            array.fit_type = 'FIT_CURVE'

            curve_mod = o.modifiers.new("Curve", 'CURVE')
            curve_mod.object = curve[0]
            curve_mod.deform_axis = 'POS_X'

        bpy.ops.object.select_all(action='DESELECT')

        # rename stairs
        stairs[0].name = "Planted " + post + " - Stairs - " + stairs_type
        stairs[0].data.name = stairs[0].name

        bpy.data.objects[stairs_name].hide_set(True)

        return {'FINISHED'}


class WM_OT_rotatestairs(bpy.types.Operator):
    """Stairs Must be Selected"""
    bl_label = "Rotate Selected Steps"
    bl_idname = "wm.rotatestairs"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        bpy.context.scene.tool_settings.transform_pivot_point = 'INDIVIDUAL_ORIGINS'

        bpy.ops.object.editmode_toggle()
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.object.vertex_group_select()
        bpy.ops.transform.rotate(value=0.174533, orient_axis='Y', orient_type='GLOBAL', orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), orient_matrix_type='GLOBAL', constraint_axis=(
            False, True, False), mirror=True, use_proportional_edit=False, proportional_edit_falloff='SPHERE', proportional_size=1, use_proportional_connected=True, use_proportional_projected=False)
        bpy.ops.object.editmode_toggle()

        return {'FINISHED'}


class WM_OT_stairsnarrow(bpy.types.Operator):
    """Narrow Selected Stairs"""
    bl_label = "Narrow Selected"
    bl_idname = "wm.stairsnarrow"

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        bpy.context.scene.tool_settings.transform_pivot_point = 'MEDIAN_POINT'
        bpy.context.scene.tool_settings.use_transform_correct_face_attributes = False

        for ob in selected:
            if 'Stairs' in ob.name:
                bpy.ops.object.editmode_toggle()
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.transform.resize(value=(1, 0.9, 1), orient_type='GLOBAL', orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), orient_matrix_type='GLOBAL', constraint_axis=(
                    False, True, False), mirror=True, use_proportional_edit=False, proportional_edit_falloff='SPHERE', proportional_size=1, use_proportional_connected=True, use_proportional_projected=False)
                bpy.ops.object.editmode_toggle()

        return {'FINISHED'}


class WM_OT_stairswiden(bpy.types.Operator):
    """Widen Selected Stairs"""
    bl_label = "Widen Selected"
    bl_idname = "wm.stairswiden"

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        bpy.context.scene.tool_settings.transform_pivot_point = 'MEDIAN_POINT'
        bpy.context.scene.tool_settings.use_transform_correct_face_attributes = False

        for ob in selected:
            if 'Stairs' in ob.name:
                bpy.ops.object.editmode_toggle()
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.transform.resize(value=(1, 1.1, 1), orient_type='GLOBAL', orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), orient_matrix_type='GLOBAL', constraint_axis=(
                    False, True, False), mirror=True, use_proportional_edit=False, proportional_edit_falloff='SPHERE', proportional_size=1, use_proportional_connected=True, use_proportional_projected=False)
                bpy.ops.object.editmode_toggle()

        return {'FINISHED'}


class WM_OT_removestairs(bpy.types.Operator):
    """Stairs Must be Selected"""
    bl_label = "Remove Selected Stairs"
    bl_idname = "wm.removestairs"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        for o in selected:
            if "Stairs" in o.name:
                bpy.ops.object.select_all(action='DESELECT')
                bpy.context.view_layer.objects.active = o
                bpy.ops.object.select_grouped(type='CHILDREN_RECURSIVE')
                o.select_set(True)

                # find curve
                if "." in o.name:
                    pre = o.name[:-15]
                    post = o.name[-3:]
                    curve = pre + "Curve." + post
                else:
                    pre = o.name[:-11]
                    curve = pre + "Curve"

                bpy.data.objects[curve].select_set(True)

                bpy.ops.object.delete(use_global=False)

        return {'FINISHED'}


class WM_OT_addbed(bpy.types.Operator):
    """Must select be in Edit Mode and Face Select"""
    bl_label = "Add Raised Bed  (Face Select in Edit Mode)"
    bl_idname = "wm.addbed"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        wallheight = opcdtools.wallheight + 0.2
        wallwidth = opcdtools.wallwidth

        if 'Planter_Mulch' not in bpy.data.materials:
            bpy.data.materials.new(name="Planter_Mulch")
        if 'Planter_Wall' not in bpy.data.materials:
            bpy.data.materials.new(name="Planter_Wall")

        for mat in bpy.data.materials:
            if "Planter_Mulch" in mat.name:
                mat.diffuse_color = (1, 0.5, 0, 1)
            if "Planter_Wall" in mat.name:
                mat.diffuse_color = (0, 1, 0, 1)

        bpy.ops.mesh.duplicate()

        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.editmode_toggle()

        temp_select = bpy.context.selected_objects
        bpy.ops.object.select_all(action='DESELECT')
        temp_select[1].select_set(True)
        bpy.context.view_layer.objects.active = temp_select[1]

        bpy.ops.object.editmode_toggle()
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.select_mode(type="FACE")
        bpy.ops.mesh.subdivide(number_cuts=2)

        bpy.ops.mesh.region_to_loop()
        bpy.ops.mesh.looptools_relax(
            input='selected', interpolation='cubic', iterations='3', regular=True)
        bpy.ops.mesh.offset_edges(
            geometry_mode='extrude', width=wallwidth, angle=0, follow_face=False, caches_valid=False)
        bpy.ops.mesh.select_more()
        bpy.ops.mesh.extrude_region_move(MESH_OT_extrude_region={"use_normal_flip": False, "mirror": False}, TRANSFORM_OT_translate={"value": (0, 0, wallheight), "orient_type": 'LOCAL', "orient_matrix": ((1, 0, 0), (0, 1, 0), (0, 0, 1)), "orient_matrix_type": 'LOCAL', "constraint_axis": (False, False, True), "mirror": False, "use_proportional_edit": False, "proportional_edit_falloff": 'SMOOTH',
                                         "proportional_size": 1, "use_proportional_connected": False, "use_proportional_projected": False, "snap": False, "snap_target": 'CLOSEST', "snap_point": (0, 0, 0), "snap_align": False, "snap_normal": (0, 0, 0), "gpencil_strokes": False, "cursor_transform": False, "texture_space": False, "remove_on_cancel": False, "release_confirm": False, "use_accurate": False})
        bpy.ops.mesh.bevel(offset=0.02, offset_pct=0)
        bpy.ops.object.editmode_toggle()

        bpy.context.object.active_material_index = 0
        bpy.ops.object.material_slot_remove()

        mat = bpy.data.materials.get("Planter_Mulch")

        # assign new Material
        temp_select[1].data.materials.append(mat)

        bpy.context.object.active_material_index = 1

        mat = bpy.data.materials.get("Planter_Wall")

        # assign new Material
        temp_select[1].data.materials.append(mat)

        bpy.ops.object.editmode_toggle()
        bpy.ops.mesh.select_mode(type="FACE")
        bpy.ops.mesh.select_more()
        bpy.ops.object.material_slot_assign()
        bpy.ops.mesh.select_all(action='INVERT')
        bpy.context.object.active_material_index = 0
        bpy.ops.object.material_slot_assign()

        move = wallheight - 0.1
        bpy.ops.transform.translate(value=(0, 0, move), orient_type='GLOBAL', orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), orient_matrix_type='GLOBAL', constraint_axis=(
            False, False, True), mirror=True, use_proportional_edit=False, proportional_edit_falloff='SPHERE', proportional_size=1, use_proportional_connected=True, use_proportional_projected=False)

        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.transform.translate(value=(0, 0, -0.2), orient_type='GLOBAL', orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), orient_matrix_type='GLOBAL', constraint_axis=(
            False, False, True), mirror=True, use_proportional_edit=False, proportional_edit_falloff='SPHERE', proportional_size=1, use_proportional_connected=True, use_proportional_projected=False)

        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.context.object.active_material_index = 0
        bpy.ops.object.material_slot_select()

        bpy.ops.mesh.select_mode(type="VERT")
        bpy.ops.mesh.select_less()
        bpy.ops.transform.translate(value=(0, 0, 0.1), orient_type='GLOBAL', orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), orient_matrix_type='GLOBAL', constraint_axis=(
            False, False, True), mirror=True, use_proportional_edit=False, proportional_edit_falloff='SPHERE', proportional_size=1, use_proportional_connected=True, use_proportional_projected=False)

        bpy.ops.object.editmode_toggle()
        bpy.ops.object.shade_smooth()

        # move collections with function
        move_collections_obj('Planted Objects', temp_select[1])

        # rename spline
        post = temp_select[0].data.name
        temp_select[1].name = "Planted " + post + " - Planter"
        temp_select[1].data.name = temp_select[1].name

        return {'FINISHED'}


class WM_OT_createouter(bpy.types.Operator):
    """Create Outer Mesh and Conform to Outer Terrain"""
    bl_label = "Create Outer Mesh and Conform to Terrain"
    bl_idname = "wm.createouter"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools
        selection_type = opcdtools.conform_selection_type
        inner_size = opcdtools.inner_terrain_size
        outer_size = opcdtools.outer_terrain_size
        diff_size = outer_size - inner_size

        # Add Autogen Material Course Plane
        autogen_mat = 'Outerplot'

        if 'Outerplot' not in bpy.data.materials:
            bpy.data.materials.new(name="Outerplot")

        outer_size = outer_size*0.98

        cursor = bpy.context.scene.cursor.location = (
            -inner_size/2, -inner_size/2, 0.0)
        print(cursor)

        # Create Cube
        bpy.ops.mesh.primitive_cube_add(calc_uvs=True, size=inner_size-10,
                                        enter_editmode=False, align='WORLD', location=(cursor), rotation=(0, 0, 0))
        cube = bpy.context.object
        bpy.ops.object.move_to_collection(collection_index=0)
        bpy.ops.object.transform_apply(
            location=True, rotation=True, scale=True)

        # Create Plane
        bpy.ops.mesh.primitive_plane_add(
            calc_uvs=True, size=outer_size, enter_editmode=False, align='WORLD', location=(cursor), rotation=(0, 0, 0))
        outer = bpy.context.object

        bpy.ops.object.move_to_collection(collection_index=0)
        bpy.ops.object.transform_apply(
            location=True, rotation=True, scale=True)
        outer.name = 'Outerplot - Mesh'
        outer.data.name = 'Outerplot - Mesh'

        color_to_vertices('red', 1.0)

        me = outer.data
        bm = bmesh.new()
        bm.from_mesh(me)

        targetsize = 20
        cuts = math.ceil(outer_size/targetsize)

        # subdivide
        bmesh.ops.subdivide_edges(bm,
                                  edges=bm.edges,
                                  cuts=cuts,
                                  use_grid_fill=True,
                                  )

        # Write back to the mesh
        bm.to_mesh(me)
        me.update()

        # Get material
        mat = bpy.data.materials.get(autogen_mat)
        # Assign it to object
        if outer.data.materials:
            # assign to 1st material slot
            outer.data.materials[0] = mat
        else:
            # no slots
            outer.data.materials.append(mat)

        # subtract Cube from Outer Terrain
        obj_modifier = outer.modifiers.new('Boolean', 'BOOLEAN')
        obj_modifier.object = bpy.data.objects[cube.name]
        obj_modifier.operation = 'DIFFERENCE'
        obj_modifier.solver = 'FAST'
        bpy.ops.object.modifier_apply(modifier='Boolean')

        # remove Cube
        bpy.ops.object.select_all(action='DESELECT')
        bpy.data.objects[cube.name].select_set(True)
        bpy.ops.object.delete()

        bpy.context.scene.cursor.location = (0, 0, 0)

        return {'FINISHED'}


class WM_OT_removeblend(bpy.types.Operator):
    """Remove Blend and Assign Inner Material"""
    bl_label = "Remove Blend"
    bl_idname = "wm.removeblend"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        # Remove Blend Routine
        for o in selected:
            bpy.ops.object.select_all(action='DESELECT')
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            if 'Blend' in o.name:
                print("Removing Blend - ", o.name)
                bpy.ops.object.mode_set(mode='EDIT')

                # Vert Paint Red
                bpy.ops.mesh.select_all(action='SELECT')
                color_to_vertices('red', 1.0)

                mat_name = bpy.context.active_object.active_material.name
                new_mat = mat_name[:-8]

                if 'Blend' in mat_name:

                    # Assign Material - remove Blend mat
                    mat = bpy.data.materials.get(new_mat)

                    if o.data.materials:
                        o.data.materials[0] = mat
                    else:
                        o.data.materials.append(mat)

                bpy.ops.object.mode_set(mode='OBJECT')

                if (o.name[-3:]).isnumeric():
                    o.name = o.name[:-12] + '_-_Mesh'
                else:
                    o.name = o.name[:-8] + '_-_Mesh'

        return {'FINISHED'}


class WM_OT_addblend(bpy.types.Operator):
    """Add Blend"""
    bl_label = "Add Blend"
    bl_idname = "wm.addblend"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        # Add Blend Routine
        for o in selected:
            bpy.ops.object.select_all(action='DESELECT')
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            if 'Mesh' in o.name:
                print("Add Blend - ", o.name)

                mat_name = bpy.context.active_object.active_material.name
                print(mat_name)
                new_mat = o.active_material.name + ' - Blend'

                bpy.ops.object.mode_set(mode='EDIT')

                # vertex group select
                bpy.ops.mesh.select_all(action='DESELECT')
                bpy.ops.object.vertex_group_select()
                bpy.ops.object.mode_set(mode='OBJECT')

                # Black
                color_to_vertices('black', 1.0)

                mat_name = bpy.context.active_object.active_material.name
                new_mat = mat_name + ' - Blend'

                # Assign Material - remove Blend mat
                if new_mat in bpy.data.materials:
                    mat = bpy.data.materials.get(new_mat)
                else:
                    mat = bpy.data.materials.new(new_mat)
                    mat.diffuse_color = (0.8, 0.16, 0.5, 1)

                if o.data.materials:
                    o.data.materials[0] = mat
                else:
                    o.data.materials.append(mat)

                if (o.name[-3:]).isnumeric():
                    o.name = o.name[:-11] + '_-_Blend'
                else:
                    o.name = o.name[:-7] + '_-_Blend'

        return {'FINISHED'}


class WM_OT_addblendinset(bpy.types.Operator):
    """Add Blend Inset"""
    bl_label = "Add Blend Inset"
    bl_idname = "wm.addblendinset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        inset_distance = opcdtools.inset_distance

        selected = bpy.context.selected_objects

        # Add Blend Inset to a Blend-less Island Mesh
        for o in selected:
            bpy.ops.object.select_all(action='DESELECT')
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            if 'Mesh' in o.name:
                print("Add Blend Inset - ", o.name)

                bpy.ops.object.mode_set(mode='EDIT')

                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.inset(use_boundary=True, use_even_offset=True, use_relative_offset=False, use_edge_rail=False,
                                   thickness=inset_distance, depth=0, use_outset=False, use_select_inset=False, use_individual=False, use_interpolate=True)

                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.region_to_loop()
                color_to_vertices('black', 1.0)

                bpy.ops.object.vertex_group_assign()
                bpy.ops.mesh.select_more(use_face_step=True)

                bpy.ops.mesh.separate(type='SELECTED')

                bpy.ops.object.mode_set(mode='OBJECT')

                temp_select = bpy.context.selected_objects

                bpy.context.view_layer.objects.active = temp_select[1]
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.remove_doubles(threshold=0.01)

                bpy.ops.object.mode_set(mode='OBJECT')
                temp_select = bpy.context.selected_objects
                bpy.context.view_layer.objects.active = temp_select[0]

                mat_name = bpy.context.active_object.active_material.name
                new_mat = mat_name + ' - Blend'

                # Assign Material - remove Blend mat
                if new_mat in bpy.data.materials:
                    mat = bpy.data.materials.get(new_mat)
                else:
                    mat = bpy.data.materials.new(new_mat)
                    mat.diffuse_color = (0.8, 0.16, 0.5, 1)

                if temp_select[1].data.materials:
                    temp_select[1].data.materials[0] = mat
                else:
                    temp_select[1].data.materials.append(mat)

                if (temp_select[1].name[-3:]).isnumeric():
                    temp_select[1].name = temp_select[1].name[:-
                                                              11] + '_-_Blend'
                else:
                    temp_select[1].name = temp_select[1].name[:-7] + '_-_Blend'

        bpy.ops.object.select_all(action='DESELECT')
        for o in selected:
            o.select_set(True)

        return {'FINISHED'}


class WM_OT_separation(bpy.types.Operator):
    """Separates Selected Vertices into Mesh Type of Dropdown"""
    bl_label = "Mesh Separation"
    bl_idname = "wm.separatemesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        original_mode = context.object.mode

        try:
            scene = context.scene
            opcdtools = scene.opcdtools
            separate_mat_selection = opcdtools.separate_mat_selection

            bpy.ops.object.mode_set(mode='OBJECT')
            selected = bpy.context.selected_objects

            for o in selected:
                bpy.ops.object.select_all(action='DESELECT')
                o.select_set(True)
                bpy.context.view_layer.objects.active = o

                bpy.ops.object.duplicate()
                o.hide_set(True)

                bpy.ops.object.mode_set(mode='EDIT')

                bpy.ops.mesh.separate(type="SELECTED")
                bpy.ops.object.mode_set(mode='OBJECT')

                mesh = bpy.context.selected_objects

                # Vertex Paint the unselected portion Red
                bpy.ops.object.select_all(action='DESELECT')
                mesh[0].select_set(True)
                bpy.context.view_layer.objects.active = mesh[0]
                bpy.ops.object.mode_set(mode='EDIT')

                bpy.ops.mesh.select_all(action='SELECT')
                color_to_vertices('red', 1.0)
                bpy.ops.object.mode_set(mode='OBJECT')

                # Rename the selected portion
                bpy.ops.object.select_all(action='DESELECT')
                mesh[1].select_set(True)
                bpy.context.view_layer.objects.active = mesh[1]

                # Your renaming and material logic...
                truncated_name = "_".join(mesh[1].name.split("_")[:-3])
                if separate_mat_selection in bpy.data.materials:
                    mat = bpy.data.materials.get(separate_mat_selection)
                else:
                    mat = bpy.data.materials.new(separate_mat_selection)
                    mat.diffuse_color = (0.8, 0.16, 0.5, 1)

                if mesh[1].data.materials:
                    mesh[1].data.materials[0] = mat
                else:
                    mesh[1].data.materials.append(mat)

                if 'Blend' in mesh[1].name:
                    mesh[1].name = truncated_name + "_" + \
                        separate_mat_selection + '_-_Blend'
                if 'Mesh' in mesh[1].name:
                    mesh[1].name = truncated_name + "_" + \
                        separate_mat_selection + '_-_Mesh'

        finally:
            if context.object and context.object.mode != original_mode:
                bpy.ops.object.mode_set(mode=original_mode)

        return {'FINISHED'}


class WM_OT_invertselection(bpy.types.Operator):
    """Invert Selection of Vertices"""
    bl_label = "Mesh Separation"
    bl_idname = "wm.invertselection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        active_obj = context.active_object
        original_mode = active_obj.mode

        try:
            # If not in Edit Mode, switch to Edit Mode
            if original_mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')
                # face mode to support the normal use case...
                # https://www.loom.com/share/c937c148baf248e58a2666b7926083b5
                bpy.ops.mesh.select_mode(
                    use_extend=False, use_expand=False, type='FACE')

            bpy.ops.mesh.select_all(action='INVERT')

        finally:
            if original_mode != 'EDIT':
                bpy.ops.mesh.select_mode(
                    use_extend=False, use_expand=False, type='VERT')
                bpy.ops.object.mode_set(mode=original_mode)

        return {'FINISHED'}


class WM_OT_zshiftloop(bpy.types.Operator):
    """Z Shift vertices of a loop"""
    bl_label = "Z Shift Loop"
    bl_idname = "wm.zshiftloop"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        loop_inset = opcdtools.zshiftloop_inset
        z_shift = opcdtools.zshiftloop_z_shift
        only_longest = opcdtools.zshiftloop_onlylongest

        selected = bpy.context.selected_objects

        counter = 1
        total = len(selected)
        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Painting -", ob.name, counter, " of ", total)

            mesh = (ob.data)

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')
            bpy.ops.mesh.select_all(action='SELECT')

            if loop_inset > 0:
                bpy.ops.mesh.region_to_loop()
                bpy.ops.mesh.select_all(action='INVERT')

                for i in range(loop_inset-1):
                    bpy.ops.mesh.select_less()

            bpy.ops.mesh.region_to_loop()

            if only_longest:
                select_external_boundary()

            bpy.ops.transform.translate(
                value=(0.0, 0.0, z_shift),
                orient_type='GLOBAL',
                orient_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                orient_matrix_type='GLOBAL',
                constraint_axis=(False, False, True),
                mirror=False,
                use_proportional_edit=False
            )
            bpy.ops.object.mode_set(mode='OBJECT')
            counter = counter + 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_normalsdatatransfer(bpy.types.Operator):
    """Transfer Normals Data from the Active Object to the Initial Selected Object"""
    bl_label = "Normals Data Transfer"
    bl_idname = "wm.normalsdatatransfer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        # Check if there are any selected objects
        if selected and len(selected) == 2:

            # assign variable to active object
            active_obj = bpy.context.active_object

            # assign variable to first object selected
            for ob in selected:
                if ob != active_obj:
                    first_obj = ob

            print("First Selected - ", first_obj.name)

            # Add Auto Smooth Normals to selected Meshes at 60 degrees
            for ob in selected:
                bpy.ops.object.select_all(action='DESELECT')
                ob.select_set(True)
                bpy.context.view_layer.objects.active = ob
                bpy.context.object.data.use_auto_smooth = True
                bpy.context.object.data.auto_smooth_angle = 1.0472

            # deselect all objects and add Vertex Group and Data Transfer on First Object selected
            bpy.ops.object.select_all(action='DESELECT')
            first_obj.select_set(True)
            bpy.context.view_layer.objects.active = first_obj

            print("Adding Data Transfer to Mesh - ", first_obj.name)

            # add Vertex Group "Normals" to outer loop
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(type="FACE")

            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()

            if "LoopNormal" in first_obj.vertex_groups:
                print(first_obj.name,
                      " - Already has a Loop Boundary Normals Vertex Group")

            else:
                bpy.context.object.vertex_groups.new(name='LoopNormal')
                bpy.ops.object.vertex_group_assign()

            bpy.ops.object.mode_set(mode='OBJECT')

            # check if a modifier exists, then add a Data Transfer modifier using Custom Normals from the Outer mesh
            if first_obj.modifiers:
                print(first_obj.name, " - Already has a Modifier")

            else:
                data_transfer_mod = first_obj.modifiers.new(
                    "Data Transfer", 'DATA_TRANSFER')
                data_transfer_mod.object = active_obj
                data_transfer_mod.mix_mode = 'REPLACE'
                data_transfer_mod.mix_factor = 1
                data_transfer_mod.vertex_group = "LoopNormal"
                data_transfer_mod.use_loop_data = True
                data_transfer_mod.data_types_loops = {'CUSTOM_NORMAL'}
                data_transfer_mod.loop_mapping = 'NEAREST_POLYNOR'

        return {'FINISHED'}


class WM_OT_vertexgroupassign(bpy.types.Operator):
    """Assign Selected Vertices to Specified Group Name"""
    bl_label = "Assign Vertex Group"
    bl_idname = "wm.vertexgroupassign"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        vtx_group_name = opcdtools.vtx_group_name

        selected_obj = bpy.context.active_object

        if vtx_group_name in selected_obj.vertex_groups:
            # print(selected_obj.name," - the Vertex Group Name Already Exists on this Mesh")
            bpy.context.window_manager.popup_menu(lambda self, context: self.layout.label(
                text="The Vertex Group Name Already Exists on this Mesh"), title="Warning", icon='ERROR')

        else:
            bpy.context.object.vertex_groups.new(name=vtx_group_name)
            bpy.ops.object.vertex_group_assign()

        return {'FINISHED'}


class WM_OT_normaltransfervertexgroup(bpy.types.Operator):
    """Transfer Normals from Active Object to Vertex Group"""
    bl_label = "Transfer Normals to Selected Vertex Group"
    bl_idname = "wm.normaltransfervertexgroup"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        vtx_group_name = opcdtools.vtx_group_name

        selected = bpy.context.selected_objects

        # Check if there are any selected objects
        if selected and len(selected) == 2:

            # assign variable to active object
            active_obj = bpy.context.active_object

            # assign variable to first object selected
            for ob in selected:
                if ob != active_obj:
                    first_obj = ob

            print("First Selected - ", first_obj.name)

            # Add Auto Smooth Normals to selected Meshes at 60 degrees
            for ob in selected:
                bpy.ops.object.select_all(action='DESELECT')
                ob.select_set(True)
                bpy.context.view_layer.objects.active = ob
                bpy.context.object.data.use_auto_smooth = True
                bpy.context.object.data.auto_smooth_angle = 1.0472

            # deselect all objects and add Vertex Group and Data Transfer on First Object selected
            bpy.ops.object.select_all(action='DESELECT')
            first_obj.select_set(True)
            bpy.context.view_layer.objects.active = first_obj

            if vtx_group_name in first_obj.vertex_groups:
                print("Adding Data Transfer to Mesh - ", first_obj.name)

                # check if a modifier exists, then add a Data Transfer modifier using Custom Normals from the Outer mesh
                # if first_obj.modifiers:
                #     print(first_obj.name," - Already has a Modifier")

                # else:
                data_transfer_mod = first_obj.modifiers.new(
                    "Data Transfer", 'DATA_TRANSFER')
                data_transfer_mod.object = active_obj
                data_transfer_mod.mix_mode = 'REPLACE'
                data_transfer_mod.mix_factor = 1
                data_transfer_mod.vertex_group = vtx_group_name
                data_transfer_mod.use_loop_data = True
                data_transfer_mod.data_types_loops = {'CUSTOM_NORMAL'}
                data_transfer_mod.loop_mapping = 'NEAREST_POLYNOR'

            else:
                bpy.context.window_manager.popup_menu(lambda self, context: self.layout.label(
                    text="There are No Vertex Groups to Transfer Normals"), title="Warning", icon='ERROR')
        else:
            bpy.context.window_manager.popup_menu(lambda self, context: self.layout.label(
                text="You Must Select 2 Objects in Object Mode"), title="Warning", icon='ERROR')

        return {'FINISHED'}


class WM_OT_vertexpaint(bpy.types.Operator):
    """Custom Vertex Paint Menu"""
    bl_label = "Paint Vertices Selected Color"
    bl_idname = "wm.vertexpaint"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        vertex_paint_type = opcdtools.vertex_paint_type
        paint_strength = opcdtools.paint_strength

        # Paint selected vertices according to selected Paint color
        color_to_vertices(vertex_paint_type, paint_strength)

        return {'FINISHED'}


class WM_OT_fillvertexpaint(bpy.types.Operator):
    """Custom Vertex Paint Menu"""
    bl_label = "Paint Interior Vertices Selected Color"
    bl_idname = "wm.fillvertexpaint"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        vertex_paint_type = opcdtools.vertex_paint_type
        paint_strength = opcdtools.paint_strength
        random_amt = 1.0 - opcdtools.random_amt
        loop_inset = opcdtools.paint_loop_inset

        selected = bpy.context.selected_objects

        counter = 1
        total = len(selected)
        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Painting -", ob.name, counter, " of ", total)

            mesh = (ob.data)

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')
            bpy.ops.mesh.select_all(action='SELECT')

            if loop_inset > 0:
                bpy.ops.mesh.region_to_loop()
                bpy.ops.mesh.select_all(action='INVERT')

                for i in range(loop_inset-1):
                    bpy.ops.mesh.select_less()

            if opcdtools.random_amt < 1.0:
                bpy.ops.mesh.select_random(
                    ratio=random_amt, seed=random.randint(1, 100), action='DESELECT')

            bm = bmesh.from_edit_mesh(mesh)

            color_to_vertices(vertex_paint_type, paint_strength)

            bpy.ops.object.mode_set(mode='OBJECT')
            counter = counter + 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_outervertexpaint(bpy.types.Operator):
    """Custom Vertex Paint Menu"""
    bl_label = "Paint Vertices Selected Color"
    bl_idname = "wm.outervertexpaint"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        counter = 1
        total = len(selected)
        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Painting -", ob.name, counter, " of ", total)

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            color_to_vertices('red', 1.0)

            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_more()
            bpy.ops.mesh.select_all(action='INVERT')
            # bpy.ops.mesh.select_less()

            # Paint selected vertices according to selected Paint color
            color_to_vertices('black', 1.0)

            bpy.ops.object.mode_set(mode='OBJECT')
            counter = counter + 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_randomvertexpaintloop(bpy.types.Operator):
    """Custom Vertex Paint Menu"""
    bl_label = "Paint Loop Randomly"
    bl_idname = "wm.randomvertexpaintloop"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        random_amt = 1.0 - opcdtools.random_amt
        vertex_paint_type = opcdtools.vertex_paint_type
        paint_strength = opcdtools.paint_strength
        loop_inset = opcdtools.paint_loop_inset
        skip_longest_loop = opcdtools.skip_longest_loop

        selected = bpy.context.selected_objects

        counter = 1
        total = len(selected)
        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Painting -", ob.name, counter, " of ", total)

            mesh = (ob.data)

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')
            bpy.ops.mesh.select_all(action='SELECT')

            if loop_inset > 0:
                bpy.ops.mesh.region_to_loop()
                bpy.ops.mesh.select_all(action='INVERT')

                for i in range(loop_inset-1):
                    bpy.ops.mesh.select_less()

                bpy.ops.mesh.region_to_loop()
            else:
                # for 0 inset, don't paint verts that are neighboring same material
                # this can (usually) be detected based on the spacing between verts and their neighbor
                bpy.ops.mesh.region_to_loop()
                bm = bmesh.from_edit_mesh(mesh)
                maxDist = 0
                for v in bm.verts:
                    if v.select:
                        minNeighborDist = float('inf')
                        linked_vertices = [edge.other_vert(
                            v) for edge in v.link_edges]
                        for vlinked in linked_vertices:
                            # local coords are fine
                            distCheck = vert_distance(vlinked.co, v.co)
                            if distCheck < minNeighborDist:
                                minNeighborDist = distCheck
                        # print(minNeighborDist, ', ')
                        if minNeighborDist > 0.75:
                            v.select = False

            if skip_longest_loop:
                # the usage is to paint the smallest loop, typically because there is a green inside
                # a fairway mesh with rough outside the fairway mesh (or similar situation) and we want to paint the inner
                # loop a different color than the outer. So if there is only one loop present, it is the outer loop
                # so don't paint that. This makes multiple selections work.
                select_internal_boundaries()

            if opcdtools.random_amt < 1.0:
                bpy.ops.mesh.select_random(
                    ratio=random_amt, seed=random.randint(1, 100), action='DESELECT')

            bm = bmesh.from_edit_mesh(mesh)

            color_to_vertices(vertex_paint_type, paint_strength)

            bmesh.update_edit_mesh(mesh)

            bm.clear

            bpy.ops.object.mode_set(mode='OBJECT')
            counter = counter + 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_randomvertexpaint_editmode(bpy.types.Operator):
    """Custom Vertex Paint Menu"""
    bl_label = "Paint Vertices Selected Color Randomly"
    bl_idname = "wm.randomvertexpaint_editmode"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        random_ratio = opcdtools.random_amt
        vertex_paint_type = opcdtools.vertex_paint_type
        paint_strength = opcdtools.paint_strength

        selected_objects = bpy.context.selected_objects  # Get all selected objects

        for ob in selected_objects:
            bpy.context.view_layer.objects.active = ob
            bm = bmesh.from_edit_mesh(ob.data)
            selected_vertices = [v for v in bm.verts if v.select]

            if selected_vertices:
                num_to_keep = int(len(selected_vertices) * random_ratio)
                vertices_to_keep = random.sample(
                    selected_vertices, num_to_keep)
                for v in selected_vertices:
                    v.select = False
                for v in vertices_to_keep:
                    v.select = True
                bmesh.update_edit_mesh(ob.data)

                color_to_vertices(vertex_paint_type, paint_strength)

        return {'FINISHED'}


class WM_OT_slopevertexpaint(bpy.types.Operator):
    """Slope Based Vertex Paint (select objects in Object mode)"""
    bl_label = "Paint Vertices on Slopes"
    bl_idname = "wm.slopevertexpaint"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        slope_min = opcdtools.slope_min
        slope_max = opcdtools.slope_max
        vertex_paint_type = opcdtools.vertex_paint_type
        paint_strength = opcdtools.paint_strength

        selected = bpy.context.selected_objects

        counter = 1
        total = len(selected)
        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Painting -", ob.name, counter, " of ", total)

            # Convert degrees to radians for angle comparison
            min_slope = math.radians(slope_min)
            max_slope = math.radians(slope_max)

            # # Get the active object (assuming it's the mesh you want to work with)
            # obj = bpy.context.active_object

            # Ensure the object is a mesh
            if ob and ob.type == 'MESH':
                mesh = ob.data

                bm = bmesh.new()
                bm.from_mesh(mesh)

                selected_verts = []  # List to store selected vertices

                # Deselect all vertices
                for vert in bm.verts:
                    vert.select = False

                # Iterate through vertices
                for vert in bm.verts:
                    normal = vert.normal

                    # Calculate the dot product between the normal and the world up vector (0, 0, 1)
                    dot_product = normal.dot((0, 0, 1))

                    # Calculate the slope angle using arccosine
                    slope_angle = 90 - math.degrees(math.acos(dot_product))

                    # Check if the slope angle is within the specified range
                    if slope_min <= slope_angle <= slope_max:
                        # Add the vertex to the list
                        selected_verts.append(vert)

                # Select the vertices from the list
                for vert in selected_verts:
                    vert.select = True

                bm.to_mesh(mesh)

                mesh.update

                bm.free()

                color_to_vertices(vertex_paint_type, paint_strength)

            counter = counter + 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_changecolors(bpy.types.Operator):
    """Custom Vertex Paint Menu"""
    bl_label = "Change Vertex Color"
    bl_idname = "wm.changecolors"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        vertex_paint_type_to = opcdtools.vertex_paint_type_to
        vertex_paint_type_from = opcdtools.vertex_paint_type_from

        selected = bpy.context.selected_objects

        colorFrom = (1, 0, 0)
        if vertex_paint_type_from == 'red':
            colorFrom = (1, 0, 0)
        elif vertex_paint_type_from == 'green':
            colorFrom = (0, 1, 0)
        elif vertex_paint_type_from == 'blue':
            colorFrom = (0, 0, 1)
        elif vertex_paint_type_from == 'black':
            colorFrom = (0, 0, 0)

        counter = 1
        total = len(selected)
        for ob in selected:
            mesh = ob.data

            print("Painting -", ob.name, counter, " of ", total)

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')

            bm = bmesh.from_edit_mesh(mesh)

            tris = bm.calc_loop_triangles()

            for name, cl in bm.loops.layers.color.items():
                for i, tri in enumerate(tris):
                    for loop in tri:
                        if vertex_paint_type_from == 'black':
                            if (loop[cl][0] + loop[cl][1] + loop[cl][2]) > 0.999:
                                loop.vert.select = False
                        elif (loop[cl][0] * colorFrom[0]) + (loop[cl][1] * colorFrom[1]) + (loop[cl][2] * colorFrom[2]) == 0:
                            loop.vert.select = False

            # keep behavior simple, otherwise color mixing is confusing
            color_to_vertices(vertex_paint_type_to, 1.0)

            bmesh.update_edit_mesh(mesh)

            bm.clear

            bpy.ops.object.mode_set(mode='OBJECT')
            counter = counter + 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_swapcolors(bpy.types.Operator):
    """Custom Vertex Paint Menu"""
    bl_label = "Swap Vertex Colors"
    bl_idname = "wm.swapcolors"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        vertex_paint_type_to = opcdtools.vertex_paint_type_to
        vertex_paint_type_from = opcdtools.vertex_paint_type_from

        selected = bpy.context.selected_objects

        colorFrom = (1, 0, 0)
        if vertex_paint_type_from == 'red':
            colorFrom = (1, 0, 0)
        elif vertex_paint_type_from == 'green':
            colorFrom = (0, 1, 0)
        elif vertex_paint_type_from == 'blue':
            colorFrom = (0, 0, 1)
        elif vertex_paint_type_from == 'black':
            colorFrom = (0, 0, 0)

        colorTo = (1, 0, 0)
        if vertex_paint_type_to == 'red':
            colorTo = (1, 0, 0)
        elif vertex_paint_type_to == 'green':
            colorTo = (0, 1, 0)
        elif vertex_paint_type_to == 'blue':
            colorTo = (0, 0, 1)
        elif vertex_paint_type_to == 'black':
            colorTo = (0, 0, 0)

        counter = 1
        total = len(selected)
        for ob in selected:
            mesh = ob.data

            print("Painting -", ob.name, counter, " of ", total)

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bm = bmesh.from_edit_mesh(mesh)
            tris = bm.calc_loop_triangles()
            for name, cl in bm.loops.layers.color.items():
                for i, tri in enumerate(tris):
                    for loop in tri:
                        if vertex_paint_type_from == 'black':
                            if (loop[cl][0] + loop[cl][1] + loop[cl][2]) > 0.999:
                                loop.vert.select = False
                        elif (loop[cl][0] * colorFrom[0]) + (loop[cl][1] * colorFrom[1]) + (loop[cl][2] * colorFrom[2]) == 0:
                            loop.vert.select = False
            color_to_vertices('white', 1.0)  # mark with white temporarily
            bmesh.update_edit_mesh(mesh)
            bm.clear

            bpy.ops.mesh.select_all(action='SELECT')
            bm = bmesh.from_edit_mesh(mesh)
            tris = bm.calc_loop_triangles()
            for name, cl in bm.loops.layers.color.items():
                for i, tri in enumerate(tris):
                    for loop in tri:
                        if (loop[cl][0] + loop[cl][1] + loop[cl][2]) > 2.9:
                            loop.vert.select = False  # skip the temporary white vertices
                        elif vertex_paint_type_to == 'black':
                            if (loop[cl][0] + loop[cl][1] + loop[cl][2]) == 1:
                                loop.vert.select = False
                        elif (loop[cl][0] * colorTo[0]) + (loop[cl][1] * colorTo[1]) + (loop[cl][2] * colorTo[2]) == 0:
                            loop.vert.select = False
            color_to_vertices(vertex_paint_type_from, 1.0)
            bmesh.update_edit_mesh(mesh)
            bm.clear

            bpy.ops.mesh.select_all(action='SELECT')
            bm = bmesh.from_edit_mesh(mesh)
            tris = bm.calc_loop_triangles()
            for name, cl in bm.loops.layers.color.items():
                for i, tri in enumerate(tris):
                    for loop in tri:
                        if (loop[cl][0] + loop[cl][1] + loop[cl][2]) < 2.9:
                            loop.vert.select = False
            color_to_vertices(vertex_paint_type_to, 1.0)
            bmesh.update_edit_mesh(mesh)
            bm.clear

            bpy.ops.object.mode_set(mode='OBJECT')
            counter = counter + 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


def select_furthest_vertices_in_loops():
    if bpy.context.object.mode != 'EDIT':
        bpy.ops.object.mode_set(mode='EDIT')

    obj = bpy.context.object
    bm = bmesh.from_edit_mesh(obj.data)

    selected_verts = [v for v in bm.verts if v.select]

    if not selected_verts:
        print("No vertices selected.")
        return

    def find_connected_loop(start_vert, visited):
        loop = []
        stack = [start_vert]
        while stack:
            v = stack.pop()
            if v not in visited:
                visited.add(v)
                loop.append(v)
                stack.extend([e.other_vert(v)
                             for e in v.link_edges if e.other_vert(v).select])
        return loop

    visited = set()
    loops = []
    for vert in selected_verts:
        if vert not in visited:
            loop = find_connected_loop(vert, visited)
            if len(loop) > 1:
                loops.append(loop)

    for loop in loops:
        max_distance = 0
        vertex_pair = None

        for i, v1 in enumerate(loop):
            for v2 in loop[i+1:]:
                distance = (v1.co - v2.co).length
                if distance > max_distance:
                    max_distance = distance
                    vertex_pair = (v1, v2)

        for v in loop:
            v.select = False

        if vertex_pair:
            vertex_pair[0].select = True
            vertex_pair[1].select = True

    bmesh.update_edit_mesh(obj.data)


class WM_OT_growcolor(bpy.types.Operator):
    """Custom Vertex Paint Menu"""
    bl_label = "Grow Vertex Color"
    bl_idname = "wm.growcolor"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        random_amt = 1.0 - opcdtools.random_amt
        vertex_paint_type = opcdtools.vertex_paint_type
        paint_strength = opcdtools.paint_strength
        grow_repeat = opcdtools.grow_repeat
        grow_mode = opcdtools.grow_mode
        grow_strict = opcdtools.grow_strict

        selected = bpy.context.selected_objects

        colorFrom = (1, 0, 0)
        if vertex_paint_type == 'red':
            colorFrom = (1, 0, 0)
        elif vertex_paint_type == 'green':
            colorFrom = (0, 1, 0)
        elif vertex_paint_type == 'blue':
            colorFrom = (0, 0, 1)
        elif vertex_paint_type == 'black':
            colorFrom = (0, 0, 0)

        counter = 1
        total = len(selected)
        for ob in selected:
            mesh = ob.data

            print("Painting -", ob.name, counter, " of ", total)

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            for i in range(grow_repeat):
                bpy.ops.mesh.select_all(action='SELECT')
                bm = bmesh.from_edit_mesh(mesh)

                tris = bm.calc_loop_triangles()

                for name, cl in bm.loops.layers.color.items():
                    for i, tri in enumerate(tris):
                        for loop in tri:
                            if grow_strict:
                                if (loop[cl][0] != colorFrom[0] or loop[cl][1] != colorFrom[1] or loop[cl][2] != colorFrom[2]):
                                    loop.vert.select = False
                            elif vertex_paint_type == 'black':
                                if (loop[cl][0] + loop[cl][1] + loop[cl][2]) > 0.999:
                                    loop.vert.select = False
                            elif (loop[cl][0] * colorFrom[0]) + (loop[cl][1] * colorFrom[1]) + (loop[cl][2] * colorFrom[2]) == 0:
                                loop.vert.select = False
                bmesh.update_edit_mesh(mesh)
                bpy.ops.mesh.select_more()
                bpy.ops.mesh.select_more()  # why must I call this twice? Idk but it works

                if grow_mode == 'EDGEONLY' or grow_mode == 'LINEAR':
                    bpy.ops.mesh.region_to_loop()

                if grow_mode == 'LINEAR':
                    select_furthest_vertices_in_loops()
                elif opcdtools.random_amt < 1.0:
                    bpy.ops.mesh.select_random(
                        ratio=random_amt, seed=random.randint(1, 100), action='DESELECT')

                color_to_vertices(vertex_paint_type, paint_strength)

                bmesh.update_edit_mesh(mesh)

                bm.clear

            bpy.ops.object.mode_set(mode='OBJECT')
            counter = counter + 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_MeshBlendJoinPermanent(bpy.types.Operator):
    """Join a mesh with the alphanumerically last object containing its 'Blend' name"""
    bl_label = "Join Mesh with Last Blend Permanently"
    bl_idname = "wm.meshblendjoinpermanent"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        selected_at_start = [
            obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_at_start:
            self.report({'INFO'}, "No mesh objects selected.")
            return {'CANCELLED'}

        base_name_list = []

        for selected_obj in selected_at_start:
            if not selected_obj.name.endswith("Mesh"):
                self.report(
                    {'INFO'}, f"Skipping '{selected_obj.name}' as it does not end with 'Mesh'.")
                continue

            base_name = selected_obj.name[:-4]
            base_name_list.append(base_name)

            blend_objects_to_join = opcd_find_blends_for_mesh(selected_obj)

            if blend_objects_to_join:
                self.report(
                    {'INFO'}, f"Found {len(blend_objects_to_join)} blend match(es). Joining all into '{selected_obj.name}'.")

                bpy.ops.object.select_all(action='DESELECT')
                selected_obj.select_set(True)
                for b in blend_objects_to_join:
                    b.select_set(True)

                context.view_layer.objects.active = selected_obj
                bpy.ops.object.join()

                bpy.ops.object.editmode_toggle()
                bpy.ops.mesh.select_mode(type="VERT")
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.remove_doubles(threshold=0.01)
                bpy.ops.object.editmode_toggle()

                bpy.ops.object.mode_set(mode='OBJECT')
                if len(selected_obj.material_slots) > 1:
                    selected_obj.active_material_index = 1
                    bpy.ops.object.material_slot_remove()

            else:
                self.report(
                    {'WARNING'}, f"No blend objects found for {selected_obj.name} (missing metadata or unexpected naming).")

        # Reselect the final joined objects
        bpy.ops.object.select_all(action='DESELECT')
        for ob in scene.objects:
            for base_name in base_name_list:
                if ob.name.startswith(base_name + "Mesh"):
                    ob.select_set(True)
                    break

        return {'FINISHED'}


class WM_OT_MeshBlendJoin(bpy.types.Operator):
    """Join a mesh with the alphanumerically last object containing its 'Blend' name"""
    bl_label = "Join Mesh with Last Blend"
    bl_idname = "wm.meshblendjoin"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene

        selected_at_start = [
            obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_at_start:
            self.report({'INFO'}, "No mesh objects selected.")
            return {'CANCELLED'}

        base_name_list = []

        for selected_obj in selected_at_start:
            if not selected_obj.name.endswith("Mesh"):
                self.report(
                    {'INFO'}, f"Skipping '{selected_obj.name}' as it does not end with 'Mesh'.")
                continue

            base_name = selected_obj.name[:-4]
            base_name_list.append(base_name)

            # Use robust pairing (metadata if present; name-based fallback otherwise).
            blend_objects_to_join = opcd_find_blends_for_mesh(selected_obj)
            if not blend_objects_to_join:
                self.report(
                    {'WARNING'}, f"No blend objects found for {selected_obj.name} (missing metadata or unexpected naming).")
                continue

            # Preserve historical behavior: join the *last* blend in alphanumeric order.
            blend_obj_to_join = blend_objects_to_join[-1]
            self.report(
                {'INFO'}, f"Found {len(blend_objects_to_join)} match(es). Joining with '{blend_obj_to_join.name}'.")

            bpy.ops.object.select_all(action='DESELECT')
            selected_obj.select_set(True)
            blend_obj_to_join.select_set(True)
            context.view_layer.objects.active = selected_obj

            bpy.ops.object.join()

            bpy.ops.object.editmode_toggle()
            bpy.ops.mesh.select_mode(type="VERT")
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold=0.01)
            bpy.ops.object.editmode_toggle()

        # Reselect the final joined objects
        bpy.ops.object.select_all(action='DESELECT')
        for ob in scene.objects:
            for base_name in base_name_list:
                if ob.name.startswith(base_name + "Mesh"):
                    ob.select_set(True)
                    break

        return {'FINISHED'}


class WM_OT_MeshBlendSeparate(bpy.types.Operator):
    """Separate the joined mesh and blend"""
    bl_label = "Separate the joined mesh and blend"
    bl_idname = "wm.meshblendseparate"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene

        selected = [
            o for o in context.selected_objects if o and o.type == 'MESH']
        if not selected:
            self.report({'INFO'}, "Please select a valid joined mesh.")
            return {'CANCELLED'}

        def is_blend_material_obj(obj):
            try:
                mats = [m.name for m in obj.data.materials if m is not None]
            except Exception:
                mats = []
            # Pipeline convention: blend materials typically contain "Blend" (often " - Blend").
            return any("Blend" in mn for mn in mats)

        def vcount(obj):
            try:
                return len(obj.data.vertices)
            except Exception:
                return 0

        base_name_list = []

        for joined_obj in selected:
            # Blender ops are context-sensitive: operate one active object at a time.
            bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            joined_obj.select_set(True)
            context.view_layer.objects.active = joined_obj

            name_before = joined_obj.name
            base_name = name_before[:-
                                    4] if name_before.endswith("Mesh") else name_before
            base_name_list.append(base_name)

            # Separate into one object per material.
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.separate(type='MATERIAL')
            bpy.ops.object.mode_set(mode='OBJECT')

            separated_objs = [
                o for o in context.selected_objects if o and o.type == 'MESH']
            if not separated_objs:
                self.report(
                    {'WARNING'}, f"Separate produced no objects for {name_before}.")
                continue

            blend_parts = [
                o for o in separated_objs if is_blend_material_obj(o)]
            mesh_parts = [o for o in separated_objs if o not in blend_parts]

            if not mesh_parts:
                self.report(
                    {'WARNING'}, f"Could not identify a non-blend mesh part for {name_before}; leaving names unchanged.")
                continue

            # Choose the main mesh as the non-blend part with the most vertices.
            primary_mesh = max(mesh_parts, key=vcount)
            primary_mesh.name = base_name + "Mesh"

            # If there are other non-blend materials, keep them but make them explicit.
            extra_mesh = [o for o in mesh_parts if o != primary_mesh]
            extra_mesh.sort(key=lambda o: o.name)
            for i, o in enumerate(extra_mesh, start=1):
                o.name = f"{base_name}Mesh_part{i:03d}"

            # Name blend parts deterministically. If multiple blends exist (split blends),
            # use Blender's numeric style: Blend, Blend.001, Blend.002, ...
            blend_parts.sort(key=lambda o: o.name)
            if len(blend_parts) == 1:
                blend_parts[0].name = base_name + "Blend"
            elif len(blend_parts) > 1:
                for i, o in enumerate(blend_parts):
                    suffix = "" if i == 0 else f".{i:03d}"
                    o.name = base_name + "Blend" + suffix

            print(
                f"Separated {name_before} into {len(mesh_parts)} mesh part(s) and {len(blend_parts)} blend part(s).")

        # Reselect the mesh objects for convenience.
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in scene.objects:
            for base_name in base_name_list:
                if ob.name.startswith(base_name + "Mesh"):
                    ob.select_set(True)
                    break

        return {'FINISHED'}


class WM_OT_separateblend(bpy.types.Operator):
    """Separate Blends For Different Destination Mesh Types"""
    bl_label = "Select one blend and up to 3 meshes"
    bl_idname = "wm.separateblend"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        paint_strength = opcdtools.paint_strength

        selected = bpy.context.selected_objects

        assert len(selected) <= 4, "Select one blend and up to 3 meshes"

        desintationObjs = []
        desintationObjsUnordered = []
        for ob in selected:
            if 'Blend' in ob.name:
                blendObject = ob
            else:
                desintationObjsUnordered.append(ob)

        intMaterialTypes = (
            ('Tee', 'Tee', ''),
            ('Fairway', 'Fairway', ''),
            ('Semi', 'Semi', ''),
            ('Green', 'Green', ''),
            ('Rough', 'Rough', ''),
            ('Deep', 'Deep', ''),
            ('Pinestraw', 'Pinestraw', ''),
            ('Bunker', 'Bunker', ''),
            ('Concrete', 'Concrete', ''),
            ('Water_Base_Lake', 'Water_Base_Lake', ''),
            ('Water_Base_Creek', 'Water_Base_Creek', ''),
            ('Custom1', 'Custom1', ''),
            ('Custom2', 'Custom2', ''),
            ('Custom3', 'Custom3', ''),
            ('Custom4', 'Custom4', ''),
            # ('Hole99', 'Hole99',''),
        )

        # order them for consistent painting
        for matType in intMaterialTypes:
            for ob in desintationObjsUnordered:
                if matType[1] in ob.name:
                    desintationObjs.append(ob)

        assert len(desintationObjs) <= 3, "Select one blend"
        assert len(desintationObjs) == len(selected)-1, "Only select one blend"

        # first find the exterior of the blend,
        # identify by number of groups the vertex is in
        mesh = blendObject.data

        bpy.ops.object.select_all(action='DESELECT')
        blendObject.select_set(True)
        bpy.context.view_layer.objects.active = blendObject

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')

        # assemble list of vertex indices that belong to a group
        # the exterior of the blend only belongs to a group, don't know why
        verticesIndices = []
        verts = [v for v in mesh.vertices if len(v.groups) > 0]

        for v in verts:
            verticesIndices.append(v.index)

        # now get the verts at those indices
        vertsCoordsExterior = []
        vertsIndicesExterior = []
        bm = bmesh.from_edit_mesh(mesh)
        verts = [v for v in bm.verts]
        bpy.ops.mesh.select_all(action='DESELECT')
        for v in verts:
            if (v.index in verticesIndices):
                vertsCoordsExterior.append(blendObject.matrix_world @ v.co)
                vertsIndicesExterior.append(v.index)
                v.select = True

        total = len(vertsCoordsExterior)
        # now find which mesh the outer blend verts are bordering
        for i in range(len(vertsCoordsExterior)):
            vCoord = vertsCoordsExterior[i]
            print("Painting -", blendObject.name, i, " of ", total)
            objectIndex = 0
            minDist = float('inf')
            bpy.ops.object.mode_set(mode='OBJECT')
            for ob in desintationObjs:
                mesh = ob.data

                bpy.ops.object.select_all(action='DESELECT')
                ob.select_set(True)
                bpy.context.view_layer.objects.active = ob

                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')

                bm = bmesh.from_edit_mesh(mesh)

                for v1 in bm.verts:
                    distCheck = vert_distance(vCoord, ob.matrix_world @ v1.co)
                    if distCheck < minDist:
                        minDist = distCheck
                        nearestObIndex = objectIndex

                objectIndex = objectIndex + 1
                bpy.ops.object.mode_set(mode='OBJECT')

            if (nearestObIndex == 0):
                vertex_paint_type_to = 'black'
            elif (nearestObIndex == 1):
                vertex_paint_type_to = 'blue'
            elif (nearestObIndex == 2):
                vertex_paint_type_to = 'green'
            else:
                vertex_paint_type_to = 'red'  # this shouldn't happen but is useful for debugging

            bpy.ops.object.mode_set(mode='OBJECT')
            mesh = blendObject.data
            bpy.ops.object.select_all(action='DESELECT')
            blendObject.select_set(True)
            bpy.context.view_layer.objects.active = blendObject
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bm = bmesh.from_edit_mesh(mesh)
            verts = [v for v in bm.verts]
            # this is all ineffecient... improve later
            for v in verts:
                if (v.index == vertsIndicesExterior[i]):
                    v.select = True
                    break

            # it would be more efficient to call this once after marking all the colors,
            # but this is easier to write, improve it in the next version...
            color_to_vertices(vertex_paint_type_to, 1.0)

        bpy.ops.object.mode_set(mode='OBJECT')

        return {'FINISHED'}


class WM_OT_levelmesh(bpy.types.Operator):
    """Level selected Mesh to Lowest Vertice"""
    bl_label = "Level Mesh"
    bl_idname = "wm.levelmesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = [
            o for o in bpy.context.selected_objects if 'Mesh' in o.name and 'Spline' in o.name]

        for o in selected:
            bpy.ops.object.select_all(action='DESELECT')
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            flatten_base(o)

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


def smooth_mesh_interior(smooth_amount, smooth_repeat, smooth_interior_inset, smooth_interior_3d=False):
    """Smooth Mesh Interior Execution"""
    selected = [
        o for o in bpy.context.selected_objects if 'Mesh' in o.name and 'Spline' in o.name]

    counter = 1
    total = len(selected)

    # Mesh smoothing
    for ob in selected:

        bpy.ops.object.select_all(action='DESELECT')
        ob.select_set(True)
        bpy.context.view_layer.objects.active = ob

        print("Smoothing -", ob.name, counter, " of ", total)

        # Check specifically for the "Project" modifier and ensure it's enabled
        for mod in ob.modifiers:
            if mod.name == "Project":
                # Ensure the modifier is enabled before applying
                mod.show_viewport = True
                mod.show_render = True
                try:
                    bpy.ops.object.modifier_apply(modifier="Project")
                except:
                    print(
                        f"Warning: Could not apply Project modifier to {ob.name}")
                break

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(
            use_extend=False, use_expand=False, type='VERT')
        bpy.ops.mesh.select_all(action='SELECT')

        if smooth_interior_inset > 0:
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')
            for i in range(smooth_interior_inset-1):
                bpy.ops.mesh.select_less()

        bpy.ops.mesh.vertices_smooth(factor=smooth_amount, repeat=smooth_repeat,
                                     xaxis=smooth_interior_3d, yaxis=smooth_interior_3d, zaxis=True, wait_for_input=False)

        bpy.ops.object.mode_set(mode='OBJECT')

        counter += 1

    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    for ob in selected:
        ob.select_set(True)


class WM_OT_smoothmesh(bpy.types.Operator):
    """Smooth Mesh Interior"""
    bl_label = "Smooth Mesh Interior"
    bl_idname = "wm.smoothmesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        smooth_amount = opcdtools.smooth_interior
        smooth_repeat = opcdtools.smooth_interior_repeat
        smooth_interior_inset = opcdtools.smooth_interior_inset
        smooth_interior_3d = opcdtools.smooth_interior_3d

        smooth_mesh_interior(smooth_amount, smooth_repeat,
                             smooth_interior_inset, smooth_interior_3d)

        return {'FINISHED'}


class WM_OT_subdividemesh(bpy.types.Operator):
    """Subdivide Mesh Interior"""
    bl_label = "Subdivide Mesh Interior"
    bl_idname = "wm.subdividemesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        subdivide_inset = opcdtools.subdivide_inset

        selected = [
            o for o in bpy.context.selected_objects if 'Mesh' in o.name and 'Spline' in o.name]

        counter = 1
        total = len(selected)

        for ob in selected:

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Subdividing -", ob.name, counter, " of ", total)

            # Check specifically for the "Project" modifier and ensure it's enabled
            for mod in ob.modifiers:
                if mod.name == "Project":
                    # Ensure the modifier is enabled before applying
                    mod.show_viewport = True
                    mod.show_render = True
                    try:
                        bpy.ops.object.modifier_apply(modifier="Project")
                    except:
                        print(
                            f"Warning: Could not apply Project modifier to {ob.name}")
                    break

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='EDGE')
            bpy.ops.mesh.select_all(action='SELECT')

            if subdivide_inset > 0:
                bpy.ops.mesh.region_to_loop()
                bpy.ops.mesh.select_all(action='INVERT')
                for i in range(subdivide_inset-1):
                    bpy.ops.mesh.select_less()

            bpy.ops.mesh.subdivide(
                number_cuts=1,              # Number of cuts to make
                smoothness=0.0,             # Smoothness factor
                quadcorner='STRAIGHT_CUT'     # Corner type for quad/n-gon faces
            )

            bpy.ops.object.mode_set(mode='OBJECT')

            counter += 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


def build_kdtree_from_coords(coords):
    """
    Build a KDTree from a list of coordinates, ignoring the z value during comparison.

    Args:
        coords (list of Vector): List of world-space coordinates.

    Returns:
        KDTree: The KDTree for the coordinates.
    """
    kd = KDTree(len(coords))

    for i, coord in enumerate(coords):
        # Create a 3D vector where Z is set to 0 for comparison
        coord_3d = Vector((coord.x, coord.y, 0))  # Use Z = 0 for comparison
        kd.insert(coord_3d, i)  # Insert the 3D vector (Z = 0) into the KDTree

    kd.balance()
    return kd


def find_closest_vert(selected_coords, vertex):
    """
    Finds the closest coordinate in selected_coords (ignoring Z for comparison) to a given vertex.

    Args:
        selected_coords (list of Vector): List of coordinates to search.
        vertex (bpy.types.MeshVertex): The vertex for which to find the closest coordinate.

    Returns:
        Vector: The closest 3D coordinate (including Z value).
    """
    # Build KDTree from selected_coords (Z value ignored during comparison)
    kd = build_kdtree_from_coords(selected_coords)

    # Create a 3D vector for the vertex (ignoring Z for comparison)
    vertex_3d = Vector((vertex.co.x, vertex.co.y, 0)
                       )  # Set Z = 0 for comparison

    # Find the closest coordinate (ignoring Z for comparison)
    # This is the closest 2D coordinate found
    closest_coord_2d = kd.find(vertex_3d)[0]

    # Now find the index of the closest 2D coordinate in selected_coords
    # We search for the closest 2D vector in selected_coords based on x, y, and z information.
    closest_coord = None
    min_distance = float('inf')

    for coord in selected_coords:
        # Calculate the Euclidean distance in 2D (ignoring Z)
        dist = (coord.x - closest_coord_2d.x) ** 2 + \
            (coord.y - closest_coord_2d.y) ** 2
        if dist < min_distance:
            min_distance = dist
            closest_coord = coord

    return closest_coord


def smooth_falloff(t):
    """Smooth falloff curve: starts at 1 and smoothly decreases to 0."""
    return 1 - (3 * t**2 - 2 * t**3) if 0 <= t <= 1 else 0


def linear_falloff(t):
    """Linear falloff: uniform decrease."""
    return max(0, 1 - t)


def collect_selected_vertices_world(active_obj):
    """Collects the world coordinates of all selected vertices in the active object."""
    bpy.ops.object.mode_set(mode='EDIT')  # Ensure Edit Mode
    bm = bmesh.from_edit_mesh(active_obj.data)
    bm.verts.ensure_lookup_table()

    selected_vertices = [active_obj.matrix_world @
                         v.co for v in bm.verts if v.select]

    return selected_vertices


def build_kdtree_for_object_2d(obj):
    """Builds a KDTree for the given mesh object in *world space*, 
    with Z=0 so that lookups ignore the Z dimension."""
    mesh = obj.data
    kd = KDTree(len(mesh.vertices))

    for i, vert in enumerate(mesh.vertices):
        # Convert to world space
        co_3d = obj.matrix_world @ vert.co
        # Zero out Z so we effectively ignore it
        co_2d = Vector((co_3d.x, co_3d.y, 0.0))
        kd.insert(co_2d, i)

    kd.balance()
    return kd


def find_nearby_vertices(selected_coords, radius):
    """
    Finds all vertices in all mesh objects within the radius of any selected vertex,
    ignoring the Z dimension (search is effectively in 2D).

    Returns:
        A dictionary where keys are (object, vertex_index) tuples and values
        are the minimum distance found.
    """
    nearby_vertices = {}

    # Iterate through all mesh objects in the scene
    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH':
            continue
        if 'Spline' not in obj.name:
            continue
        if obj == bpy.context.active_object:
            continue  # Optionally skip the active object

        kd = build_kdtree_for_object_2d(obj)

        # Check each selected coordinate
        for center_co_3d in selected_coords:
            # Zero out Z to match how we built the KDTree
            center_co_2d = Vector((center_co_3d.x, center_co_3d.y, 0.0))

            # Find all vertices in the radius (using 2D distance)
            matches = kd.find_range(center_co_2d, radius)

            for match in matches:
                vert_co, index, dist = match  # dist is the 2D distance ignoring Z
                key = (obj, index)

                # If this vertex hasn't been seen or we found a closer center
                if key not in nearby_vertices or dist < nearby_vertices[key]:
                    nearby_vertices[key] = dist

    return nearby_vertices


def apply_proportional_z_move(selected_coords, nearby_vertices, radius, falloff_function):
    """
    Applies a proportional Z move to the collected nearby vertices.

    Args:
        nearby_vertices (dict): Dictionary with keys as (object, vertex index) and values as distances.
        radius (float): Radius of influence.
        falloff_function (callable): Function to calculate influence based on normalized distance.
    """
    # Group vertices by object to minimize mode switching
    obj_to_vertices = {}
    for (obj, index), dist in nearby_vertices.items():
        if obj not in obj_to_vertices:
            obj_to_vertices[obj] = []
        obj_to_vertices[obj].append((index, dist))

    # Store the current active object and mode
    original_active_obj = bpy.context.view_layer.objects.active
    original_mode = bpy.context.mode

    countA = 0
    # Iterate through each object and apply transformations
    for obj, verts in obj_to_vertices.items():
        print("countA = ", countA)
        countA += 1

        # More robust checking if object is valid and can be made active
        if obj.name not in bpy.context.view_layer.objects:
            print(f"Object {obj.name} is not in the view layer.")
            continue

        # Check if object is valid in the scene
        if obj.name not in bpy.data.objects:
            print(f"Object {obj.name} is not in the scene data.")
            continue

        # Check if object is visible and not hidden
        view_layer_obj = bpy.context.view_layer.objects.get(obj.name)
        if not view_layer_obj:
            print(f"Object {obj.name} not accessible in view layer.")
            continue

        print(f"Object {obj.name} works")

        try:
            # First ensure we're in object mode before switching active object
            if bpy.context.mode != 'OBJECT':
                if bpy.context.active_object:
                    bpy.ops.object.mode_set(mode='OBJECT')

            # Switch to the target object
            bpy.context.view_layer.objects.active = obj

            # Double-check the active object was set correctly
            if bpy.context.active_object != obj:
                print(f"Failed to set {obj.name} as active object.")
                continue

            # Now switch to object mode for this object
            if bpy.context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')

            mesh = obj.data
            for index, dist in verts:
                if dist > radius:
                    continue  # Just in case
                closest = find_closest_vert(
                    selected_coords, mesh.vertices[index])
                z_offset = closest.z - mesh.vertices[index].co.z
                x1, y1 = closest.x, closest.y
                x2, y2 = mesh.vertices[index].co.x, mesh.vertices[index].co.y
                distXY = ((x2 - x1)**2 + (y2 - y1)**2)**0.5
                weight = falloff_function(distXY / radius)
                mesh.vertices[index].co.z += z_offset * weight

            # Update the mesh to reflect changes
            mesh.update()

        except Exception as e:
            print(f"Error processing object {obj.name}: {e}")
            continue

    # Restore the original active object and mode
    try:
        if original_active_obj and original_active_obj.name in bpy.context.view_layer.objects:
            bpy.context.view_layer.objects.active = original_active_obj
            if original_mode == 'EDIT_MESH':
                bpy.ops.object.mode_set(mode='EDIT')
    except Exception as e:
        print(f"Error restoring original context: {e}")


def smooth_outside(radius):
    # Parameters
    # Choose the falloff function: smooth_falloff or linear_falloff
    falloff = smooth_falloff

    # Get the active object
    active_obj = bpy.context.active_object
    if not active_obj or active_obj.type != 'MESH':
        print("Active object is not a mesh.")
        return

    # Collect selected vertices in the active object
    selected_coords = collect_selected_vertices_world(active_obj)
    if not selected_coords:
        print("No vertices selected in the active object.")
        return

    # Find all nearby vertices across the scene
    nearby_vertices = find_nearby_vertices(selected_coords, radius)
    if not nearby_vertices:
        print("No nearby vertices found within the specified radius.")
        return

    # Apply proportional Z move to the nearby vertices
    apply_proportional_z_move(
        selected_coords, nearby_vertices, radius, falloff)

    print(
        f"Applied proportional Z move to {len(nearby_vertices)} vertices within {radius} units.")


class WM_OT_smoothcartpaths(bpy.types.Operator):
    """Smooth Cart Paths"""
    bl_label = "Smooth Cart Paths"
    bl_idname = "wm.smoothcartpaths"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        smooth_path_distance = opcdtools.smooth_path_distance
        smooth_path_amt = opcdtools.smooth_path_amt

        selected = [o for o in bpy.context.selected_objects if (
            'Spline' in o.name) and not ('Blend' in o.name)]

        smooth_mesh_interior(1.0, smooth_path_amt, 0)

        counter = 1
        total = len(selected)

        # Mesh smoothing
        for ob in selected:
            bpy.context.view_layer.objects.active = ob

            # find the boundary loop vertices of mesh
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(type='VERT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bm = bmesh.from_edit_mesh(ob.data)
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')
            for v in bm.verts:
                v.select = True
            for edge in bm.edges:
                edge.select = True
            for face in bm.faces:
                face.select = True
            bmesh.update_edit_mesh(ob.data)
            bpy.ops.mesh.region_to_loop()

            smooth_outside(smooth_path_distance)

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


def knife_cut_and_color_bmesh(obj, bm_passed, keep_both_sides=True, color_name='blue', color_strength=1.0):
    """
    Performs a knife cut using the passed BMesh with a VERTICAL plane,
    LIMITED to geometry near the cut line.
    """
    if obj is None or obj.type != 'MESH':
        print("Error (knife_cut_bmesh): No active mesh object, or not a mesh.")
        return None

    selected_verts_for_cut_definition = [
        v for v in bm_passed.verts if v.select]

    if len(selected_verts_for_cut_definition) != 2:
        print(
            f"Error (knife_cut_bmesh): Expected 2 selected verts, found {len(selected_verts_for_cut_definition)}.")
        return None

    vert1_def = selected_verts_for_cut_definition[0]
    vert2_def = selected_verts_for_cut_definition[1]

    if not vert1_def.is_valid or not vert2_def.is_valid:
        print("Error (knife_cut_bmesh): Defining vertices are no longer valid.")
        return None

    plane_co_local = vert1_def.co.copy()
    cut_vector = (vert2_def.co - vert1_def.co)

    if cut_vector.length < 0.00001:
        print("Error (knife_cut_bmesh): Cut direction is zero.")
        return None

    # Calculate plane normal (horizontal, perpendicular to cut)
    up_vector = Vector((0.0, 0.0, 1.0))
    plane_no_local = up_vector.cross(cut_vector).normalized()

    if plane_no_local.length < 0.01:
        # Cut is vertical - use the cut direction projected onto XY plane
        cut_xy = Vector((cut_vector.x, cut_vector.y, 0.0))
        if cut_xy.length > 0.001:
            plane_no_local = cut_xy.normalized()
        else:
            plane_no_local = Vector((1.0, 0.0, 0.0))

    if plane_no_local.length < 0.01:
        print("Error (knife_cut_bmesh): Could not determine a valid plane normal.")
        return None

    # *** KEY FIX: Only cut geometry NEAR the line segment ***
    cut_midpoint = (vert1_def.co + vert2_def.co) / 2.0
    cut_length = cut_vector.length
    search_radius = cut_length * 0.6  # Buffer around the cut line

    # Filter geometry to only include elements near the cut
    nearby_verts = []
    nearby_edges = []
    nearby_faces = []

    for vert in bm_passed.verts:
        if (vert.co - cut_midpoint).length <= search_radius:
            nearby_verts.append(vert)

    # Include edges if both verts are nearby OR if edge intersects the cut region
    for edge in bm_passed.edges:
        if any(v in nearby_verts for v in edge.verts):
            nearby_edges.append(edge)

    # Include faces if any vert is nearby
    for face in bm_passed.faces:
        if any(v in nearby_verts for v in face.verts):
            nearby_faces.append(face)

    if not nearby_faces:
        print("Warning: No geometry found near cut line")
        return None

    geom_to_cut = nearby_verts + nearby_edges + nearby_faces

    # print(f"Cutting {len(nearby_faces)} faces near cut line (search radius: {search_radius:.2f})")

    # Deselect the defining vertices
    vert1_def.select_set(False)
    vert2_def.select_set(False)
    bm_passed.select_flush(False)

    newly_created_bmverts_from_cut = []

    # print(f"Attempting cut (bmesh) with plane_co: {plane_co_local}, plane_no: {plane_no_local}")

    try:
        result = bmesh.ops.bisect_plane(bm_passed,
                                        geom=geom_to_cut,  # *** Only nearby geometry ***
                                        plane_co=plane_co_local,
                                        plane_no=plane_no_local,
                                        clear_inner=not keep_both_sides,
                                        clear_outer=not keep_both_sides)

        if result and 'geom_cut' in result:
            for elem in result['geom_cut']:
                if isinstance(elem, bmesh.types.BMVert):
                    if elem.is_valid:
                        newly_created_bmverts_from_cut.append(elem)

            if newly_created_bmverts_from_cut:
                # print(f"Knife_cut_bmesh: Created {len(newly_created_bmverts_from_cut)} new verts.")

                # Deselect all, then select only new vertices
                for v_iter in bm_passed.verts:
                    v_iter.select_set(False)

                for v_new_cut in newly_created_bmverts_from_cut:
                    if v_new_cut.is_valid:
                        v_new_cut.select_set(True)
                bm_passed.select_flush(True)

                # Update mesh from BMesh
                me = obj.data
                bmesh.update_edit_mesh(me)

                # Use color_to_vertices which works in edit mode
                color_to_vertices(color_name, color_strength)

            else:
                print("Knife_cut_bmesh: No new vertices found in geom_cut.")
        else:
            print("Knife_cut_bmesh: Bisect_plane result did not contain 'geom_cut'.")

        # print("Knife cut (bmesh) operation successful.")
        return newly_created_bmverts_from_cut

    except RuntimeError as e:
        print(f"Error (knife_cut_bmesh) during bmesh.ops.bisect_plane: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred during bisect_plane (bmesh): {e}")
        return None


def get_ordered_boundary_loops(bm):
    """
    Finds all distinct, ordered boundary loops in a BMesh.
    Returns a list of lists, where each inner list contains BMVerts in loop order.
    """
    loops = []
    visited_verts = set()

    boundary_edges = [e for e in bm.edges if e.is_boundary]
    if not boundary_edges:
        return []

    # Create a lookup for quick access to an edge's other vertex
    adj = {v: [] for v in bm.verts}
    for e in boundary_edges:
        adj[e.verts[0]].append(e.verts[1])
        adj[e.verts[1]].append(e.verts[0])

    for v_start in bm.verts:
        if v_start not in visited_verts and v_start in adj and adj[v_start]:
            current_loop = []
            curr = v_start
            # Try to pick a consistent starting direction (if possible, not strictly needed for closed loop)
            # For simplicity, just pick the first available boundary neighbor
            prev = None  # Keep track of previous to avoid going back immediately on simple spurs

            queue = deque([(curr, None)])  # (vertex, previous_vertex_in_path)

            loop_path = {}  # To reconstruct path if needed, or just build list directly

            # Start traversal for a new loop
            path_found_for_curr_start = False

            # Find an unvisited boundary neighbor to start the walk
            initial_next_v = None
            for neighbor in adj[curr]:
                if neighbor not in visited_verts:  # Check global visited for starting edge
                    initial_next_v = neighbor
                    break

            if not initial_next_v and curr not in visited_verts:  # одиночный boundary vert
                if not adj[curr]:  # Truly isolated
                    continue

            # If curr itself is a boundary vert but all its boundary neighbors are visited
            # it might be the closing vert of a loop already found, or an unvisited boundary vert.
            # This logic needs to be robust for multiple disconnected loops.

            # Simplified loop walk:
            q = deque()
            # Find a boundary vert that hasn't been fully processed
            unprocessed_boundary_verts = [
                bv for bv in bm.verts if bv.is_boundary and bv not in visited_verts]
            if not unprocessed_boundary_verts:
                break  # All boundary verts processed

            v_start_loop = unprocessed_boundary_verts[0]

            # Start walking the loop
            current_v = v_start_loop
            ordered_loop = []

            # Find a starting edge
            start_edge = None
            for edge in current_v.link_edges:
                if edge.is_boundary:
                    start_edge = edge
                    break

            if not start_edge:  # Should not happen if v_start_loop is boundary
                # Mark as visited to avoid infinite loop
                visited_verts.add(current_v)
                continue

            # Walk the loop using the edges
            current_edge = start_edge

            # Determine initial direction
            if current_edge.verts[0] == current_v:
                next_v_in_loop = current_edge.verts[1]
            else:
                next_v_in_loop = current_edge.verts[0]

            for _i in range(len(bm.verts) + 1):  # Safety break
                if current_v in visited_verts and current_v != v_start_loop:  # Visited by another loop
                    # This loop segment might be invalid or part of another
                    ordered_loop = []  # Discard partial
                    break

                ordered_loop.append(current_v)
                visited_verts.add(current_v)

                found_next_edge = False
                for edge in next_v_in_loop.link_edges:
                    if edge.is_boundary and edge != current_edge:
                        current_edge = edge
                        current_v = next_v_in_loop  # old next_v_in_loop becomes current_v

                        # Determine the new next_v_in_loop from current_edge
                        if current_edge.verts[0] == current_v:
                            next_v_in_loop = current_edge.verts[1]
                        else:
                            next_v_in_loop = current_edge.verts[0]
                        found_next_edge = True
                        break

                if not found_next_edge or next_v_in_loop == v_start_loop:
                    # Loop closed or hit a dead end (shouldn't for closed boundary)
                    # Proper close
                    if next_v_in_loop == v_start_loop and len(ordered_loop) > 0:
                        # Ensure start not added by another loop
                        if ordered_loop[0] not in visited_verts or ordered_loop[0] == v_start_loop:
                            # Already added current_v (which is now next_v_in_loop's predecessor)
                            pass
                        else:  # Start was visited by another loop, discard
                            ordered_loop = []

                    elif not found_next_edge and current_v not in visited_verts:  # Reached end of an open boundary segment
                        ordered_loop.append(current_v)  # Add the last vertex
                        visited_verts.add(current_v)

                    break  # Exit walk for this loop

            if len(ordered_loop) > 2:  # Minimum 3 verts for a sensible loop
                loops.append(ordered_loop)

    return loops


class WM_OT_cutcartpaths(bpy.types.Operator):
    """Cut Cart Paths"""
    bl_label = "Cut Cart Paths"
    bl_idname = "wm.cutcartpaths"
    bl_options = {'REGISTER', 'UNDO'}

    def calculate_angle_from_perpendicular(self, vec1, vec2):
        """
        Calculate how many degrees a vector is from being perpendicular to another.
        """
        try:
            angle_rad = vec1.angle(vec2)
            angle_deg = math.degrees(angle_rad)
            deviation = abs(angle_deg - 90.0)
            return deviation
        except ValueError:
            return 180.0

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        # Get parameters from UI
        step_size = opcdtools.cart_cut_steps
        angle_tolerance_degrees = opcdtools.cart_angle_tolerance
        max_cut_distance = opcdtools.cart_max_distance
        preferred_cut_distance = opcdtools.cart_preferred_distance
        min_candidate_dist = opcdtools.cart_min_distance

        selected = [o for o in bpy.context.selected_objects if (
            'Spline' in o.name) and not ('Blend' in o.name)]

        for obj in selected:
            bpy.context.view_layer.objects.active = obj

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(type='VERT')
            bpy.ops.mesh.select_all(action='DESELECT')

            me = obj.data
            bm = bmesh.from_edit_mesh(me)

            cut_pairs = []
            used_vertices = set()  # Track ALL vertices used in cuts (start OR target)

            all_loops_verts = get_ordered_boundary_loops(bm)

            if not all_loops_verts:
                self.report({'WARNING'}, "No boundary loops found.")
                bmesh.update_edit_mesh(me)
                return {'CANCELLED'}

            all_loops_verts.sort(key=len, reverse=True)
            boundary_loop_verts = [v for loop in all_loops_verts for v in loop]
            loop_len = len(boundary_loop_verts)

            if loop_len < 3:
                self.report({'WARNING'}, "Longest boundary loop is too short.")
                bmesh.update_edit_mesh(me)
                return {'CANCELLED'}

            print(
                f"\nProcessing {obj.name}: Found boundary loop with {loop_len} vertices")
            print(
                f"Angle tolerance: {angle_tolerance_degrees}° from perpendicular")
            print(
                f"Max cut distance: {max_cut_distance}, Preferred: {preferred_cut_distance}")
            print(f"Min candidate distance: {min_candidate_dist}")
            print(f"Step size: {step_size}")

            # Identify all cut pairs - now looping through ALL vertices
            for i in range(0, loop_len):
                # Check if any of the last step_size vertices were used in cuts
                skip_this_vertex = False
                for lookback in range(-step_size, step_size + 1):
                    check_idx = i + lookback
                    if check_idx >= 0 and check_idx in used_vertices:
                        skip_this_vertex = True
                        break

                if skip_this_vertex:
                    continue

                # Also skip if this vertex itself is already used
                if i in used_vertices:
                    continue

                current_bm_vert = boundary_loop_verts[i]

                # Determine local tangent at current vertex
                prev_idx = (i - 1 + loop_len) % loop_len
                next_idx = (i + 1) % loop_len

                prev_bm_vert = boundary_loop_verts[prev_idx]
                next_bm_vert = boundary_loop_verts[next_idx]

                tangent_vec_start = (next_bm_vert.co - prev_bm_vert.co)
                if tangent_vec_start.length < 0.0001:
                    continue
                tangent_vec_start.normalize()

                # Search for target vertex
                best_target_bm_vert = None
                best_target_idx = None
                best_score = float('inf')
                best_angles = None
                best_distance = None

                for k in range(loop_len):
                    if k == i:
                        continue

                    # Skip if this vertex is already used in a cut
                    if k in used_vertices:
                        continue

                    candidate_bm_vert = boundary_loop_verts[k]

                    vec_to_candidate = candidate_bm_vert.co - current_bm_vert.co
                    dist_to_candidate = vec_to_candidate.length

                    # Skip if distance exceeds maximum
                    if dist_to_candidate > max_cut_distance:
                        continue

                    if dist_to_candidate < min_candidate_dist:
                        continue

                    if vec_to_candidate.length < 0.0001:
                        continue

                    vec_to_candidate_normalized = vec_to_candidate.normalized()

                    # Check angle at START vertex
                    angle_dev_start = self.calculate_angle_from_perpendicular(
                        tangent_vec_start,
                        vec_to_candidate_normalized
                    )

                    if angle_dev_start > angle_tolerance_degrees:
                        continue

                    # Calculate tangent at candidate vertex
                    candidate_prev_idx = (k - 1 + loop_len) % loop_len
                    candidate_next_idx = (k + 1) % loop_len

                    candidate_prev_vert = boundary_loop_verts[candidate_prev_idx]
                    candidate_next_vert = boundary_loop_verts[candidate_next_idx]

                    tangent_vec_target = (
                        candidate_next_vert.co - candidate_prev_vert.co)
                    if tangent_vec_target.length < 0.0001:
                        continue
                    tangent_vec_target.normalize()

                    # Check angle at TARGET vertex
                    vec_from_candidate = -vec_to_candidate_normalized

                    angle_dev_target = self.calculate_angle_from_perpendicular(
                        tangent_vec_target,
                        vec_from_candidate
                    )

                    if angle_dev_target > angle_tolerance_degrees:
                        continue

                    # Calculate score favoring shorter cuts and better angles
                    if dist_to_candidate <= preferred_cut_distance:
                        distance_score = 0.0
                    else:
                        distance_score = (
                            dist_to_candidate - preferred_cut_distance) / (max_cut_distance - preferred_cut_distance)

                    angle_score = (angle_dev_start + angle_dev_target) / \
                        (2.0 * angle_tolerance_degrees)

                    # Combined score: 70% distance, 30% angle
                    combined_score = 0.7 * distance_score + 0.3 * angle_score

                    if combined_score < best_score:
                        best_score = combined_score
                        best_target_bm_vert = candidate_bm_vert
                        best_target_idx = k
                        best_angles = (angle_dev_start, angle_dev_target)
                        best_distance = dist_to_candidate

                # Store the cut pair and mark both vertices as used
                if best_target_bm_vert:
                    # Check if any of the last step_size vertices were used in cuts
                    skip_this_vertex = False
                    for lookback in range(-step_size, step_size + 1):
                        check_idx = best_target_idx + lookback
                        if check_idx >= 0 and check_idx in used_vertices:
                            skip_this_vertex = True
                            break

                    if skip_this_vertex:
                        continue

                    cut_pairs.append((
                        current_bm_vert.co.copy(),
                        best_target_bm_vert.co.copy()
                    ))

                    # Mark BOTH vertices as used
                    used_vertices.add(i)
                    used_vertices.add(best_target_idx)

                    print(f"  Cut {len(cut_pairs)}: vert {i} to vert {best_target_idx}, "
                          f"angles from perp: start={best_angles[0]:.1f}°, target={best_angles[1]:.1f}°, "
                          f"dist={best_distance:.2f}")

            # Perform all cuts
            print(f"\nPerforming {len(cut_pairs)} cuts...")
            cuts_made = 0
            for idx, (start_co, end_co) in enumerate(cut_pairs):
                bm.free()
                bm = bmesh.from_edit_mesh(me)
                bm.verts.ensure_lookup_table()

                closest_start = min(
                    bm.verts, key=lambda v: (v.co - start_co).length)
                closest_end = min(
                    bm.verts, key=lambda v: (v.co - end_co).length)

                for v in bm.verts:
                    v.select_set(False)
                bm.select_flush(False)

                closest_start.select_set(True)
                closest_end.select_set(True)
                bm.select_flush(True)

                newly_cut_bmverts_list = knife_cut_and_color_bmesh(
                    obj, bm,
                    keep_both_sides=True,
                    color_name='blue',
                    color_strength=1.0
                )

                if newly_cut_bmverts_list is not None:
                    cuts_made += 1
                    bmesh.update_edit_mesh(me)
                    print(f"  Cut {idx+1}/{len(cut_pairs)} success")
                else:
                    print(f"  WARNING: Cut {idx+1}/{len(cut_pairs)} failed")

            bpy.ops.mesh.select_mode(type="VERT")
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold=0.0001)

            bm.free()
            bmesh.update_edit_mesh(me)
            print(f"Completed {cuts_made}/{len(cut_pairs)} cuts\n")
            self.report({'INFO'}, f"Finished. Made {cuts_made} cuts.")

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


def get_boundary_loop(bm):
    """Get boundary loop vertices efficiently"""
    boundary_verts = set()
    for edge in bm.edges:
        if edge.is_boundary:
            boundary_verts.add(edge.verts[0])
            boundary_verts.add(edge.verts[1])
    return list(boundary_verts)


def get_loop_at_depth(bm, depth):
    """Get loop at specific depth from boundary efficiently"""
    # Start with boundary
    boundary = get_boundary_loop(bm)
    if depth == 0:
        return boundary

    # Mark boundary vertices
    for v in bm.verts:
        v.tag = False
    for v in boundary:
        v.tag = True

    # Grow selection inward by depth
    current_ring = set(boundary)
    for _ in range(depth):
        next_ring = set()
        for v in current_ring:
            for edge in v.link_edges:
                other = edge.other_vert(v)
                if not other.tag:
                    next_ring.add(other)
                    other.tag = True
        if not next_ring:
            break
        current_ring = next_ring

    return list(current_ring)


def build_kdtree(verts):
    """Build a KDTree for fast nearest neighbor searches"""
    kd = KDTree(len(verts))
    for i, v in enumerate(verts):
        kd.insert(v.co, i)
    kd.balance()
    return kd, verts


class WM_OT_randomslideloop(bpy.types.Operator):
    """Random Slide Loop"""
    bl_label = "Slide a inset loop in randomly"
    bl_idname = "wm.randomslideloop"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        random_amt = opcdtools.loopslide_random
        loop_inset = opcdtools.loopslide_inset

        selected = bpy.context.selected_objects

        counter = 1
        total = len(selected)

        for ob in selected:
            print(f"Sliding - {ob.name} {counter} of {total}")

            if 'Blend' in ob.name:
                print('blends are not supported for loop slide')
                counter += 1
                continue

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            mesh = ob.data
            bpy.ops.object.mode_set(mode='EDIT')

            # Create BMesh once
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()

            # Get all three loops at once
            try:
                # Target loop
                target_verts = get_loop_at_depth(bm, loop_inset)

                # Outer loop (one step out from target)
                outer_verts = get_loop_at_depth(bm, max(0, loop_inset - 1))

                # Inner loop (one step in from target)
                inner_verts = get_loop_at_depth(bm, loop_inset + 1)

                if not target_verts or not outer_verts or not inner_verts:
                    print(f"Could not find loops for {ob.name}")
                    bpy.ops.object.mode_set(mode='OBJECT')
                    counter += 1
                    continue

                # Build KDTrees for fast nearest neighbor search
                kd_outer, outer_list = build_kdtree(outer_verts)
                kd_inner, inner_list = build_kdtree(inner_verts)

                # Process all vertices at once
                for v in target_verts:
                    # Generate random value with gaussian distribution
                    randomVal = random.gauss(0, random_amt/3.0)
                    # Clamp to [-0.95, 0.95]
                    randomVal = max(-0.95, min(0.95, randomVal))

                    if randomVal > 0:
                        # Find nearest vertex in outer loop
                        co, index, dist = kd_outer.find(v.co)
                        nearest_vert = outer_list[index]
                    else:
                        # Find nearest vertex in inner loop
                        randomVal = -randomVal
                        co, index, dist = kd_inner.find(v.co)
                        nearest_vert = inner_list[index]

                    # Calculate and apply translation
                    edge_vector = nearest_vert.co - v.co
                    v.co += edge_vector * randomVal

                # Update the mesh
                bmesh.update_edit_mesh(mesh)

            except Exception as e:
                print(f"Error processing {ob.name}: {e}")

            bpy.ops.object.mode_set(mode='OBJECT')
            counter += 1

        # Restore selection
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_straightenloops(bpy.types.Operator):
    """Straighten loops"""
    bl_label = "Straighten interior loops"
    bl_idname = "wm.straightenloops"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        loop_inset = opcdtools.loopslide_inset

        selected = bpy.context.selected_objects

        if loop_inset == 0:
            print('inset must be greater than zero')
            return {'FINISHED'}

        counter = 1
        total = len(selected)

        for ob in selected:
            print(f"Sliding - {ob.name} {counter} of {total}")

            if 'Blend' in ob.name:
                print('blends are not supported for loop slide')
                counter += 1
                continue

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            mesh = ob.data
            bpy.ops.object.mode_set(mode='EDIT')

            # Create BMesh once
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()

            try:
                # Get target and exterior loops
                target_verts = get_loop_at_depth(bm, loop_inset)
                exterior_verts = get_boundary_loop(bm)

                if not target_verts or not exterior_verts:
                    print(f"Could not find loops for {ob.name}")
                    bpy.ops.object.mode_set(mode='OBJECT')
                    counter += 1
                    continue

                # Build KDTree for exterior vertices
                kd_exterior, exterior_list = build_kdtree(exterior_verts)

                # Find global minimum distance (closest vertex to exterior)
                global_min_dist = float('inf')
                nearest_pairs = []  # Store nearest exterior vert for each target vert

                for v in target_verts:
                    co, index, dist = kd_exterior.find(v.co)
                    nearest_pairs.append((v, exterior_list[index], dist))
                    if dist < global_min_dist:
                        global_min_dist = dist

                # Slide each vertex to uniform distance
                for v, nearest_exterior, current_dist in nearest_pairs:
                    edge_vector = nearest_exterior.co - v.co
                    slide_amount = current_dist - global_min_dist
                    edge_vector.normalize()
                    v.co += edge_vector * slide_amount

                # Update the mesh
                bmesh.update_edit_mesh(mesh)

            except Exception as e:
                print(f"Error processing {ob.name}: {e}")

            bpy.ops.object.mode_set(mode='OBJECT')
            counter += 1

        # Restore selection
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_spaceloops(bpy.types.Operator):
    """Space out loops"""
    bl_label = "Slide exterior loops"
    bl_idname = "wm.spaceloops"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        loop_inset = opcdtools.loopslide_inset
        slide_amount = opcdtools.loopslide_amount

        selected = bpy.context.selected_objects

        counter = 1
        total = len(selected)

        for ob in selected:
            print(f"Sliding - {ob.name} {counter} of {total}")

            if 'Blend' in ob.name:
                print('blends are not supported for loop slide')
                counter += 1
                continue

            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            mesh = ob.data
            bpy.ops.object.mode_set(mode='EDIT')

            # Create BMesh once
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()

            try:
                # Get target and inner loops
                target_verts = get_loop_at_depth(bm, loop_inset)
                inner_verts = get_loop_at_depth(bm, loop_inset + 1)

                if not target_verts or not inner_verts:
                    print(f"Could not find loops for {ob.name}")
                    bpy.ops.object.mode_set(mode='OBJECT')
                    counter += 1
                    continue

                # Build KDTree for inner vertices
                kd_inner, inner_list = build_kdtree(inner_verts)

                # Process all vertices at once
                for v in target_verts:
                    # Find nearest vertex in inner loop
                    co, index, dist = kd_inner.find(v.co)
                    nearest_vert = inner_list[index]

                    # Calculate and apply translation
                    edge_vector = nearest_vert.co - v.co
                    edge_vector.normalize()
                    v.co += edge_vector * slide_amount

                # Update the mesh
                bmesh.update_edit_mesh(mesh)

            except Exception as e:
                print(f"Error processing {ob.name}: {e}")

            bpy.ops.object.mode_set(mode='OBJECT')
            counter += 1

        # Restore selection
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


def separate_vertices_into_loops(input_verts_list):
    if not input_verts_list:
        return []

    input_verts_set = set(input_verts_list)
    visited_verts = set()
    all_loops = []

    for start_vert in input_verts_list:
        if start_vert in visited_verts:
            continue  # Already processed this vertex as part of another loop

        # Found a starting point for a new potential loop
        current_loop = []
        queue = deque([start_vert])
        loop_visited_in_trace = set()

        processed_start_node = False
        while queue:
            current_vert = queue.popleft()

            # This check prevents adding the start node twice if loop closes perfectly
            # and handles cases where a node might be revisited during trace (though ideally shouldn't happen in simple loops)
            if current_vert in loop_visited_in_trace:
                continue

            # Check if it belongs to the original input set
            # And ensure it hasn't been globally visited by a *previous* loop's trace
            if current_vert in input_verts_set and current_vert not in visited_verts:
                current_loop.append(current_vert)
                visited_verts.add(current_vert)  # Mark globally visited
                loop_visited_in_trace.add(current_vert)

                # Find connected neighbors that are also in the input set and not yet visited globally
                found_neighbor = False
                for edge in current_vert.link_edges:
                    other_vert = edge.other_vert(current_vert)

                    # Check if the neighbor is part of the loops we are interested in
                    if other_vert in input_verts_set:
                        # If the neighbor is the start of THIS loop and we have > 2 verts, we closed the loop
                        # Or if the neighbour hasn't been added to *this specific* loop trace yet
                        if other_vert == start_vert and len(current_loop) > 2:
                            found_neighbor = True
                            break

                        elif other_vert not in loop_visited_in_trace and other_vert not in visited_verts:
                            # Add valid neighbor to process
                            queue.append(other_vert)
                            found_neighbor = True

                # This break might be needed if the start node itself closes the loop
                if found_neighbor and current_vert == start_vert and len(current_loop) > 2:
                    break

            elif current_vert == start_vert and len(current_loop) > 0:
                # Handles closing the loop if we revisit the start vertex via the queue
                # Ensure not to add start vert twice if already added
                pass

        # Add the found loop to the list if it's valid
        if len(current_loop) > 1:
            if current_loop:
                all_loops.append(current_loop)
        elif len(current_loop) == 1 and start_vert not in visited_verts:
            pass

    return all_loops


class WM_OT_loopcut(bpy.types.Operator):
    """Loop Cut"""
    bl_label = "Loop cut at an inset for all selected meshes"
    bl_idname = "wm.loopcut"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        loop_inset = opcdtools.loopcut_inset

        selected = bpy.context.selected_objects

        counter = 1
        total = len(selected)
        for ob in selected:
            print("Cutting -", ob.name, counter, " of ", total)
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            mesh = (ob.data)

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='VERT')

            if 'Mesh' in ob.name:
                # find target loop
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.region_to_loop()
                if (loop_inset > 0):
                    bpy.ops.mesh.select_all(action='INVERT')
                    for i in range(loop_inset-1):
                        bpy.ops.mesh.select_less()
                    bpy.ops.mesh.region_to_loop()
                bm = bmesh.from_edit_mesh(mesh)
                all_verts = [v for v in bm.verts if v.select]

                list_of_loops = separate_vertices_into_loops(all_verts)
                list_of_loops_indices = [
                    [v.index for v in loop_bmverts] for loop_bmverts in list_of_loops]

                for i, loop in enumerate(list_of_loops_indices):
                    print(f"  Loop {i+1}: {len(loop)} vertices")
                    verts_i = [v_i for v_i in loop]
                    # find inner loop
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.mesh.region_to_loop()
                    bpy.ops.mesh.select_all(action='INVERT')
                    for i in range(loop_inset):
                        bpy.ops.mesh.select_less()
                    bpy.ops.mesh.region_to_loop()
                    bm = bmesh.from_edit_mesh(mesh)
                    bm.verts.ensure_lookup_table()
                    bm.edges.ensure_lookup_table()

                    # Get the vertices selected by the inner loop selection ops
                    verts_inner = [v for v in bm.verts if v.select]

                    # Get the starting vertex 'v' from the outer loop index list
                    # Check if the list and index are valid first
                    if not verts_i or verts_i[len(verts_i)//2] >= len(bm.verts):
                        print(
                            f"    Error: Invalid starting vertex index {verts_i[len(verts_i)//2]}. Skipping nearest vertex search.")
                        nearestVert = None
                    else:
                        v = bm.verts[verts_i[len(verts_i)//2]]

                        for vert_in_bm in bm.verts:
                            vert_in_bm.select = False
                        v.select = True

                        nearestVert = None
                        minDist = float('inf')

                        if not verts_inner:
                            print(
                                "    Warning: Inner loop vertex list ('verts_inner') is empty.")
                        else:
                            for v1 in verts_inner:
                                distCheck = vert_distance(v.co, v1.co)

                                # Check if this vertex is closer than the current minimum
                                if distCheck < minDist:
                                    # Now, check if v and v1 are directly connected by an edge
                                    is_connected = False
                                    for edge in v.link_edges:  # Iterate through edges connected to 'v'
                                        # Check if the other vertex of this edge is 'v1'
                                        if edge.other_vert(v) == v1:
                                            is_connected = True
                                            break  # Found the connecting edge, no need to check further edges for v

                                    if is_connected:
                                        print("CONNECTED: ", distCheck)
                                        minDist = distCheck
                                        nearestVert = v1

                        if nearestVert:
                            nearestVert.select = True

                    bpy.ops.mesh.select_all(action='DESELECT')
                    v.select = True
                    nearestVert.select = True

                    for edge in bm.edges:
                        if edge.verts[0].select and edge.verts[1].select:
                            edge.select = True
                            my_edge = edge

                    bpy.ops.mesh.loopcut(
                        object_index=0, edge_index=my_edge.index)

            elif 'Blend' in ob.name:
                # assemble list of vertex indices that belong to a group
                # the exterior of the blend only belongs to a group, don't know why
                verticesIndices = []
                verts = [v for v in mesh.vertices if len(v.groups) > 0]

                for v in verts:
                    verticesIndices.append(v.index)

                # now select the verts at those indices
                bm = bmesh.from_edit_mesh(mesh)
                verts = [v for v in bm.verts]
                bpy.ops.mesh.select_all(action='DESELECT')
                arbitraryVertSet = False
                for v in verts:
                    if (v.index in verticesIndices):
                        v.select = True
                    elif (not arbitraryVertSet) and (len(v.link_edges) == 3):
                        arbitraryVert = v
                        arbitraryVertSet = True

                # select the vert across from the arbitrary chosen vert
                verts = [v for v in bm.verts if v.select]
                nearestVert = verts[0]
                minDist = float('inf')
                for v1 in verts:
                    distCheck = vert_distance(arbitraryVert.co, v1.co)
                    if distCheck < minDist:
                        minDist = distCheck
                        nearestVert = v1
                v = arbitraryVert

                bpy.ops.mesh.select_all(action='DESELECT')
                v.select = True
                nearestVert.select = True

                for edge in bm.edges:
                    if edge.verts[0].select and edge.verts[1].select:
                        edge.select = True
                        my_edge = edge

                bpy.ops.mesh.loopcut(object_index=0, edge_index=my_edge.index)

                # for blends, people are usually wanting a separate color on the middle loop
                # they can always change it later easily
                color_to_vertices('blue', 1.0)

            else:
                print('not a blend or a mesh - skipping')
                continue

            bpy.ops.object.mode_set(mode='OBJECT')
            counter += 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_topoedit(bpy.types.Operator):
    """Apply Topology Changes to Meshes"""
    bl_label = "Apply Ripple effect to selected Mesh"
    bl_idname = "wm.topoedit"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        ripple_height = opcdtools.ripple_height
        ripple_smooth = opcdtools.ripple_smooth

        selected = [o for o in bpy.context.selected_objects if 'Mesh' in o.name]

        for o in selected:
            bpy.ops.object.select_all(action='DESELECT')
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            ripple_inset = opcdtools.ripple_inset

            bpy.ops.object.mode_set(mode='EDIT')

            # get selection excluding outer set of vertices - in Face Selection Mode
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.region_to_loop()
            bpy.ops.mesh.select_all(action='INVERT')
            bpy.ops.mesh.select_mode(
                use_extend=False, use_expand=False, type='FACE')
            # bpy.ops.mesh.select_less(use_face_step=False)

            # inset loops, integer value
            if 'Bunker' in o.name or 'Water_Base_Lake' in o.name:
                # ripple_inset = ripple_inset-1
                while ripple_inset > 1:
                    ripple_inset = ripple_inset-1
                    bpy.ops.mesh.select_less(use_face_step=False)
            else:
                while ripple_inset >= 1:
                    ripple_inset = ripple_inset-1
                    bpy.ops.mesh.select_less(use_face_step=False)

            # use Mesh Tools, Random Vertices
            bpy.ops.mesh.random_vertices(factor=30, valmin=(
                0, 0, -ripple_height), valmax=(1, 1, ripple_height))
            bpy.ops.mesh.vertices_smooth(
                factor=ripple_smooth, repeat=1, xaxis=True, yaxis=True, zaxis=True)

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for ob in selected:
            ob.select_set(True)

        return {'FINISHED'}


class WM_OT_Meshjoin(bpy.types.Operator):
    """Join Selected Meshes and Edit - for Bulkhead Lowering"""
    bl_label = "Join Selected Meshes"
    bl_idname = "wm.joinandedit"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = bpy.context.selected_objects

        for ob in selected:
            if 'Base' in ob.name:
                bpy.context.view_layer.objects.active = ob

        bpy.ops.object.join()

        join = bpy.context.view_layer.objects.active

        bpy.ops.object.editmode_toggle()
        bpy.ops.mesh.select_mode(type="EDGE")
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.remove_doubles(threshold=0.01)
        bpy.ops.object.editmode_toggle()

        return {'FINISHED'}


class WM_OT_matselection(bpy.types.Operator):
    """Select All Meshes of Material Type"""
    bl_label = "Select Meshes"
    bl_idname = "wm.matselection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        mat_selection = opcdtools.mat_selection

        meshes = bpy.context.visible_objects

        bpy.ops.object.select_all(action='DESELECT')

        for m in meshes:
            if mat_selection in m.name and not 'Blend' in m.name:
                m.select_set(True)
                bpy.context.view_layer.objects.active = m

        return {'FINISHED'}


class WM_OT_blendselection(bpy.types.Operator):
    """Select All Blends of Material Type"""
    bl_label = "Select Blends"
    bl_idname = "wm.blendselection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        mat_selection = opcdtools.mat_selection

        meshes = bpy.context.visible_objects

        bpy.ops.object.select_all(action='DESELECT')

        for m in meshes:
            if mat_selection in m.name and 'Blend' in m.name:
                m.select_set(True)
                bpy.context.view_layer.objects.active = m

        return {'FINISHED'}

    # def execute(self, context):
    #     layout = self.layout
    #     scene = context.scene
    #     opcdtools = scene.opcdtools
    #     blend_selection = opcdtools.blend_selection

    #     meshes = bpy.context.visible_objects

    #     bpy.ops.object.select_all(action='DESELECT')

    #     for m in meshes:
    #         if blend_selection in m.name and 'Blend' in m.name:
    #             m.select_set(True)
    #             bpy.context.view_layer.objects.active = m

    #     return { 'FINISHED' }


class WM_OT_storemesh(bpy.types.Operator):
    """Duplicate and Hide Selected Meshes"""
    bl_label = "Create Backup Mesh"
    bl_idname = "wm.storemesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        # Get all selected objects
        selected_objects = bpy.context.selected_objects

        for selected_obj in selected_objects:
            if selected_obj and selected_obj.type == 'MESH':
                if not selected_obj.name + "_-_bak" in bpy.context.scene.objects:

                    # if not '_-_bak' in selected_obj.name:

                    # Duplicate the selected object
                    duplicated_obj = selected_obj.copy()
                    duplicated_obj.data = selected_obj.data.copy()
                    duplicated_obj.name = remove_numerical_extension(
                        duplicated_obj.name) + "_-_bak"

                    # Link the duplicated object to the same collection as the selected object
                    collection = selected_obj.users_collection[0]
                    collection.objects.link(duplicated_obj)

                    # Make the duplicated mesh the active object
                    bpy.context.view_layer.objects.active = selected_obj
                    selected_obj.select_set(True)

                    # Hide the original object
                    duplicated_obj.hide_set(True)
                    # selected_obj.hide_render = True

                else:
                    # If multiple matches exist, display a message to the user
                    bpy.context.window_manager.popup_menu(lambda self, context: self.layout.label(
                        text="A duplicate mesh already exists."), title="Duplicate Mesh Warning", icon='INFO')

        return {'FINISHED'}


class WM_OT_restoremesh(bpy.types.Operator):
    """Restore to Original Mesh"""
    bl_label = "Restore from Backup Mesh"
    bl_idname = "wm.restoremesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        # Get all selected objects
        selected_objects = bpy.context.selected_objects

        for selected_obj in selected_objects:
            # Check if the selected object is a mesh
            if selected_obj and selected_obj.type == 'MESH':
                if selected_obj.name + "_-_bak" in bpy.context.scene.objects:

                    # get reference to backup mesh
                    backup_mesh = bpy.data.objects.get(
                        selected_obj.name + "_-_bak")

                    # Get a reference to the object you want to remove
                    object_to_remove = selected_obj

                    # Remove the object from the scene
                    bpy.data.objects.remove(object_to_remove, do_unlink=True)

                    # Unhide the backup and make active mesh
                    backup_mesh.hide_set(False)
                    backup_mesh.select_set(True)
                    bpy.context.view_layer.objects.active = backup_mesh

                    # rename backup mesh
                    backup_mesh.name = backup_mesh.name[:-6]

                    # # get name of original object
                    # if selected_obj.name[-3:].isnumeric():
                    #     original_name = selected_obj.name[:-10]

                    # else:
                    #     original_name = selected_obj.name[:-6]

                    # if original_name in bpy.data.objects:
                    #     obj_to_select = bpy.data.objects[original_name]
                    #     bpy.ops.object.select_all(action='DESELECT')
                    #     obj_to_select.hide_set(False)
                    #     obj_to_select.select_set(True)
                    #     bpy.context.view_layer.objects.active = obj_to_select

        return {'FINISHED'}


class WM_OT_matchange(bpy.types.Operator):
    """Change Mesh and Material Name to Dropdown"""
    bl_label = "Change Mesh and Material"
    bl_idname = "wm.matchange"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        mat_change = opcdtools.mat_change
        mat_name = mat_change

        selected = bpy.context.selected_objects

        bpy.ops.object.select_all(action='DESELECT')

        for o in selected:
            bpy.ops.object.select_all(action='DESELECT')
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            if '_-_mod' in o.name:
                truncated_name = "_".join(o.name.split("_")[:-5])
            else:
                truncated_name = "_".join(o.name.split("_")[:-3])

            print(truncated_name)

            # Assign Material - remove Blend mat
            if 'Blend' in o.name:
                mat_change = mat_change + " - Blend"

            if mat_change in bpy.data.materials:
                mat = bpy.data.materials.get(mat_change)
            else:
                mat = bpy.data.materials.new(mat_change)
                mat.diffuse_color = (0.8, 0.16, 0.5, 1)

            if o.data.materials:
                o.data.materials[0] = mat
            else:
                o.data.materials.append(mat)

            if 'Blend' in o.name:
                o.name = truncated_name + "_" + mat_name + '_-_Blend'

            if 'Mesh' in o.name:
                o.name = truncated_name + "_" + mat_name + '_-_Mesh'

        return {'FINISHED'}


class WM_OT_applymod(bpy.types.Operator):
    """Apply Modifiers to Selected Mesh"""
    bl_label = "Apply Modifiers to Selected"
    bl_idname = "wm.applymod"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        mat_selection = opcdtools.mat_selection

        selected = bpy.context.selected_objects

        bpy.ops.object.select_all(action='DESELECT')

        for o in selected:
            bpy.ops.object.select_all(action='DESELECT')
            o.select_set(True)
            bpy.context.view_layer.objects.active = o

            bpy.ops.object.convert(target='MESH')

        return {'FINISHED'}


class WM_OT_area_debug(bpy.types.Operator):
    """Evaluate Cut shapes for Microscopic Meshes"""
    bl_label = "Area Evaluate"
    bl_idname = "wm.areadebug"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = [o for o in context.visible_objects if 'Cut' in o.name]

        bpy.ops.object.select_all(action='DESELECT')

        counter = 1
        total = len(selected)
        errors = []
        for ob in selected:
            bpy.ops.object.select_all(action='DESELECT')
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob

            print("Analyzing Mesh - ", counter, " of ", total)

            me = ob.data

            bm = bmesh.new()
            bm.from_mesh(me)

            area = sum(f.calc_area() for f in bm.faces)
            if area < 0.01:
                errors.append(ob.name)
                print(ob.name, ' - ', area)

            bm.free()
            counter += 1

        bpy.ops.object.select_all(action='DESELECT')

        if errors:
            print("\nPossible errors in the following Meshes: ")
            for error in errors:
                print(error)
        else:
            print("\nNo microscopic Shapes detected that could cause Meshing errors")

        return {'FINISHED'}


class WM_OT_meshcut(bpy.types.Operator):
    """Cut out a Mesh - select Cutter and then mesh to be Cut"""
    bl_label = "Mesh Cut"
    bl_idname = "wm.meshcut"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        context = bpy.context
        scene = context.scene
        opcdtools = scene.opcdtools

        selected = [o for o in context.selected_objects if 'Spline' in o.name]

        bpy.ops.object.select_all(action='DESELECT')

        # create duplicate mesh for cutting
        cutter = selected[0].copy()
        cutter.data = selected[0].data.copy()
        scene.collection.objects.link(cutter)
        print(cutter.name)

        bm = bmesh.new()
        bm.from_mesh(cutter.data)

        for f in bm.faces:
            f.select = True
        bmesh.ops.solidify(
            bm, geom=[f for f in bm.faces if f.select], thickness=1)

        for v in bm.verts:
            v.select = True
            v.co.z += 0.5

        bm.to_mesh(cutter.data)
        cutter.data.update()
        bm.clear()

        bm.free()

        print(selected[0].name)
        print(selected[1].name)
        print(cutter.name)

        obj_modifier = selected[1].modifiers.new('Boolean', 'BOOLEAN')
        obj_modifier.object = bpy.data.objects[cutter.name]
        obj_modifier.operation = 'DIFFERENCE'
        obj_modifier.solver = 'FAST'
        bpy.ops.object.modifier_apply(modifier='Boolean')
#
        objs = bpy.data.objects
        objs.remove(objs[cutter.name], do_unlink=True)

        return {'FINISHED'}


class WM_OT_TerrainSmooth(bpy.types.Operator):
    """Apply Terrain Smoothing Settings"""
    bl_label = "Apply Terrain Smooth"
    bl_idname = "wm.terrainsmooth"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools

        terrainsmooth = opcdtools.terrainsmooth

        bpy.ops.object.select_all(action='DESELECT')

        ob = bpy.data.objects['Terrain']

        if 'Terrain' in ob.name:
            ob.select_set(True)
            bpy.context.view_layer.objects.active = ob
            ob.modifiers["Smooth"].factor = terrainsmooth

        bpy.ops.object.select_all(action='DESELECT')

        return {'FINISHED'}


class WM_OT_ClearMeshes(bpy.types.Operator):
    """Save the current blend file"""
    bl_idname = "wm.saveandclearcache"
    bl_label = "SAVE FILE"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not (bpy.data.filepath and bpy.data.filepath.strip()):
            self.report({'ERROR'}, "Save the blend file first.")
            return {'CANCELLED'}
        bpy.ops.wm.save_mainfile()
        return {'FINISHED'}


class BatchExporter(bpy.types.Operator):
    """Batch Export FBX files"""
    bl_idname = "object.batch_fbx_export"
    bl_label = "Batch Export ALL FBX"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        export_folder = os.path.realpath(
            bpy.path.abspath(opcdtools.export_folder))

        # set cursor location and origin to 0,0,0
        bpy.context.scene.cursor.location = (0, 0, 0)
        bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')

        # export FBX files to "fbx" folder
        user_selected_folder_valid = False
        if export_folder:
            isExist = os.path.exists(export_folder)
            if isExist:
                path = export_folder
                user_selected_folder_valid = True

        if not user_selected_folder_valid:
            basedir = os.path.dirname(bpy.data.filepath)
            path = os.path.join(basedir, "fbx")
            isExist = os.path.exists(path)
            if not isExist:
                os.makedirs(path)
                print("Making fbx export directory")

        selected = bpy.context.visible_objects

        bpy.ops.object.select_all(action='DESELECT')

        for obj in selected:
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj

            name = bpy.path.clean_name(obj.name)
            fn = os.path.join(path, name)

            bpy.ops.export_scene.fbx(
                filepath=fn + ".fbx", use_selection=True, bake_space_transform=True)

            obj.select_set(False)

            print("written:", fn)

        return {'FINISHED'}


class SelectedExporter(bpy.types.Operator):
    """Export Selected Meshes to FBX files"""
    bl_idname = "object.selected_fbx_export"
    bl_label = "Export Selected FBX"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        layout = self.layout
        scene = context.scene
        opcdtools = scene.opcdtools
        export_folder = os.path.realpath(
        bpy.path.abspath(opcdtools.export_folder))

        # set cursor location and origin to 0,0,0
        bpy.context.scene.cursor.location = (0, 0, 0)
        bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')

        # export FBX files to "fbx" folder
        user_selected_folder_valid = False
        if opcdtools.export_folder:
            isExist = os.path.exists(export_folder)
            if isExist:
                path = export_folder
                user_selected_folder_valid = True

        if not user_selected_folder_valid:
            basedir = os.path.dirname(bpy.data.filepath)
            path = os.path.join(basedir, "fbx")
            isExist = os.path.exists(path)
            if not isExist:
                os.makedirs(path)
                print("Making fbx export directory")

        selected = bpy.context.selected_objects

        bpy.ops.object.select_all(action='DESELECT')

        for obj in selected:
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj

            name = bpy.path.clean_name(obj.name)
            fn = os.path.join(path, name)

            bpy.ops.export_scene.fbx(
                filepath=fn + ".fbx", use_selection=True, bake_space_transform=True)

            obj.select_set(False)

            print("written:", fn)

        return {'FINISHED'}


class WM_OT_readoperations(Operator, ImportHelper):
    """Read JSON structure from file and trigger loopcut based on parsed data"""
    bl_idname = "wm.readoperations"
    bl_label = "Read JSON Operations"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(
        default="*.json",
        options={'HIDDEN'},
    )

    def execute(self, context):
        # Try to open and read the JSON file
        try:
            with open(self.filepath, 'r') as json_file:
                data = json.load(json_file)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to read JSON: {e}")
            return {'CANCELLED'}

        if not isinstance(data, list):
            self.report({'ERROR'}, "JSON root must be an array.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Successfully read JSON: {data}")

        # Parse JSON data
        for item in data:
            if "bunkeredit" in item:
                sub_data = item["bunkeredit"]
                bunker_selection_type = sub_data.get(
                    "bunker_selection_type", None)
                bunker_from_conformed = sub_data.get(
                    "bunker_from_conformed", None)
                bunker_lip_depth = sub_data.get("bunker_lip_depth", None)
                bunker_inner_depth = sub_data.get("bunker_inner_depth", None)
                bunkerlip_type = sub_data.get("bunkerlip_type", None)
                bunker_dig_depth = sub_data.get("bunker_dig_depth", None)
                bunker_dig_shape = sub_data.get("bunker_dig_shape", None)
                bunker_dig_inset = sub_data.get("bunker_dig_inset", None)

                if (bunker_selection_type is not None) and \
                   (bunker_from_conformed is not None) and \
                   (bunker_lip_depth is not None) and \
                   (bunker_inner_depth is not None) and \
                   (bunkerlip_type is not None) and \
                   (bunker_dig_depth is not None) and \
                   (bunker_dig_shape is not None) and \
                   (bunker_dig_inset is not None):
                    context.scene.opcdtools.bunker_selection_type = bunker_selection_type
                    context.scene.opcdtools.bunker_from_conformed = bunker_from_conformed
                    context.scene.opcdtools.bunker_lip_depth = bunker_lip_depth
                    context.scene.opcdtools.bunker_inner_depth = bunker_inner_depth
                    context.scene.opcdtools.bunkerlip_type = bunkerlip_type
                    context.scene.opcdtools.bunker_dig_depth = bunker_dig_depth
                    context.scene.opcdtools.bunker_dig_shape = bunker_dig_shape
                    context.scene.opcdtools.bunker_dig_inset = bunker_dig_inset
                    bpy.ops.wm.bunkeredit()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "bunkereditlip" in item:
                sub_data = item["bunkereditlip"]
                bunker_selection_type = sub_data.get(
                    "bunker_selection_type", None)
                bunker_lip_depth = sub_data.get("bunker_lip_depth", None)
                bunker_dig_inset = sub_data.get("bunker_dig_inset", None)
                random_amt_topo = sub_data.get("random_amt_topo", 1.0)

                if (bunker_selection_type is not None) and \
                   (bunker_lip_depth is not None) and \
                   (bunker_dig_inset is not None):
                    context.scene.opcdtools.bunker_selection_type = bunker_selection_type
                    context.scene.opcdtools.bunker_lip_depth = bunker_lip_depth
                    context.scene.opcdtools.bunker_dig_inset = bunker_dig_inset
                    context.scene.opcdtools.random_amt_topo = random_amt_topo
                    bpy.ops.wm.bunkereditlip()
                    context.scene.opcdtools.random_amt_topo = 1.0
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "bunkereditangle" in item:
                sub_data = item["bunkereditangle"]
                bunker_selection_type = sub_data.get(
                    "bunker_selection_type", None)
                bunker_dig_inset = sub_data.get("bunker_dig_inset", None)
                random_amt_topo = sub_data.get("random_amt_topo", 1.0)
                dig_z_threshold = sub_data.get("dig_z_threshold", None)
                dig_flatness = sub_data.get("dig_flatness", None)

                if (bunker_selection_type is not None) and \
                   (dig_z_threshold is not None) and \
                   (dig_flatness is not None) and \
                   (bunker_dig_inset is not None):
                    context.scene.opcdtools.bunker_selection_type = bunker_selection_type
                    context.scene.opcdtools.bunker_dig_inset = bunker_dig_inset
                    context.scene.opcdtools.random_amt_topo = random_amt_topo
                    context.scene.opcdtools.dig_z_threshold = dig_z_threshold
                    context.scene.opcdtools.dig_flatness = dig_flatness
                    bpy.ops.wm.bunkereditangle()
                    context.scene.opcdtools.random_amt_topo = 1.0
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "widenbunkerinterior" in item:
                sub_data = item["widenbunkerinterior"]
                bunker_selection_type = sub_data.get(
                    "bunker_selection_type", None)
                bunker_dig_inset = sub_data.get("bunker_dig_inset", None)
                bunker_xy_shift = sub_data.get("bunker_xy_shift", None)
                random_amt_topo = sub_data.get("random_amt_topo", 1.0)

                if (bunker_selection_type is not None) and \
                   (bunker_dig_inset is not None) and \
                   (bunker_xy_shift is not None):
                    context.scene.opcdtools.bunker_selection_type = bunker_selection_type
                    context.scene.opcdtools.bunker_dig_inset = bunker_dig_inset
                    context.scene.opcdtools.bunker_xy_shift = bunker_xy_shift
                    context.scene.opcdtools.random_amt_topo = random_amt_topo
                    bpy.ops.wm.widenbunkerinterior()
                    context.scene.opcdtools.random_amt_topo = 1.0
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "flattenbunker" in item:
                sub_data = item["flattenbunker"]
                bunker_selection_type = sub_data.get(
                    "bunker_selection_type", None)

                if (bunker_selection_type is not None):
                    context.scene.opcdtools.bunker_selection_type = bunker_selection_type
                    bpy.ops.wm.flattenbunker()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "addpotwall" in item:
                sub_data = item["addpotwall"]
                bunker_selection_type = sub_data.get(
                    "bunker_selection_type", None)
                pot_inset = sub_data.get("pot_inset", None)

                if (bunker_selection_type is not None) and \
                   (pot_inset is not None):
                    context.scene.opcdtools.bunker_selection_type = bunker_selection_type
                    context.scene.opcdtools.pot_inset = pot_inset
                    bpy.ops.wm.addpotwall()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "smoothmesh" in item:
                sub_data = item["smoothmesh"]
                smooth_interior = sub_data.get("smooth_interior", None)
                smooth_interior_repeat = sub_data.get(
                    "smooth_interior_repeat", None)
                smooth_interior_inset = sub_data.get(
                    "smooth_interior_inset", None)
                smooth_interior_3d = sub_data.get("smooth_interior_3d", False)

                if (smooth_interior is not None) and \
                   (smooth_interior_repeat is not None) and \
                   (smooth_interior_inset is not None):
                    context.scene.opcdtools.smooth_interior = smooth_interior
                    context.scene.opcdtools.smooth_interior_repeat = smooth_interior_repeat
                    context.scene.opcdtools.smooth_interior_inset = smooth_interior_inset
                    context.scene.opcdtools.smooth_interior_3d = smooth_interior_3d
                    bpy.ops.wm.smoothmesh()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "subdividemesh" in item:
                sub_data = item["subdividemesh"]
                subdivide_inset = sub_data.get("subdivide_inset", None)

                if (subdivide_inset is not None):
                    context.scene.opcdtools.subdivide_inset = subdivide_inset
                    bpy.ops.wm.subdividemesh()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "zshiftmesh" in item:
                sub_data = item["zshiftmesh"]
                z_shift = sub_data.get("z_shift", None)
                inset = sub_data.get("inset", None)
                outset = sub_data.get("outset", None)

                if (z_shift is not None) and \
                   (inset is not None) and \
                   (outset is not None):
                    context.scene.opcdtools.z_shift = z_shift
                    context.scene.opcdtools.tees_flat_inset = inset
                    context.scene.opcdtools.tees_flat_outset = outset
                    bpy.ops.wm.zshiftmesh()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "fillvertexpaint" in item:
                sub_data = item["fillvertexpaint"]
                vertex_paint_type = sub_data.get("vertex_paint_type", None)
                paint_strength = sub_data.get("paint_strength", None)
                paint_loop_inset = sub_data.get("paint_loop_inset", None)
                random_amt = sub_data.get("random_amt", None)

                if (vertex_paint_type is not None) and \
                   (paint_strength is not None) and \
                   (paint_loop_inset is not None) and \
                   (random_amt is not None):
                    context.scene.opcdtools.vertex_paint_type = vertex_paint_type
                    context.scene.opcdtools.paint_strength = paint_strength
                    context.scene.opcdtools.paint_loop_inset = paint_loop_inset
                    context.scene.opcdtools.random_amt = random_amt
                    bpy.ops.wm.fillvertexpaint()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "randomvertexpaintloop" in item:
                sub_data = item["randomvertexpaintloop"]
                vertex_paint_type = sub_data.get("vertex_paint_type", None)
                paint_strength = sub_data.get("paint_strength", None)
                paint_loop_inset = sub_data.get("paint_loop_inset", None)
                random_amt = sub_data.get("random_amt", None)
                skip_longest_loop = sub_data.get("skip_longest_loop", None)

                if (vertex_paint_type is not None) and \
                   (paint_strength is not None) and \
                   (paint_loop_inset is not None) and \
                   (random_amt is not None) and \
                   (skip_longest_loop is not None):
                    context.scene.opcdtools.vertex_paint_type = vertex_paint_type
                    context.scene.opcdtools.paint_strength = paint_strength
                    context.scene.opcdtools.paint_loop_inset = paint_loop_inset
                    context.scene.opcdtools.random_amt = random_amt
                    context.scene.opcdtools.skip_longest_loop = skip_longest_loop
                    bpy.ops.wm.randomvertexpaintloop()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "growcolor" in item:
                sub_data = item["growcolor"]
                vertex_paint_type = sub_data.get("vertex_paint_type", None)
                paint_strength = sub_data.get("paint_strength", None)
                random_amt = sub_data.get("random_amt", None)
                grow_mode = sub_data.get("grow_mode", None)
                grow_repeat = sub_data.get("grow_repeat", None)
                grow_strict = sub_data.get("strict", False)

                if (vertex_paint_type is not None) and \
                   (paint_strength is not None) and \
                   (random_amt is not None) and \
                   (grow_mode is not None) and \
                   (grow_repeat is not None):
                    context.scene.opcdtools.vertex_paint_type = vertex_paint_type
                    context.scene.opcdtools.paint_strength = paint_strength
                    context.scene.opcdtools.random_amt = random_amt
                    context.scene.opcdtools.grow_mode = grow_mode
                    context.scene.opcdtools.grow_repeat = grow_repeat
                    context.scene.opcdtools.grow_strict = grow_strict
                    bpy.ops.wm.growcolor()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "slopevertexpaint" in item:
                sub_data = item["slopevertexpaint"]
                vertex_paint_type = sub_data.get("vertex_paint_type", None)
                paint_strength = sub_data.get("paint_strength", None)

                if (vertex_paint_type is not None) and (paint_strength is not None):
                    context.scene.opcdtools.vertex_paint_type = vertex_paint_type
                    context.scene.opcdtools.paint_strength = paint_strength
                    bpy.ops.wm.slopevertexpaint()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "changecolors" in item:
                sub_data = item["changecolors"]
                vertex_paint_type_to = sub_data.get(
                    "vertex_paint_type_to", None)
                vertex_paint_type_from = sub_data.get(
                    "vertex_paint_type_from", None)

                if (vertex_paint_type_to is not None) and (vertex_paint_type_from is not None):
                    context.scene.opcdtools.vertex_paint_type_to = vertex_paint_type_to
                    context.scene.opcdtools.vertex_paint_type_from = vertex_paint_type_from
                    bpy.ops.wm.changecolors()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "swapcolors" in item:
                sub_data = item["swapcolors"]
                vertex_paint_type_to = sub_data.get(
                    "vertex_paint_type_to", None)
                vertex_paint_type_from = sub_data.get(
                    "vertex_paint_type_from", None)

                if (vertex_paint_type_to is not None) and (vertex_paint_type_from is not None):
                    context.scene.opcdtools.vertex_paint_type_to = vertex_paint_type_to
                    context.scene.opcdtools.vertex_paint_type_from = vertex_paint_type_from
                    bpy.ops.wm.swapcolors()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "randomslideloop" in item:
                sub_data = item["randomslideloop"]
                loopslide_inset = sub_data.get("loopslide_inset", None)
                loopslide_random = sub_data.get("loopslide_random", None)
                random_amt_topo = sub_data.get("random_amt_topo", 1.0)

                if (loopslide_inset is not None) and (loopslide_random is not None):
                    context.scene.opcdtools.loopslide_inset = loopslide_inset
                    context.scene.opcdtools.loopslide_random = loopslide_random
                    context.scene.opcdtools.random_amt_topo = random_amt_topo
                    bpy.ops.wm.randomslideloop()
                    context.scene.opcdtools.random_amt_topo = 1.0
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "straightenloops" in item:
                sub_data = item["straightenloops"]
                loopslide_inset = sub_data.get("loopslide_inset", None)
                loopslide_amount = sub_data.get("loopslide_amount", None)

                if (loopslide_inset is not None) and (loopslide_amount is not None):
                    context.scene.opcdtools.loopslide_inset = loopslide_inset
                    context.scene.opcdtools.loopslide_amount = loopslide_amount
                    bpy.ops.wm.straightenloops()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "spaceloops" in item:
                sub_data = item["spaceloops"]
                loopslide_inset = sub_data.get("loopslide_inset", None)
                loopslide_amount = sub_data.get("loopslide_amount", None)

                if (loopslide_inset is not None) and (loopslide_amount is not None):
                    context.scene.opcdtools.loopslide_inset = loopslide_inset
                    context.scene.opcdtools.loopslide_amount = loopslide_amount
                    bpy.ops.wm.spaceloops()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "loopcut" in item:
                sub_data = item["loopcut"]
                loopcut_inset = sub_data.get("loopcut_inset", None)

                if loopcut_inset is not None:
                    context.scene.opcdtools.loopcut_inset = loopcut_inset
                    bpy.ops.wm.loopcut()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "meshblendjoin" in item:
                bpy.ops.wm.meshblendjoin()
            elif "meshblendseparate" in item:
                bpy.ops.wm.meshblendseparate()
            elif "meshblendjoinpermanent" in item:
                bpy.ops.wm.meshblendjoinpermanent()
            elif "separatemesh" in item:
                sub_data = item["separatemesh"]
                separate_mat_selection = sub_data.get(
                    "separate_mat_selection", None)

                if separate_mat_selection is not None:
                    context.scene.opcdtools.separate_mat_selection = separate_mat_selection
                    bpy.ops.wm.separatemesh()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "invertselection" in item:
                bpy.ops.wm.invertselection()
            elif "addblendinset" in item:
                sub_data = item["addblendinset"]
                inset_distance = sub_data.get("inset_distance", None)

                if inset_distance is not None:
                    context.scene.opcdtools.inset_distance = inset_distance
                    bpy.ops.wm.addblendinset()
                    # keeping this out of the UI for now
                    context.scene.opcdtools.inset_distance = 0.12
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "renamematerial" in item:
                sub_data = item["renamematerial"]
                mat_change = sub_data.get("mat_change", None)

                if mat_change is not None:
                    context.scene.opcdtools.mat_change = mat_change
                    bpy.ops.wm.matchange()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            elif "zshiftloop" in item:  # keeping this out of the UI for now
                sub_data = item["zshiftloop"]
                zshiftloop_z_shift = sub_data.get("z_shift", None)
                zshiftloop_inset = sub_data.get("inset", None)
                zshiftloop_onlylongest = sub_data.get(
                    "only_longest_loop", None)

                if (zshiftloop_z_shift is not None) and \
                   (zshiftloop_inset is not None) and \
                   (zshiftloop_onlylongest is not None):
                    context.scene.opcdtools.zshiftloop_z_shift = zshiftloop_z_shift
                    context.scene.opcdtools.zshiftloop_inset = zshiftloop_inset
                    context.scene.opcdtools.zshiftloop_onlylongest = zshiftloop_onlylongest
                    bpy.ops.wm.zshiftloop()
                else:
                    print(f"Invalid parameters: {item}")
                    self.report({'ERROR'}, f"Invalid parameters: {item}")
                    return {'CANCELLED'}
            else:
                print(f"Command not found: {item}")
                self.report({'WARNING'}, f"Command not found: {item}")

        return {'FINISHED'}


class WM_OT_createEmptyJson(Operator, ExportHelper):
    """Create or replace a JSON file with an empty JSON structure"""
    bl_idname = "wm.create_empty_json"
    bl_label = "Create Empty JSON"
    bl_options = {'REGISTER', 'UNDO'}

    # Allow only JSON files
    filename_ext = ".json"
    filter_glob: StringProperty(
        default="*.json",
        options={'HIDDEN'},
    )

    def execute(self, context):
        try:
            # Write an empty JSON object to the file
            with open(self.filepath, 'w') as json_file:
                json.dump({}, json_file, indent=4)

            self.report({'INFO'}, f"Created empty JSON file: {self.filepath}")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to create empty JSON file: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


classes = [opcdtoolsSettings, menutoolsSettings, VIEW3D_PT_header, WM_OT_importTerrain, WM_OT_importOuter, WM_OT_projectMesh, WM_OT_projectMeshEdit, WM_OT_bunkerEdit, WM_OT_bunkerEditLip, WM_OT_bunkerEditAngle, WM_OT_widenBunkerInterior, WM_OT_addpotwall, WM_OT_waterEdit, WM_OT_paintbunker, WM_OT_paintwater, WM_OT_flattenbunker, WM_OT_flattenwaterbase, WM_OT_flattenTees, WM_OT_smoothcartpaths, WM_OT_cutcartpaths, WM_OT_addCurbs, WM_OT_addbulkheads_inner, WM_OT_flipdirection, WM_OT_stakeandropes, WM_OT_addwaterplane, WM_OT_Meshjoin, WM_OT_TerrainSmooth, WM_OT_hazardstake, WM_OT_addbridge, WM_OT_bridgenarrow, WM_OT_bridgewiden, WM_OT_removesupports, WM_OT_addstairs, WM_OT_removestairs, WM_OT_rotatestairs, WM_OT_stairsnarrow, WM_OT_stairswiden, WM_OT_addbed, WM_OT_createouter, WM_OT_removeblend, WM_OT_MeshBlendJoinPermanent,
           WM_OT_MeshBlendJoin, WM_OT_MeshBlendSeparate, WM_OT_addblend, WM_OT_addblendinset, WM_OT_separation, WM_OT_invertselection, WM_OT_zshiftloop, WM_OT_normalsdatatransfer, WM_OT_vertexgroupassign, WM_OT_normaltransfervertexgroup, WM_OT_vertexpaint, WM_OT_fillvertexpaint, WM_OT_outervertexpaint, WM_OT_randomvertexpaintloop, WM_OT_randomslideloop, WM_OT_straightenloops, WM_OT_spaceloops, WM_OT_loopcut, WM_OT_randomvertexpaint_editmode, WM_OT_slopevertexpaint, WM_OT_changecolors, WM_OT_growcolor, WM_OT_swapcolors, WM_OT_separateblend, WM_OT_levelmesh, WM_OT_smoothmesh, WM_OT_subdividemesh, WM_OT_ZShiftMesh, WM_OT_topoedit, WM_OT_matselection, WM_OT_blendselection, WM_OT_storemesh, WM_OT_restoremesh, WM_OT_matchange, WM_OT_applymod, WM_OT_area_debug, WM_OT_meshcut, WM_OT_ClearMeshes, SelectedExporter, BatchExporter, WM_OT_readoperations, WM_OT_createEmptyJson]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.opcdtools = bpy.props.PointerProperty(
        type=opcdtoolsSettings)
    bpy.types.WindowManager.menutools = PointerProperty(type=menutoolsSettings)


def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.opcdtools
    del bpy.types.WindowManager.menutools


if __name__ == "__main__":
    register()
