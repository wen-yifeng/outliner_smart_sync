bl_info = {
    "name": "大纲切换显隐",
    "author": "一枫",
    "version": (2, 0, 1),
    "blender": (3, 0, 0),
    "category": "Outliner",
    "description": "在大纲视图中快速同步切换对象/集合的视图显隐、渲染显隐和集合排除状态",
}

import bpy
from bpy.types import Operator, AddonPreferences
from bpy.props import BoolProperty, EnumProperty


# ============================================================
# 基础配置
# ============================================================

ADDON_ID = __package__ if __package__ else __name__

addon_keymaps = []


# ============================================================
# 工具函数
# ============================================================

def get_addon_prefs():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


def get_pref(prefs, attr, default):
    if not prefs:
        return default
    return getattr(prefs, attr, default)


def get_selected_ids_safe(context):
    selected = getattr(context, "selected_ids", None)
    if not selected:
        return []
    return list(selected)


def is_object(item):
    return isinstance(item, bpy.types.Object)


def is_collection(item):
    return isinstance(item, bpy.types.Collection)


def iter_layer_collections(layer_collection):
    yield layer_collection
    for child in layer_collection.children:
        yield from iter_layer_collections(child)


def build_collection_lc_index(view_layer):
    """
    构建 Collection -> [LayerCollection] 索引。
    同一个 Collection 可能在当前 ViewLayer 中出现多次。
    """
    index = {}

    if not view_layer or not view_layer.layer_collection:
        return index

    for lc in iter_layer_collections(view_layer.layer_collection):
        col = lc.collection
        if col:
            index.setdefault(col.as_pointer(), []).append(lc)

    return index


def get_layer_collections_for_collection(col_lc_index, collection, affect_all_instances=True):
    if not collection:
        return []

    lcs = col_lc_index.get(collection.as_pointer(), [])

    if not affect_all_instances and lcs:
        return [lcs[0]]

    return lcs


def get_child_collections_recursive(collection):
    result = []
    if not collection:
        return result

    stack = list(reversed(collection.children))
    while stack:
        child = stack.pop()
        result.append(child)
        stack.extend(reversed(child.children))

    return result


def expand_collections_from_items(items, recursive=False):
    """
    从选中项中提取 Collection。
    recursive=True 时递归处理子集合。
    """
    result = []
    seen = set()

    for item in items:
        if not is_collection(item):
            continue

        ptr = item.as_pointer()
        if ptr not in seen:
            seen.add(ptr)
            result.append(item)

        if recursive:
            for child in get_child_collections_recursive(item):
                child_ptr = child.as_pointer()
                if child_ptr not in seen:
                    seen.add(child_ptr)
                    result.append(child)

    return result


def first_actionable_item(items, allow_object=True, allow_collection=True):
    for item in items:
        if allow_object and is_object(item):
            return item
        if allow_collection and is_collection(item):
            return item
    return None


def safe_set_object_viewport_hidden(obj, hidden, view_layer):
    try:
        current = obj.hide_get(view_layer=view_layer)
    except TypeError:
        current = obj.hide_get()
    except Exception:
        return -1

    if current == hidden:
        return 0

    try:
        try:
            obj.hide_set(hidden, view_layer=view_layer)
        except TypeError:
            obj.hide_set(hidden)
        return 1
    except Exception:
        return -1


def safe_set_object_render_hidden(obj, hidden):
    if not hasattr(obj, "hide_render"):
        return -1

    try:
        current = obj.hide_render
        if current == hidden:
            return 0

        obj.hide_render = hidden
        return 1
    except Exception:
        return -1


def safe_set_collection_render_hidden(collection, hidden):
    if not hasattr(collection, "hide_render"):
        return -1

    try:
        current = collection.hide_render
        if current == hidden:
            return 0

        collection.hide_render = hidden
        return 1
    except Exception:
        return -1


def safe_set_layer_collection_viewport_hidden(lc, hidden):
    try:
        current = lc.hide_viewport
        if current == hidden:
            return 0

        lc.hide_viewport = hidden
        return 1
    except Exception:
        return -1


def safe_set_layer_collection_exclude(lc, excluded):
    try:
        current = lc.exclude
        if current == excluded:
            return 0

        lc.exclude = excluded
        return 1
    except Exception:
        return -1


def object_is_viewport_visible(obj, view_layer):
    try:
        return not obj.hide_get(view_layer=view_layer)
    except TypeError:
        return not obj.hide_get()
    except Exception:
        return False


