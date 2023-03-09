try:
    from pathlib import Path
    from threading import Timer
    from time import time
    from typing import Any, Callable, Optional

    import klayout.lay as lay
    from ipyevents import Event  # type: ignore[import]
    from IPython.display import clear_output
    from ipytree import Node, Tree  # type: ignore[import]
    from ipywidgets import (  # type: ignore[import]
        HTML,
        Accordion,
        AppLayout,
        Button,
        HBox,
        Image,
        Label,
        Layout,
        Output,
        RadioButtons,
        Tab,
        ToggleButtons,
        VBox,
    )

    from .. import kdb, lay
    from ..config import logger
    from ..kcell import CplxKCell, KCell, KLib

except ImportError as e:
    print("You need install jupyter notebook plugin with `pip install kfactory[ipy]`")
    raise e


class LayoutWidget:
    def __init__(
        self,
        cell: KCell | CplxKCell,
        layer_properties: Optional[str] = None,
        hide_unused_layers: bool = True,
        with_layer_selector: bool = True,
    ):
        # self.debug = Output()

        self.hide_unused_layers = hide_unused_layers

        self.layout_view = lay.LayoutView()
        self.layout_view.show_layout(cell.klib, False)
        self.layer_properties: Optional[Path] = None
        if layer_properties is not None:
            self.layer_properties = Path(layer_properties)
            if self.layer_properties.exists() and self.layer_properties.is_file():
                self.layer_properties = self.layer_properties
                self.layout_view.load_layer_props(str(self.layer_properties))
        self.show_cell(cell)
        png_data = self.layout_view.get_screenshot_pixels().to_png_data()

        self.image = Image(value=png_data, format="png")
        self.refresh()
        scroll_event = Event(source=self.image, watched_events=["wheel"], wait=10)
        scroll_event.on_dom_event(self.on_scroll)

        enter_event = Event(source=self.image, watched_events=["mouseenter"])
        leave_event = Event(source=self.image, watched_events=["mouseleave"])
        enter_event.on_dom_event(self.on_mouse_enter)
        leave_event.on_dom_event(self.on_mouse_leave)

        self.layout_view.on_image_updated_event = self.refresh  # type: ignore[attr-defined]

        mouse_event = Event(
            source=self.image,
            watched_events=[
                "mousedown",
                "mouseup",
                "mousemove",
                "contextmenu",
            ],
            wait=10,
            throttle_or_debounce="debounce",
            prevent_default_action=True,
        )
        mouse_event.on_dom_event(self.on_mouse)

        max_height = self.layout_view.viewport_height()
        if with_layer_selector:
            selector_tabs = self.build_selector(max_height=max_height)
        else:
            selector_tabs = None

        mode_selector = self.build_modes(max_height)

        def switch_mode(buttons: ToggleButtons) -> None:
            self.layout_view.switch_mode(buttons.value)
            self.refresh()

        self.widget = AppLayout(
            left_sidebar=mode_selector,
            center=self.image,
            right_sidebar=selector_tabs,
            align_items="top",
            justify_items="center",
            # footer=self.debug,
            # footer=mode_selector,
            pane_weights=[1, 3, 1],
        )

    def show_cell(self, cell: kdb.Cell) -> None:
        self.layout_view.active_cellview().cell = cell
        self.layout_view.max_hier()
        self.layout_view.resize(800, 600)
        self.layout_view.add_missing_layers()
        self.layout_view.zoom_fit()

    def button_toggle(self, button: Button) -> None:
        button.style.button_color = (
            "transparent"
            if (button.style.button_color == button.default_color)
            else button.default_color
        )

        logger.info("button toggle")
        for props in self.layout_view.each_layer():
            if props == button.layer_props:
                props.visible = not props.visible
                props.name = button.name
                button.layer_props = props
                break
        self.refresh()

    def build_layer_toggle(
        self, prop_iter: lay.LayerPropertiesIterator
    ) -> Optional[HBox]:
        props = prop_iter.current()
        layer_color = f"#{props.eff_fill_color():06x}"
        # Would be nice to use LayoutView.icon_for_layer() rather than simple colored box
        button_layout = Layout(
            width="5px",
            height="20px",
            border=f"solid 2px {layer_color}",
            display="block",
        )

        layer_checkbox = Button(
            style={"button_color": layer_color if props.visible else "transparent"},
            layout=button_layout,
        )
        layer_checkbox.default_color = layer_color
        layer_checkbox.layer_props = props

        if props.has_children():
            prop_iter = prop_iter.down_first_child()
            n_children = prop_iter.num_siblings()
            children = []
            for _i in range(n_children):
                prop_iter = prop_iter.next()
                _layer_toggle = self.build_layer_toggle(prop_iter)
                if _layer_toggle is not None:
                    children.append(_layer_toggle)

            if not children:
                return None
            layer_label = Accordion([VBox(children)], titles=(props.name,))
            layer_checkbox.label = layer_label
            layer_checkbox.name = props.name

        else:
            cell = self.layout_view.active_cellview().cell
            if cell.bbox_per_layer(prop_iter.current().layer_index()).empty():
                return None
            layer_label = (
                Label(props.name)
                if props.name
                else Label(f"{props.source_layer}/{props.source_datatype}")
            )
            layer_checkbox.label = layer_label
            layer_checkbox.name = layer_label.value

        layer_checkbox.on_click(self.button_toggle)
        return HBox([layer_checkbox, layer_label])

    def build_modes(self, max_height: float) -> VBox:
        modes = self.layout_view.mode_names()
        tb = ToggleButtons(options=modes, layout=Layout(width="100px"))

        def switch_mode(mode: dict[str, Any]) -> None:
            self.layout_view.switch_mode(mode["new"])

        tb.observe(switch_mode, "value")
        return tb

    def build_cell_selector(self, cell: kdb.Cell) -> Node:
        child_cells = [
            self.build_cell_selector(
                self.layout_view.active_cellview().layout().cell(_cell)
            )
            for _cell in cell.each_child_cell()
        ]

        node = Node(cell.name, show_icon=False)
        node.observe(self.on_select_cell, "selected")

        for cc in child_cells:
            node.add_node(cc)

        return node

    def build_selector(self, max_height: float) -> Tab:
        """Builds a widget for toggling layer displays.

        Args:
            max_height: Maximum height to set for the widget (likely the height of the pixel buffer).
        """

        all_boxes = []

        prop_iter = self.layout_view.begin_layers()
        while not prop_iter.at_end():
            if layer_toggle := self.build_layer_toggle(prop_iter):
                all_boxes.append(layer_toggle)
            prop_iter.next()

        layout = Layout(
            max_height=f"{max_height}px", overflow_y="auto", display="block"
        )
        selector = VBox(all_boxes, layout=layout)

        cells: list[RadioButtons | Accordion] = [
            self.build_cell_selector(cell)
            for cell in self.layout_view.active_cellview().layout().top_cells()
        ]
        tree = Tree(cells, multiple_selection=False, stripes=True)

        # For when tabs are implemented
        selector_tabs = Tab([selector, tree])
        selector_tabs.set_title(0, "Layers")
        selector_tabs.set_title(1, "Cells")
        # selector_tabs.titles = ("Layers",)

        return selector_tabs

    def load_layout(self, filepath: str, layer_properties: Optional[str]) -> None:
        """Loads a GDS layout.

        Args:
            filepath: path for the GDS layout.
            layer_properties: Optional path for the layer_properties klayout file (lyp).
        """
        self.layout_view.load_layout(filepath)
        self.layout_view.max_hier()
        if layer_properties:
            self.layout_view.load_layer_props(layer_properties)

    def refresh(self) -> None:
        self.layout_view.timer()  # type: ignore[attr-defined]
        png_data = self.layout_view.get_screenshot_pixels().to_png_data()
        self.image.value = png_data
        self.layout_view.timer()  # type: ignore[attr-defined]

    def _get_modifier_buttons(self, event: Event) -> int:
        shift = event["shiftKey"]
        alt = event["altKey"]
        ctrl = event["ctrlKey"]
        # meta = event["metaKey"]

        mouse_buttons = event["buttons"]

        buttons = 0
        if shift:
            buttons |= lay.ButtonState.ShiftKey
        if alt:
            buttons |= lay.ButtonState.AltKey
        if ctrl:
            buttons |= lay.ButtonState.ControlKey

        if mouse_buttons & 1:
            buttons |= lay.ButtonState.LeftButton
        if mouse_buttons & 2:
            buttons |= lay.ButtonState.RightButton
        if mouse_buttons & 4:
            buttons |= lay.ButtonState.MidButton

        return buttons

    def on_select_cell(self, event: Event) -> None:
        self.show_cell(
            self.layout_view.active_cellview().layout().cell(event["owner"].name)
        )

        max_height = self.layout_view.viewport_height()

        all_boxes = []
        prop_iter = self.layout_view.begin_layers()
        while not prop_iter.at_end():
            if layer_toggle := self.build_layer_toggle(prop_iter):
                all_boxes.append(layer_toggle)
            prop_iter.next()

        tabs = self.widget.right_sidebar
        vbox = tabs.children[0]
        vbox.children = all_boxes
        self.refresh()

    def on_scroll(self, event: Event) -> None:
        self.layout_view.timer()  # type: ignore[attr-defined]
        delta = int(event["deltaY"])
        x = event["relativeX"]
        y = event["relativeY"]
        buttons = self._get_modifier_buttons(event)
        self.layout_view.send_wheel_event(-delta, False, kdb.DPoint(x, y), buttons)
        self.refresh()

    def on_mouse(self, event: Event) -> None:
        self.layout_view.timer()  # type: ignore[attr-defined]
        x = event["relativeX"]
        y = event["relativeY"]
        moved_x = event["movementX"]
        moved_y = event["movementY"]
        buttons = self._get_modifier_buttons(event)

        if event["event"] == "mousedown":
            self.layout_view.send_mouse_press_event(
                kdb.DPoint(float(x), float(y)), buttons
            )
        elif event["event"] == "mouseup":
            self.layout_view.send_mouse_release_event(
                kdb.DPoint(float(x), float(y)), buttons
            )
        elif event["event"] == "mousemove":
            self.layout_view.send_mouse_move_event(
                kdb.DPoint(float(x), float(y)), buttons
            )
        self.refresh()
        self.layout_view.timer()  # type: ignore[attr-defined]

    def on_mouse_enter(self, event: Event) -> None:
        self.layout_view.timer()  # type: ignore[attr-defined]
        self.layout_view.send_enter_event()
        self.refresh()

    def on_mouse_leave(self, event: Event) -> None:
        self.layout_view.timer()  # type: ignore[attr-defined]
        self.layout_view.send_leave_event()
        self.refresh()