"""Cloud & Local PC Desktop Controller facade (`cloud_desktop` & `dsh_workspace`).

Split cleanly into domain mixins while keeping full backward compatibility:
1. `core.cloud_desktop_controller.CloudDesktopControllerMixin`
2. `core.dsh_bridge_mixin.DshBridgeMixin`
3. `core.pc_desktop_controller.PcDesktopControllerMixin`
"""
from __future__ import annotations

from assistants import get_assistant
from core.cloud_desktop_controller import CloudDesktopControllerMixin
from core.dsh_bridge_mixin import DshBridgeMixin
from core.pc_desktop_controller import PcDesktopControllerMixin
from core.vision_engine import (
    annotate_tap_crosshair,
    draw_vision_ruler_overlay,
    enrich_cloud_desktop_vision,
    zoom_region_from_args,
)
from storage import node_display_name, normalize_node_source


class DesktopControllerMixin(
    CloudDesktopControllerMixin,
    DshBridgeMixin,
    PcDesktopControllerMixin,
):
    pass


__all__ = [
    'DesktopControllerMixin',
    'CloudDesktopControllerMixin',
    'DshBridgeMixin',
    'PcDesktopControllerMixin',
    'annotate_tap_crosshair',
    'draw_vision_ruler_overlay',
    'enrich_cloud_desktop_vision',
    'zoom_region_from_args',
    'get_assistant',
    'node_display_name',
    'normalize_node_source',
]