def collection_is_viewport_visible(collection, col_lc_index, affect_all_instances=True):
    """
    只要有一个 LayerCollection 没有 hide_viewport，就认为集合当前可见。
    如果该集合不在当前 ViewLayer 中，返回 None（无法判断）。
    """
    lcs = get_layer_collections_for_collection(
        col_lc_index,
        collection,
        affect_all_instances=affect_all_instances
    )

    if not lcs:
        return None

    for lc in lcs:
        try:
            if not lc.hide_viewport:
                return True
        except Exception:
            pass

    return False


def object_is_render_visible(obj):
    try:
        return not obj.hide_render
    except Exception:
        return False


def collection_is_render_visible(collection):
    try:
        return not collection.hide_render
    except Exception:
        return False


def collection_is_excluded(collection, col_lc_index, affect_all_instances=True):
    """
    如果所有相关 LayerCollection 都被 exclude，则认为该集合被排除。
    如果该集合不在当前 ViewLayer 中，返回 None（无法判断）。
    """
    lcs = get_layer_collections_for_collection(
        col_lc_index,
        collection,
        affect_all_instances=affect_all_instances
    )

    if not lcs:
        return None

    for lc in lcs:
        try:
            if not lc.exclude:
                return False
        except Exception:
            pass

    return True


def redraw_outliner(context):
    if context.area:
        context.area.tag_redraw()


def maybe_report(operator, level, message):
    if not message:
        return
    try:
        operator.report(level, message)
    except Exception:
        pass


# ============================================================
# 偏好设置
# ============================================================

class SMARTSYNC_AddonPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    toggle_mode_e: EnumProperty(
        name="E 键模式",
        description="视图显隐的切换方式",
        items=[
            ("UNIFY", "统一模式", "根据第一个可处理项状态统一切换"),
            ("INVERT", "逐项反转", "每个项目按自身当前状态反转"),
        ],
        default="UNIFY"
    )

    toggle_mode_r: EnumProperty(
        name="R 键模式",
        description="渲染显隐的切换方式",
        items=[
            ("UNIFY", "统一模式", "根据第一个可处理项状态统一切换"),
            ("INVERT", "逐项反转", "每个项目按自身当前状态反转"),
        ],
        default="UNIFY"
    )

    toggle_mode_d: EnumProperty(
        name="D 键模式",
        description="集合排除的切换方式",
        items=[
            ("UNIFY", "统一模式", "根据第一个选中集合状态统一切换"),
            ("INVERT", "逐项反转", "每个集合按自身当前状态反转"),
        ],
        default="UNIFY"
    )

    toggle_mode_f: EnumProperty(
        name="F 键模式",
        description="视图 + 渲染的切换方式",
        items=[
            ("UNIFY", "统一模式", "根据第一个可处理项状态统一切换"),
            ("INVERT", "逐项反转", "每个项目按自身当前状态反转"),
        ],
        default="UNIFY"
    )

    recursive_collections: BoolProperty(
        name="递归处理子集合",
        description="当选中集合时，同时处理所有子集合",
        default=False
    )

    affect_all_collection_instances: BoolProperty(
        name="处理集合的所有实例",
        description="同一个 Collection 在当前 ViewLayer 中出现多次时，同时处理所有 LayerCollection 实例",
        default=True
    )

    def draw(self, context):
        layout = self.layout

        layout.label(text="大纲切换显隐 Smart Sync Pro")

        box = layout.box()
        box.label(text="快捷键模式")
        box.prop(self, "toggle_mode_e", text="E：视图显隐")
        box.prop(self, "toggle_mode_r", text="R：渲染显隐")
        box.prop(self, "toggle_mode_d", text="D：集合排除")
        box.prop(self, "toggle_mode_f", text="F：视图 + 渲染")

        box = layout.box()
        box.label(text="集合处理")
        box.prop(self, "recursive_collections")
        box.prop(self, "affect_all_collection_instances")

        box = layout.box()
        box.label(text="快捷键")
        box.label(text="E：切换视图显隐")
        box.label(text="R：切换渲染显隐")
        box.label(text="D：切换集合排除")
        box.label(text="F：同时切换视图与渲染显隐")


# ============================================================
# 核心操作函数
# ============================================================

