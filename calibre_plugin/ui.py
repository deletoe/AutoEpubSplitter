from __future__ import absolute_import, division, print_function, unicode_literals

import html
import shutil
import tempfile
import traceback
from pathlib import Path

from calibre.ebooks.metadata import MetaInformation
from calibre.gui2 import error_dialog, info_dialog, question_dialog
from calibre.gui2.actions import InterfaceAction
from calibre_plugins.auto_epub_splitter import splitter_core
from qt.core import QApplication, QCursor, QIcon, QPixmap, Qt


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
        epub_abspath = db.format_abspath(book_id, "EPUB", index_is_id=True)
        if not epub_abspath:
            error_dialog(
                self.gui,
                "No EPUB",
                "The selected book does not have an EPUB format.",
                show=True,
            )
            return

        source_mi = db.get_metadata(book_id, index_is_id=True)
        epub_path = Path(epub_abspath)
        if not epub_path.exists():
            error_dialog(
                self.gui,
                "EPUB Not Found",
                "Calibre reported an EPUB format, but the file could not be found on disk.",
                det_msg=str(epub_path),
                show=True,
            )
            return

        try:
            QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
            self.gui.status_bar.show_message("Detecting split points...", 60000)
            report = splitter_core.detect_split_ranges(epub_path)
        except Exception as exc:
            error_dialog(
                self.gui,
                "Split Detection Failed",
                "Auto EPUB Splitter could not detect split points for this EPUB.",
                det_msg=traceback.format_exc(),
                show=True,
            )
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.gui.status_bar.show_message("Finished detecting split points.", 3000)

        books = report.get("books") or []
        if not books:
            error_dialog(
                self.gui,
                "No Split Points",
                "No single-book split points were detected.",
                det_msg="\n".join(report.get("notes") or []),
                show=True,
            )
            return

        detail = self._format_detection_detail(report)
        if not question_dialog(
            self.gui,
            "Auto EPUB Splitter",
            "Create {count} split books from:\n{title}".format(count=len(books), title=source_mi.title),
            det_msg=detail,
            show_copy_button=True,
            yes_text="Create",
            no_text="Cancel",
        ):
            return

        temp_dir = Path(tempfile.mkdtemp(prefix="auto-epub-splitter-"))
        created_ids = []
        try:
            QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
            self.gui.status_bar.show_message("Writing split EPUBs...", 60000)
            output_paths = splitter_core.write_split_outputs(epub_path, temp_dir, books, overwrite=True)
            for item, output_path in zip(books, output_paths):
                new_id = self._add_split_book(db, book_id, source_mi, item, output_path)
                created_ids.append(new_id)

            db.commit()
            self.gui.library_view.model().books_added(len(created_ids))
            self.gui.library_view.model().refresh_ids(created_ids)
            self.gui.library_view.select_rows(created_ids)
            self.gui.tags_view.recount()
            if self.gui.cover_flow:
                self.gui.cover_flow.dataChanged()
        except Exception as exc:
            error_dialog(
                self.gui,
                "Split Failed",
                "Auto EPUB Splitter failed while creating split books.",
                det_msg=traceback.format_exc(),
                show=True,
            )
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.gui.status_bar.show_message("Finished writing split EPUBs.", 3000)
            shutil.rmtree(str(temp_dir), ignore_errors=True)

        info_dialog(
            self.gui,
            "Auto EPUB Splitter",
            "Created {count} split books.".format(count=len(created_ids)),
            det_msg=detail,
            show=True,
        )

    def _format_detection_detail(self, report):
        lines = [
            "Source: {0}".format(report.get("source", "")),
            "Input: {0}".format(report.get("input", "")),
            "",
            "Books:",
        ]
        for index, item in enumerate(report.get("books") or [], 1):
            lines.append(
                "{index:02d}. {title}  lines {start}-{end}  confidence {confidence:.2f}".format(
                    index=index,
                    title=item.get("title", ""),
                    start=item.get("start_line", ""),
                    end=int(item.get("end_line_exclusive", 0)) - 1,
                    confidence=float(item.get("confidence", 0) or 0),
                )
            )
            reason = item.get("reason")
            if reason:
                lines.append("    {0}".format(reason))
        notes = report.get("notes") or []
        if notes:
            lines.extend(["", "Notes:"])
            lines.extend(str(note) for note in notes)
        return "\n".join(lines)

    def _add_split_book(self, db, source_id, source_mi, item, output_path):
        mi = MetaInformation(item.get("title") or "Split EPUB", source_mi.authors)
        mi.languages = source_mi.languages
        mi.tags = list(source_mi.tags or [])
        mi.comments = (
            "<div><p>Split from: <em>{title}</em></p></div>".format(title=html.escape(source_mi.title))
            if source_mi.title
            else None
        )
        new_id = db.create_book_entry(mi, add_duplicates=True)
        if getattr(source_mi, "has_cover", False):
            db.set_cover(new_id, db.cover(source_id, index_is_id=True))
        db.add_format_with_hooks(new_id, "EPUB", str(output_path), index_is_id=True)
        return new_id
