from copy import deepcopy

import gradio as gr
import pandas as pd
import yaml
from ktem.app import BasePage

from .manager import embeddings


def format_description(cls):
    params = cls.describe()["params"]
    params_lines = ["| Name | Type | Description |", "| --- | --- | --- |"]
    for key, value in params.items():
        if isinstance(value["auto_callback"], str):
            continue
        params_lines.append(f"| {key} | {value['type']} | {value['help']} |")
    return f"{cls.__doc__}\n\n" + "\n".join(params_lines)


class EmbeddingManagement(BasePage):
    def __init__(self, app):
        self._app = app
        self.spec_desc_default = (
            "# Spec description\n\nSelect a model to view the spec description."
        )
        self.on_building_ui()

    def on_building_ui(self):
        with gr.Tab(label="View"):
            self.emb_list = gr.DataFrame(
                headers=["name", "vendor", "default"],
                interactive=False,
            )

            with gr.Column(visible=False) as self._selected_panel:
                self.selected_emb_name = gr.Textbox(value="", visible=False)
                with gr.Row():
                    with gr.Column():
                        self.edit_default = gr.Checkbox(
                            label="Set default",
                            info=(
                                "Set this Embedding model as default. This default "
                                "Embedding will be used by other components by default "
                                "if no Embedding is specified for such components."
                            ),
                        )
                        self.edit_spec = gr.Textbox(
                            label="Specification",
                            info="Specification of the Embedding model in YAML format",
                            lines=10,
                        )

                        with gr.Row(visible=False) as self._selected_panel_btn:
                            with gr.Column():
                                self.btn_edit_save = gr.Button(
                                    "Save", min_width=10, variant="primary"
                                )
                            with gr.Column():
                                self.btn_delete = gr.Button(
                                    "Delete", min_width=10, variant="stop"
                                )
                                with gr.Row():
                                    self.btn_delete_yes = gr.Button(
                                        "Confirm Delete",
                                        variant="stop",
                                        visible=False,
                                        min_width=10,
                                    )
                                    self.btn_delete_no = gr.Button(
                                        "Cancel", visible=False, min_width=10
                                    )
                            with gr.Column():
                                self.btn_close = gr.Button("Close", min_width=10)

                    with gr.Column():
                        self.edit_spec_desc = gr.Markdown("# Spec description")

        with gr.Tab(label="Add"):
            with gr.Row():
                with gr.Column(scale=2):
                    self.name = gr.Textbox(
                        label="Name",
                        info=(
                            "Must be unique and non-empty. "
                            "The name will be used to identify the embedding model."
                        ),
                    )
                    self.emb_choices = gr.Dropdown(
                        label="Vendors",
                        info=(
                            "Choose the vendor of the Embedding model. Each vendor "
                            "has different specification."
                        ),
                    )
                    self.spec = gr.Textbox(
                        label="Specification",
                        info="Specification of the Embedding model in YAML format.",
                    )
                    self.default = gr.Checkbox(
                        label="Set default",
                        info=(
                            "Set this Embedding model as default. This default "
                            "Embedding will be used by other components by default "
                            "if no Embedding is specified for such components."
                        ),
                    )
                    self.btn_new = gr.Button("Add", variant="primary")

                with gr.Column(scale=3):
                    self.spec_desc = gr.Markdown(self.spec_desc_default)

    def _on_app_created(self):
        """Called when the app is created"""
        self._app.app.load(
            self.list_embeddings,
            inputs=None,
            outputs=[self.emb_list],
        )
        self._app.app.load(
            lambda: gr.update(choices=list(embeddings.vendors().keys())),
            outputs=[self.emb_choices],
        )

    def on_emb_vendor_change(self, vendor):
        vendor = embeddings.vendors()[vendor]

        required: dict = {}
        desc = vendor.describe()
        for key, value in desc["params"].items():
            if value.get("required", False):
                required[key] = value.get("default", None)

        return yaml.dump(required), format_description(vendor)

    def on_register_events(self):
        self.emb_choices.select(
            self.on_emb_vendor_change,
            inputs=[self.emb_choices],
            outputs=[self.spec, self.spec_desc],
        )
        self.btn_new.click(
            self.create_emb,
            inputs=[self.name, self.emb_choices, self.spec, self.default],
            outputs=None,
        ).success(self.list_embeddings, inputs=None, outputs=[self.emb_list]).success(
            lambda: ("", None, "", False, self.spec_desc_default),
            outputs=[
                self.name,
                self.emb_choices,
                self.spec,
                self.default,
                self.spec_desc,
            ],
        )
        self.emb_list.select(
            self.select_emb,
            inputs=self.emb_list,
            outputs=[self.selected_emb_name],
            show_progress="hidden",
        )
        self.selected_emb_name.change(
            self.on_selected_emb_change,
            inputs=[self.selected_emb_name],
            outputs=[
                self._selected_panel,
                self._selected_panel_btn,
                # delete section
                self.btn_delete,
                self.btn_delete_yes,
                self.btn_delete_no,
                # edit section
                self.edit_spec,
                self.edit_spec_desc,
                self.edit_default,
            ],
            show_progress="hidden",
        )
        self.btn_delete.click(
            self.on_btn_delete_click,
            inputs=None,
            outputs=[self.btn_delete, self.btn_delete_yes, self.btn_delete_no],
            show_progress="hidden",
        )
        self.btn_delete_yes.click(
            self.delete_emb,
            inputs=[self.selected_emb_name],
            outputs=[self.selected_emb_name],
            show_progress="hidden",
        ).then(
            self.list_embeddings,
            inputs=None,
            outputs=[self.emb_list],
        )
        self.btn_delete_no.click(
            lambda: (
                gr.update(visible=True),
                gr.update(visible=False),
                gr.update(visible=False),
            ),
            inputs=None,
            outputs=[self.btn_delete, self.btn_delete_yes, self.btn_delete_no],
            show_progress="hidden",
        )
        self.btn_edit_save.click(
            self.save_emb,
            inputs=[
                self.selected_emb_name,
                self.edit_default,
                self.edit_spec,
            ],
            show_progress="hidden",
        ).then(
            self.list_embeddings,
            inputs=None,
            outputs=[self.emb_list],
        )
        self.btn_close.click(
            lambda: "",
            outputs=[self.selected_emb_name],
        )

    def create_emb(self, name, choices, spec, default):
        try:
            spec = yaml.safe_load(spec)
            spec["__type__"] = (
                embeddings.vendors()[choices].__module__
                + "."
                + embeddings.vendors()[choices].__qualname__
            )

            embeddings.add(name, spec=spec, default=default)
            gr.Info(f'Create Embedding model "{name}" successfully')
        except Exception as e:
            raise gr.Error(f"Failed to create Embedding model {name}: {e}")

    def list_embeddings(self):
        """List the Embedding models"""
        items = []
        for item in embeddings.info().values():
            record = {}
            record["name"] = item["name"]
            record["vendor"] = item["spec"].get("__type__", "-").split(".")[-1]
            record["default"] = item["default"]
            items.append(record)

        if items:
            emb_list = pd.DataFrame.from_records(items)
        else:
            emb_list = pd.DataFrame.from_records(
                [{"name": "-", "vendor": "-", "default": "-"}]
            )

        return emb_list

    def select_emb(self, emb_list, ev: gr.SelectData):
        if ev.value == "-" and ev.index[0] == 0:
            gr.Info("No embedding model is loaded. Please add first")
            return ""

        if not ev.selected:
            return ""

        return emb_list["name"][ev.index[0]]

    def on_selected_emb_change(self, selected_emb_name):
        if selected_emb_name == "":
            _selected_panel = gr.update(visible=False)
            _selected_panel_btn = gr.update(visible=False)
            btn_delete = gr.update(visible=True)
            btn_delete_yes = gr.update(visible=False)
            btn_delete_no = gr.update(visible=False)
            edit_spec = gr.update(value="")
            edit_spec_desc = gr.update(value="")
            edit_default = gr.update(value=False)
        else:
            _selected_panel = gr.update(visible=True)
            _selected_panel_btn = gr.update(visible=True)
            btn_delete = gr.update(visible=True)
            btn_delete_yes = gr.update(visible=False)
            btn_delete_no = gr.update(visible=False)

            info = deepcopy(embeddings.info()[selected_emb_name])
            vendor_str = info["spec"].pop("__type__", "-").split(".")[-1]
            vendor = embeddings.vendors()[vendor_str]

            edit_spec = yaml.dump(info["spec"])
            edit_spec_desc = format_description(vendor)
            edit_default = info["default"]

        return (
            _selected_panel,
            _selected_panel_btn,
            btn_delete,
            btn_delete_yes,
            btn_delete_no,
            edit_spec,
            edit_spec_desc,
            edit_default,
        )

    def on_btn_delete_click(self):
        btn_delete = gr.update(visible=False)
        btn_delete_yes = gr.update(visible=True)
        btn_delete_no = gr.update(visible=True)

        return btn_delete, btn_delete_yes, btn_delete_no

    def save_emb(self, selected_emb_name, default, spec):
        try:
            spec = yaml.safe_load(spec)
            spec["__type__"] = embeddings.info()[selected_emb_name]["spec"]["__type__"]
            embeddings.update(selected_emb_name, spec=spec, default=default)
            gr.Info(f'Save Embedding model "{selected_emb_name}" successfully')
        except Exception as e:
            gr.Error(f'Failed to save Embedding model "{selected_emb_name}": {e}')

    def delete_emb(self, selected_emb_name):
        try:
            embeddings.delete(selected_emb_name)
        except Exception as e:
            gr.Error(f'Failed to delete Embedding model "{selected_emb_name}": {e}')
            return selected_emb_name

        return ""