def apply_viewport_state(
    items,
    view_layer,
    target_hide=None,
    invert_each=False,
    recursive_collections=False,
    affect_all_instances=True,
    col_lc_index=None,
    collections=None,
):
    obj_total = 0
    obj_changed = 0
    col_total = 0
    col_changed = 0
    failed = 0

    if col_lc_index is None:
        col_lc_index = build_collection_lc_index(view_layer)

    for item in items:
        if not is_object(item):
            continue

        obj_total += 1

        if invert_each:
            hidden = object_is_viewport_visible(item, view_layer)
        else:
            hidden = target_hide

        result = safe_set_object_viewport_hidden(item, hidden, view_layer)

        if result == 1:
            obj_changed += 1
        elif result == -1:
            failed += 1

    if collections is None:
        collections = expand_collections_from_items(items, recursive=recursive_collections)

    for col in collections:
        lcs = get_layer_collections_for_collection(
            col_lc_index,
            col,
            affect_all_instances=affect_all_instances
        )

        if not lcs:
            continue

        col_total += 1
        changed_this_collection = False

        for lc in lcs:
            if invert_each:
                try:
                    hidden = not lc.hide_viewport
                except Exception:
                    failed += 1
                    continue
            else:
                hidden = target_hide

            result = safe_set_layer_collection_viewport_hidden(lc, hidden)

            if result == 1:
                changed_this_collection = True
            elif result == -1:
                failed += 1

        if changed_this_collection:
            col_changed += 1

    return {
        "obj_total": obj_total,
        "obj_changed": obj_changed,
        "col_total": col_total,
        "col_changed": col_changed,
        "failed": failed,
    }


def apply_render_state(
    items,
    target_hide=None,
    invert_each=False,
    recursive_collections=False,
    collections=None,
):
    obj_total = 0
    obj_changed = 0
    col_total = 0
    col_changed = 0
    failed = 0

    for item in items:
        if not is_object(item):
            continue

        if not hasattr(item, "hide_render"):
            continue

        obj_total += 1

        if invert_each:
            hidden = object_is_render_visible(item)
        else:
            hidden = target_hide

        result = safe_set_object_render_hidden(item, hidden)

        if result == 1:
            obj_changed += 1
        elif result == -1:
            failed += 1

    if collections is None:
        collections = expand_collections_from_items(items, recursive=recursive_collections)

    for col in collections:
        if not hasattr(col, "hide_render"):
            continue

        col_total += 1

        if invert_each:
            hidden = collection_is_render_visible(col)
        else:
            hidden = target_hide

        result = safe_set_collection_render_hidden(col, hidden)

        if result == 1:
            col_changed += 1
        elif result == -1:
            failed += 1

    return {
        "obj_total": obj_total,
        "obj_changed": obj_changed,
        "col_total": col_total,
        "col_changed": col_changed,
        "failed": failed,
    }


def apply_exclude_state(
    collections,
    view_layer,
    target_exclude=None,
    invert_each=False,
    recursive_collections=False,
    affect_all_instances=True,
    col_lc_index=None,
):
    col_total = 0
    col_changed = 0
    failed = 0

    if col_lc_index is None:
        col_lc_index = build_collection_lc_index(view_layer)
    expanded = expand_collections_from_items(collections, recursive=recursive_collections)

    for col in expanded:
        lcs = get_layer_collections_for_collection(
            col_lc_index,
            col,
            affect_all_instances=affect_all_instances
        )

        if not lcs:
            continue

        col_total += 1
        changed_this_collection = False

        for lc in lcs:
            if invert_each:
                try:
                    excluded = not lc.exclude
                except Exception:
                    failed += 1
                    continue
            else:
                excluded = target_exclude

            result = safe_set_layer_collection_exclude(lc, excluded)

            if result == 1:
                changed_this_collection = True
            elif result == -1:
                failed += 1

        if changed_this_collection:
            col_changed += 1

    return {
        "col_total": col_total,
        "col_changed": col_changed,
        "failed": failed,
    }


def get_unify_target_for_viewport(items, view_layer, affect_all_instances=True, col_lc_index=None):
    ref = first_actionable_item(items, allow_object=True, allow_collection=True)

    if ref is None:
        return None

    if is_object(ref):
        return object_is_viewport_visible(ref, view_layer)

    if is_collection(ref):
        if col_lc_index is None:
            col_lc_index = build_collection_lc_index(view_layer)
        return collection_is_viewport_visible(
            ref,
            col_lc_index,
            affect_all_instances=affect_all_instances
        )

    return None


