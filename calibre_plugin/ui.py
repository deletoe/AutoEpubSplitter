from __future__ import absolute_import, division, print_function, unicode_literals

import html
import shutil
import tempfile
import traceback
from pathlib import Path
from zipfile import ZipFile
from xml.dom.minidom import parseString

from calibre.ebooks.metadata import MetaInformation
from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from calibre_plugins.auto_epub_splitter.config import get_prefs
from calibre_plugins.auto_epub_splitter import metadata_core, splitter_core
from qt.core import (
    QDialog,
    QHBoxLayout,
    QIcon,
    QLabel,
    QProgressBar,
    QPushButton,
    QPixmap,
    QTextCursor,
    QTextEdit,
    QThread,
    QVBoxLayout,
    pyqtSignal,
)


PLUGIN_ICONS = ["images/icon.png"]


class ProgressDialog(QDialog):
    cancel_requested = pyqtSignal()

    def __init__(self, parent, title, message):
        QDialog.__init__(self, parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(620)
        self.setMinimumHeight(360)

        self.label = QLabel(message)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._request_cancel)
        self.close_button = QPushButton("Close")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.progress)
        layout.addWidget(self.log)
        layout.addLayout(buttons)
        self.setLayout(layout)

    def set_status(self, message):
        self.label.setText(message)

    def set_progress(self, current, total):
        total = max(int(total or 0), 1)
        current = max(0, min(int(current or 0), total))
        self.progress.setRange(0, total)
        self.progress.setValue(current)

    def append_log(self, message):
        self.log.append(str(message))
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def append_text(self, text):
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        self.log.insertPlainText(str(text))
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def finish(self, message=None):
        if message:
            self.set_status(message)
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)

    def _request_cancel(self):
        self.cancel_button.setEnabled(False)
        self.append_log("\nCancel requested. Waiting for the current step to stop...")
        self.cancel_requested.emit()

    def closeEvent(self, event):
        if self.close_button.isEnabled():
            event.accept()
        else:
            event.ignore()


class SplitConfirmDialog(QDialog):
    def __init__(self, parent, source_title, report, detail):
        QDialog.__init__(self, parent)
        self.setWindowTitle("Auto EPUB Splitter")
        self.setMinimumWidth(700)
        self.setMinimumHeight(440)

        books = report.get("books") or []
        label = QLabel(
            "Create and enrich {count} split books from:\n{title}".format(
                count=len(books),
                title=source_title or report.get("input", ""),
            )
        )
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlainText(detail)

        self.create_button = QPushButton("Create")
        self.cancel_button = QPushButton("Cancel")
        self.create_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.create_button)

        layout = QVBoxLayout()
        layout.addWidget(label)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)
        self.setLayout(layout)


def model_or_none(value):
    value = str(value or "").strip()
    return value or None


def metadata_sources_from_settings(settings):
    sources = []
    if settings.get("source_douban", True):
        sources.append("douban")
    if settings.get("source_google_books", False):
        sources.append("google_books")
    return sources or ["douban"]


class DetectWorker(QThread):
    progress = pyqtSignal(str)
    llm_delta = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, epub_path, settings):
        QThread.__init__(self)
        self.epub_path = Path(epub_path)
        self.settings = dict(settings)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def is_cancelled(self):
        return self._cancel

    def run(self):
        try:
            self.progress.emit("Reading EPUB structure...")
            self.progress.emit("Asking LLM to detect split points...")
            report = splitter_core.detect_split_ranges(
                self.epub_path,
                use_llm=bool(self.settings.get("use_llm", True)),
                vllm_base_url=self.settings.get("vllm_base_url") or splitter_core.DEFAULT_VLLM_BASE_URL,
                model=model_or_none(self.settings.get("model")),
                llm_timeout=int(self.settings.get("split_llm_timeout", 120)),
                llm_max_tokens_value=int(
                    self.settings.get(
                        "split_llm_max_tokens",
                        getattr(splitter_core, "DEFAULT_SPLIT_LLM_MAX_TOKENS", 65536),
                    )
                ),
                stream_callback=self.llm_delta.emit,
                cancel_callback=self.is_cancelled,
            )
            if self.is_cancelled():
                self.failed.emit("Canceled by user")
                return
            self.progress.emit("Detected {0} split target(s).".format(len(report.get("books") or [])))
            self.finished_ok.emit(report)
        except Exception:
            self.failed.emit(traceback.format_exc())


