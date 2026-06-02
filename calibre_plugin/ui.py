from __future__ import absolute_import, division, print_function, unicode_literals

from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import QIcon, QPixmap


PLUGIN_ICONS = ["images/icon.png"]


class AutoEpubSplitterAction(InterfaceAction):
    name = "AutoEpubSplitter"
    action_spec = (
        "Auto EPUB Splitter",
        None,
        "Automatically split an EPUB collection into single books.",
        None,
    )
    action_type = "global"

    def genesis(self):
        self._install_icon()
        self.qaction.triggered.connect(self.plugin_button)

    def _install_icon(self):
        icon_data = self.load_resources(PLUGIN_ICONS).get("images/icon.png")
        if not icon_data:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(icon_data):
            self.qaction.setIcon(QIcon(pixmap))

    def location_selected(self, loc):
        self.qaction.setEnabled(loc == "library")

    def plugin_button(self):
        selected_ids = list(self.gui.library_view.get_selected_ids())
        if len(selected_ids) != 1:
            error_dialog(
                self.gui,
                "Select One Book",
                "Please select exactly one EPUB collection.",
                show=True,
            )
            return

        db = self.gui.current_db
        book_id = selected_ids[0]
        if not db.has_format(book_id, "EPUB", index_is_id=True):
            error_dialog(
                self.gui,
                "No EPUB",
                "The selected book does not have an EPUB format.",
                show=True,
            )
            return

        mi = db.get_metadata(book_id, index_is_id=True)
        info_dialog(
            self.gui,
            "Auto EPUB Splitter",
            (
                "Plugin scaffold is installed and can see the selected EPUB.\n\n"
                "Selected book:\n"
                "{title}\n\n"
                "Next step: wire the existing auto-split and metadata enrichment "
                "core into this Calibre action."
            ).format(title=mi.title),
            show=True,
        )