def get_unify_target_for_render(items):
    ref = first_actionable_item(items, allow_object=True, allow_collection=True)

    if ref is None:
        return None

    if is_object(ref):
        return object_is_render_visible(ref)

    if is_collection(ref):
        return collection_is_render_visible(ref)

    return None


def get_unify_target_for_exclude(collections, view_layer, affect_all_instances=True, col_lc_index=None):
    ref = first_actionable_item(collections, allow_object=False, allow_collection=True)

    if ref is None:
        return None

    if col_lc_index is None:
        col_lc_index = build_collection_lc_index(view_layer)

    currently_excluded = collection_is_excluded(
        ref,
        col_lc_index,
        affect_all_instances=affect_all_instances
    )

    if currently_excluded is None:
        return None

    return not currently_excluded


# ============================================================
# Operators
# ============================================================

class OUTLINER_OT_toggle_viewport(Operator):
    bl_idname = "outliner.smart_sync_toggle_viewport"
    bl_label = "切换视图显隐"
    bl_description = "切换选中对象或集合的视图显隐状态"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == "OUTLINER"

    def execute(self, context):
        selected = get_selected_ids_safe(context)

        if not selected:
            maybe_report(self, {"WARNING"}, "未选中任何对象或集合")
            return {"CANCELLED"}

        prefs = get_addon_prefs()
        mode = get_pref(prefs, "toggle_mode_e", "UNIFY")
        recursive = get_pref(prefs, "recursive_collections", False)
        affect_all_instances = get_pref(prefs, "affect_all_collection_instances", True)

        col_lc_index = build_collection_lc_index(context.view_layer)

        if mode == "INVERT":
            apply_viewport_state(
                selected,
                context.view_layer,
                invert_each=True,
                recursive_collections=recursive,
                affect_all_instances=affect_all_instances,
                col_lc_index=col_lc_index,
            )
        else:
            target_hide = get_unify_target_for_viewport(
                selected,
                context.view_layer,
                affect_all_instances=affect_all_instances,
                col_lc_index=col_lc_index,
            )

            if target_hide is None:
                maybe_report(self, {"WARNING"}, "当前选择中没有可处理的对象或集合")
                return {"CANCELLED"}

            apply_viewport_state(
                selected,
                context.view_layer,
                target_hide=target_hide,
                invert_each=False,
                recursive_collections=recursive,
                affect_all_instances=affect_all_instances,
                col_lc_index=col_lc_index,
            )

        redraw_outliner(context)
        return {"FINISHED"}


class OUTLINER_OT_toggle_render(Operator):
    bl_idname = "outliner.smart_sync_toggle_render"
    bl_label = "切换渲染显隐"
    bl_description = "切换选中对象或集合的渲染显隐状态"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == "OUTLINER"

    def execute(self, context):
        selected = get_selected_ids_safe(context)

        if not selected:
            maybe_report(self, {"WARNING"}, "未选中任何对象或集合")
            return {"CANCELLED"}

        prefs = get_addon_prefs()
        mode = get_pref(prefs, "toggle_mode_r", "UNIFY")
        recursive = get_pref(prefs, "recursive_collections", False)

        if mode == "INVERT":
            apply_render_state(
                selected,
                invert_each=True,
                recursive_collections=recursive,
            )
        else:
            target_hide = get_unify_target_for_render(selected)

            if target_hide is None:
                maybe_report(self, {"WARNING"}, "当前选择中没有可处理的对象或集合")
                return {"CANCELLED"}

            apply_render_state(
                selected,
                target_hide=target_hide,
                invert_each=False,
                recursive_collections=recursive,
            )

        redraw_outliner(context)
        return {"FINISHED"}