class SplitMetadataWorker(QThread):
    status = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    llm_delta = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, epub_path, temp_dir, books, settings):
        QThread.__init__(self)
        self.epub_path = Path(epub_path)
        self.temp_dir = Path(temp_dir)
        self.books = list(books)
        self.settings = dict(settings)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def is_cancelled(self):
        return self._cancel

    def run(self):
        try:
            total = max(len(self.books) * 2, 1)
            done = 0

            split_dir = self.temp_dir / "split"
            enriched_dir = self.temp_dir / "enriched"
            enriched_dir.mkdir(parents=True, exist_ok=True)

            self.status.emit("Writing split EPUBs...")

            def split_progress(index, count, title):
                if self.is_cancelled():
                    raise RuntimeError("Canceled by user")
                self.log.emit("[split {0}/{1}] {2}".format(index, count, title))
                self.progress.emit(index - 1, total)

            output_paths = splitter_core.write_split_outputs(
                self.epub_path,
                split_dir,
                self.books,
                overwrite=True,
                progress=split_progress,
            )
            done = len(output_paths)
            self.progress.emit(done, total)
            if self.is_cancelled():
                self.failed.emit("Canceled by user")
                return

            results = []
            args = metadata_core.default_args(
                cache_dir=Path(self.settings.get("cache_dir") or Path.home() / ".cache" / "auto_epub_splitter" / "douban"),
                delay=float(self.settings.get("douban_delay", 3.0)),
                no_llm=not bool(self.settings.get("use_llm", True)),
                vllm_base_url=self.settings.get("vllm_base_url") or metadata_core.DEFAULT_VLLM_BASE_URL,
                model=model_or_none(self.settings.get("model")),
                llm_timeout=int(self.settings.get("metadata_llm_timeout", 60)),
                max_candidates=int(self.settings.get("max_candidates", 5)),
                no_author_extract=not bool(self.settings.get("extract_authors", True)),
                no_cover_vision=not bool(self.settings.get("use_cover_vision", True)),
                cover_vision_timeout=int(self.settings.get("cover_vision_timeout", 45)),
                llm_describe_miss=bool(self.settings.get("llm_describe_miss", False)),
                metadata_sources=metadata_sources_from_settings(self.settings),
                google_books_api_key=self.settings.get("google_books_api_key", ""),
                stream_callback=self.llm_delta.emit,
                cancel_callback=self.is_cancelled,
            )
            for index, (item, output_path) in enumerate(zip(self.books, output_paths), 1):
                if self.is_cancelled():
                    self.failed.emit("Canceled by user")
                    return
                self.status.emit("Enriching metadata {0}/{1}...".format(index, len(output_paths)))
                self.log.emit("[metadata {0}/{1}] {2}".format(index, len(output_paths), item.get("title", "")))
                enriched_path = enriched_dir / output_path.name
                metadata_report = None
                final_path = output_path
                try:
                    metadata_report = metadata_core.enrich_epub_file(output_path, enriched_path, args)
                    final_path = enriched_path
                    metadata = metadata_report.get("metadata") or {}
                    self.log.emit(
                        "  -> {0} / {1} [{2}]".format(
                            metadata.get("title") or item.get("title", ""),
                            "; ".join(metadata.get("authors") or []),
                            metadata.get("_match", ""),
                        )
                    )
                except Exception as exc:
                    self.log.emit("  metadata failed, keeping split EPUB: {0}".format(exc))

                results.append({"book": item, "path": str(final_path), "metadata_report": metadata_report})
                done = len(output_paths) + index
                self.progress.emit(done, total)

            self.status.emit("Finished splitting and metadata enrichment.")
            self.finished_ok.emit(results)
        except Exception:
            self.failed.emit(traceback.format_exc())


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

    def apply_settings(self):
        pass

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
        context = self._selected_epub_context()
        if context is None:
            return
        db, book_id, source_mi, epub_path = context
        settings = get_prefs()

        report = self._detect_with_progress(epub_path, settings)
        if report is None:
            return

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
        accepted_code = getattr(QDialog, "Accepted", None)
        if accepted_code is None:
            accepted_code = QDialog.DialogCode.Accepted
        if SplitConfirmDialog(self.gui, source_mi.title, report, detail).exec_() != accepted_code:
            return

        temp_dir = Path(tempfile.mkdtemp(prefix="auto-epub-splitter-"))
        try:
            results = self._split_and_enrich_with_progress(epub_path, temp_dir, books, settings)
            if results is None:
                return
            created_ids = self._add_results_to_calibre(db, book_id, source_mi, results)
        finally:
            shutil.rmtree(str(temp_dir), ignore_errors=True)

        info_dialog(
            self.gui,
            "Auto EPUB Splitter",
            "Created {count} split books.".format(count=len(created_ids)),
            det_msg=detail,
            show=True,
        )

    def _selected_epub_context(self):
        selected_ids = list(self.gui.library_view.get_selected_ids())
        if len(selected_ids) != 1:
            error_dialog(self.gui, "Select One Book", "Please select exactly one EPUB collection.", show=True)
            return None

        db = self.gui.current_db
        book_id = selected_ids[0]
        epub_abspath = db.format_abspath(book_id, "EPUB", index_is_id=True)
        if not epub_abspath:
            error_dialog(self.gui, "No EPUB", "The selected book does not have an EPUB format.", show=True)
            return None

        epub_path = Path(epub_abspath)
        if not epub_path.exists():
            error_dialog(
                self.gui,
                "EPUB Not Found",
                "Calibre reported an EPUB format, but the file could not be found on disk.",
                det_msg=str(epub_path),
                show=True,
            )
            return None

        return db, book_id, db.get_metadata(book_id, index_is_id=True), epub_path

    def _detect_with_progress(self, epub_path, settings):
        dialog = ProgressDialog(self.gui, "Auto EPUB Splitter", "Detecting split points...")
        state = {"report": None, "error": None}
        worker = DetectWorker(epub_path, settings)
        dialog.cancel_requested.connect(worker.cancel)
        worker.progress.connect(dialog.append_log)
        worker.llm_delta.connect(dialog.append_text)
        worker.finished_ok.connect(lambda report: state.update(report=report))
        worker.failed.connect(lambda error: state.update(error=error))
        worker.finished.connect(lambda: dialog.finish("Detection finished."))
        worker.finished.connect(dialog.accept)
        worker.start()
        dialog.exec_()
        worker.wait()
        if state["error"]:
            if "Canceled by user" in state["error"]:
                return None
            error_dialog(
                self.gui,
                "Split Detection Failed",
                "Auto EPUB Splitter could not detect split points for this EPUB.",
                det_msg=state["error"],
                show=True,
            )
            return None
        return state["report"]

    def _split_and_enrich_with_progress(self, epub_path, temp_dir, books, settings):
        dialog = ProgressDialog(self.gui, "Auto EPUB Splitter", "Preparing split job...")
        state = {"results": None, "error": None}
        worker = SplitMetadataWorker(epub_path, temp_dir, books, settings)
        dialog.cancel_requested.connect(worker.cancel)
        worker.status.connect(dialog.set_status)
        worker.progress.connect(dialog.set_progress)
        worker.log.connect(dialog.append_log)
        worker.llm_delta.connect(dialog.append_text)
        worker.finished_ok.connect(lambda results: state.update(results=results))
        worker.failed.connect(lambda error: state.update(error=error))
        worker.finished.connect(lambda: dialog.finish("Processing finished."))
        worker.finished.connect(dialog.accept)
        worker.start()
        dialog.exec_()
        worker.wait()
        if state["error"]:
            if "Canceled by user" in state["error"]:
                return None
            error_dialog(
                self.gui,
                "Split Failed",
                "Auto EPUB Splitter failed while creating split books.",
                det_msg=state["error"],
                show=True,
            )
            return None
        return state["results"] or []

    def _add_results_to_calibre(self, db, source_id, source_mi, results):
        created_ids = []
        for result in results:
            new_id = self._add_split_book(db, source_id, source_mi, result)
            created_ids.append(new_id)

        db.commit()
        self.gui.library_view.model().books_added(len(created_ids))
        self.gui.library_view.model().refresh_ids(created_ids)
        self.gui.library_view.select_rows(created_ids)
        self.gui.tags_view.recount()
        if self.gui.cover_flow:
            self.gui.cover_flow.dataChanged()
        return created_ids

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

    def _add_split_book(self, db, source_id, source_mi, result):
        item = result.get("book") or {}
        metadata_report = result.get("metadata_report") or {}
        metadata = metadata_report.get("metadata") or {}
        title = metadata.get("title") or item.get("title") or "Split EPUB"
        authors = metadata.get("authors") or source_mi.authors

        mi = MetaInformation(title, authors)
        mi.languages = source_mi.languages
        mi.tags = list(metadata.get("tags") or source_mi.tags or [])
        if metadata.get("publisher"):
            mi.publisher = metadata.get("publisher")
        if metadata.get("description"):
            mi.comments = metadata.get("description")
        elif source_mi.title:
            mi.comments = "<div><p>Split from: <em>{title}</em></p></div>".format(title=html.escape(source_mi.title))
        identifiers = {}
        if metadata.get("isbn"):
            identifiers["isbn"] = str(metadata.get("isbn"))
        if metadata.get("id"):
            identifiers["douban"] = str(metadata.get("id"))
        if identifiers:
            mi.set_identifiers(identifiers)

        new_id = db.create_book_entry(mi, add_duplicates=True)
        cover_bytes = self._read_epub_cover_bytes(Path(result.get("path")))
        if cover_bytes:
            db.set_cover(new_id, cover_bytes)
        elif getattr(source_mi, "has_cover", False):
            db.set_cover(new_id, db.cover(source_id, index_is_id=True))
        db.add_format_with_hooks(new_id, "EPUB", str(result.get("path")), index_is_id=True)
        return new_id

    def _read_epub_cover_bytes(self, epub_path):
        try:
            with ZipFile(epub_path) as epub:
                container = parseString(epub.read("META-INF/container.xml"))
                opf_name = container.getElementsByTagName("rootfile")[0].getAttribute("full-path")
                dom = parseString(epub.read(opf_name))
                manifest = {}
                for item in dom.getElementsByTagName("item"):
                    manifest[item.getAttribute("id")] = item.getAttribute("href")
                cover_href = ""
                for meta in dom.getElementsByTagName("meta"):
                    if meta.getAttribute("name").lower() == "cover":
                        cover_href = manifest.get(meta.getAttribute("content"), "")
                        break
                if not cover_href:
                    for item in dom.getElementsByTagName("item"):
                        if item.getAttribute("media-type").startswith("image/") and "cover" in item.getAttribute("href").lower():
                            cover_href = item.getAttribute("href")
                            break
                if not cover_href:
                    return None
                base = str(Path(opf_name).parent)
                cover_path = cover_href if base == "." else "{0}/{1}".format(base, cover_href)
                return epub.read(cover_path)
        except Exception:
            return None
