from __future__ import absolute_import, division, print_function, unicode_literals

from pathlib import Path

from calibre.utils.config import JSONConfig
from qt.core import QCheckBox, QDoubleSpinBox, QFormLayout, QLineEdit, QSpinBox, QVBoxLayout, QWidget


DEFAULT_PREFS = {
    "use_llm": True,
    "vllm_base_url": "http://10.130.92.107:8000",
    "model": "",
    "split_llm_timeout": 120,
    "split_llm_max_tokens": 65536,
    "metadata_llm_timeout": 60,
    "cover_vision_timeout": 45,
    "use_cover_vision": True,
    "extract_authors": True,
    "douban_delay": 3.0,
    "max_candidates": 5,
    "cache_dir": str(Path.home() / ".cache" / "auto_epub_splitter" / "douban"),
    "llm_describe_miss": False,
    "source_douban": True,
    "source_google_books": False,
    "google_books_api_key": "",
}

prefs = JSONConfig("plugins/AutoEpubSplitter")
prefs.defaults = DEFAULT_PREFS


def get_prefs():
    values = {}
    for key, default in DEFAULT_PREFS.items():
        values[key] = prefs.get(key, default)
    return values


def bool_pref(key):
    return bool(prefs.get(key, DEFAULT_PREFS[key]))


class ConfigWidget(QWidget):
    def __init__(self, plugin_action):
        QWidget.__init__(self)
        self.plugin_action = plugin_action
        values = get_prefs()

        layout = QVBoxLayout()
        form = QFormLayout()
        layout.addLayout(form)
        layout.addStretch(1)
        self.setLayout(layout)

        self.use_llm = QCheckBox("Use local LLM for split decisions and metadata cleanup")
        self.use_llm.setChecked(bool(values["use_llm"]))
        form.addRow("", self.use_llm)

        self.vllm_base_url = QLineEdit(str(values["vllm_base_url"]))
        form.addRow("OpenAI-compatible base URL", self.vllm_base_url)

        self.model = QLineEdit(str(values["model"] or ""))
        self.model.setPlaceholderText("Leave empty to use the first /v1/models result")
        form.addRow("Model", self.model)

        self.split_llm_timeout = QSpinBox()
        self.split_llm_timeout.setRange(5, 600)
        self.split_llm_timeout.setValue(int(values["split_llm_timeout"]))
        form.addRow("Split LLM timeout seconds", self.split_llm_timeout)

        self.split_llm_max_tokens = QSpinBox()
        self.split_llm_max_tokens.setRange(2048, 262144)
        self.split_llm_max_tokens.setSingleStep(4096)
        self.split_llm_max_tokens.setValue(int(values["split_llm_max_tokens"]))
        form.addRow("Split LLM max tokens", self.split_llm_max_tokens)

        self.metadata_llm_timeout = QSpinBox()
        self.metadata_llm_timeout.setRange(5, 600)
        self.metadata_llm_timeout.setValue(int(values["metadata_llm_timeout"]))
        form.addRow("Metadata LLM timeout seconds", self.metadata_llm_timeout)

        self.use_cover_vision = QCheckBox("Use vision-capable LLM to choose EPUB covers")
        self.use_cover_vision.setChecked(bool(values["use_cover_vision"]))
        form.addRow("", self.use_cover_vision)

        self.cover_vision_timeout = QSpinBox()
        self.cover_vision_timeout.setRange(5, 600)
        self.cover_vision_timeout.setValue(int(values["cover_vision_timeout"]))
        form.addRow("Cover vision timeout seconds", self.cover_vision_timeout)

        self.extract_authors = QCheckBox("Extract missing/ambiguous authors from front matter with LLM")
        self.extract_authors.setChecked(bool(values["extract_authors"]))
        form.addRow("", self.extract_authors)

        self.llm_describe_miss = QCheckBox("Let LLM write cautious descriptions when Douban misses")
        self.llm_describe_miss.setChecked(bool(values["llm_describe_miss"]))
        form.addRow("", self.llm_describe_miss)

        self.source_douban = QCheckBox("Use Douban Books metadata source")
        self.source_douban.setChecked(bool(values["source_douban"]))
        form.addRow("", self.source_douban)

        self.source_google_books = QCheckBox("Use Google Books metadata source")
        self.source_google_books.setChecked(bool(values["source_google_books"]))
        form.addRow("", self.source_google_books)

        self.google_books_api_key = QLineEdit(str(values["google_books_api_key"] or ""))
        self.google_books_api_key.setPlaceholderText("Optional; public API works without a key but has lower quota")
        form.addRow("Google Books API key", self.google_books_api_key)

        self.douban_delay = QDoubleSpinBox()
        self.douban_delay.setRange(0.0, 30.0)
        self.douban_delay.setDecimals(1)
        self.douban_delay.setSingleStep(0.5)
        self.douban_delay.setValue(float(values["douban_delay"]))
        form.addRow("Douban request delay seconds", self.douban_delay)

        self.max_candidates = QSpinBox()
        self.max_candidates.setRange(1, 20)
        self.max_candidates.setValue(int(values["max_candidates"]))
        form.addRow("Max Douban candidates per book", self.max_candidates)

        self.cache_dir = QLineEdit(str(values["cache_dir"]))
        form.addRow("HTTP cache directory", self.cache_dir)

    def save_settings(self):
        prefs["use_llm"] = self.use_llm.isChecked()
        prefs["vllm_base_url"] = str(self.vllm_base_url.text()).strip() or DEFAULT_PREFS["vllm_base_url"]
        prefs["model"] = str(self.model.text()).strip()
        prefs["split_llm_timeout"] = int(self.split_llm_timeout.value())
        prefs["split_llm_max_tokens"] = int(self.split_llm_max_tokens.value())
        prefs["metadata_llm_timeout"] = int(self.metadata_llm_timeout.value())
        prefs["use_cover_vision"] = self.use_cover_vision.isChecked()
        prefs["cover_vision_timeout"] = int(self.cover_vision_timeout.value())
        prefs["extract_authors"] = self.extract_authors.isChecked()
        prefs["llm_describe_miss"] = self.llm_describe_miss.isChecked()
        prefs["source_douban"] = self.source_douban.isChecked()
        prefs["source_google_books"] = self.source_google_books.isChecked()
        prefs["google_books_api_key"] = str(self.google_books_api_key.text()).strip()
        prefs["douban_delay"] = float(self.douban_delay.value())
        prefs["max_candidates"] = int(self.max_candidates.value())
        prefs["cache_dir"] = str(self.cache_dir.text()).strip() or DEFAULT_PREFS["cache_dir"]