class OUTLINER_OT_toggle_both(Operator):
    bl_idname = "outliner.smart_sync_toggle_both"
    bl_label = "切换视图与渲染显隐"
    bl_description = "同时切换选中对象或集合的视图显隐和渲染显隐状态"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == "OUTLINER"

    def execute(self, context):
        selected = get_selected_ids_safe(context)

        if not selected:
            maybe_report(self, {"WARNING"}, "未选中任何对象或集合")
            return {"CANCELLED"}

        prefs = get_addon_prefs()
        mode = get_pref(prefs, "toggle_mode_f", "UNIFY")
        recursive = get_pref(prefs, "recursive_collections", False)
        affect_all_instances = get_pref(prefs, "affect_all_collection_instances", True)

        col_lc_index = build_collection_lc_index(context.view_layer)
        expanded_collections = expand_collections_from_items(selected, recursive=recursive)

        if mode == "INVERT":
            apply_viewport_state(
                selected,
                context.view_layer,
                invert_each=True,
                recursive_collections=recursive,
                affect_all_instances=affect_all_instances,
                col_lc_index=col_lc_index,
                collections=expanded_collections,
            )

            apply_render_state(
                selected,
                invert_each=True,
                recursive_collections=recursive,
                collections=expanded_collections,
            )
        else:
            target_hide = get_unify_target_for_viewport(
                selected,
                context.view_layer,
                affect_all_instances=affect_all_instances,
                col_lc_index=col_lc_index,
            )

            if target_hide is None:
                maybe_report(self, {"WARNING"}, "当前选择中没有可处理的对象或集合")
                return {"CANCELLED"}

            apply_viewport_state(
                selected,
                context.view_layer,
                target_hide=target_hide,
                invert_each=False,
                recursive_collections=recursive,
                affect_all_instances=affect_all_instances,
                col_lc_index=col_lc_index,
                collections=expanded_collections,
            )

            apply_render_state(
                selected,
                target_hide=target_hide,
                invert_each=False,
                recursive_collections=recursive,
                collections=expanded_collections,
            )

        redraw_outliner(context)
        return {"FINISHED"}


class OUTLINER_OT_toggle_collection_exclude(Operator):
    bl_idname = "outliner.smart_sync_toggle_collection_exclude"
    bl_label = "切换集合排除"
    bl_description = "切换选中集合在当前 ViewLayer 中的排除状态"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == "OUTLINER"

    def execute(self, context):
        selected = get_selected_ids_safe(context)
        selected_cols = [item for item in selected if is_collection(item)]

        if not selected_cols:
            maybe_report(self, {"WARNING"}, "未选中任何集合")
            return {"CANCELLED"}

        prefs = get_addon_prefs()
        mode = get_pref(prefs, "toggle_mode_d", "UNIFY")
        recursive = get_pref(prefs, "recursive_collections", False)
        affect_all_instances = get_pref(prefs, "affect_all_collection_instances", True)

        col_lc_index = build_collection_lc_index(context.view_layer)

        if mode == "INVERT":
            apply_exclude_state(
                selected_cols,
                context.view_layer,
                invert_each=True,
                recursive_collections=recursive,
                affect_all_instances=affect_all_instances,
                col_lc_index=col_lc_index,
            )
        else:
            target_exclude = get_unify_target_for_exclude(
                selected_cols,
                context.view_layer,
                affect_all_instances=affect_all_instances,
                col_lc_index=col_lc_index,
            )

            if target_exclude is None:
                maybe_report(self, {"WARNING"}, "当前选择中没有可处理的集合")
                return {"CANCELLED"}

            apply_exclude_state(
                selected_cols,
                context.view_layer,
                target_exclude=target_exclude,
                invert_each=False,
                recursive_collections=recursive,
                affect_all_instances=affect_all_instances,
                col_lc_index=col_lc_index,
            )

        redraw_outliner(context)
        return {"FINISHED"}


# ============================================================
# Keymaps
# ============================================================

def register_keymaps():
    wm = bpy.context.window_manager
    if not wm:
        return

    kc = wm.keyconfigs.addon
    if not kc:
        return

    km = kc.keymaps.get("Outliner")
    if not km:
        km = kc.keymaps.new(name="Outliner", space_type="OUTLINER")

    kmi_defs = [
        (OUTLINER_OT_toggle_viewport.bl_idname, "E"),
        (OUTLINER_OT_toggle_render.bl_idname, "R"),
        (OUTLINER_OT_toggle_collection_exclude.bl_idname, "D"),
        (OUTLINER_OT_toggle_both.bl_idname, "F"),
    ]

    for idname, key in kmi_defs:
        exists = any(
            kmi.idname == idname and kmi.type == key and kmi.value == "PRESS"
            for kmi in km.keymap_items
        )

        if not exists:
            kmi = km.keymap_items.new(idname, key, "PRESS")
            addon_keymaps.append((km, kmi))


def unregister_keymaps():
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass

    addon_keymaps.clear()


# ============================================================
# 注册
# ============================================================

classes = (
    SMARTSYNC_AddonPreferences,
    OUTLINER_OT_toggle_viewport,
    OUTLINER_OT_toggle_render,
    OUTLINER_OT_toggle_both,
    OUTLINER_OT_toggle_collection_exclude,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    register_keymaps()


def unregister():
    unregister_keymaps()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
