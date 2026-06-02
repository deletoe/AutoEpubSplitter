from __future__ import absolute_import, division, print_function, unicode_literals

from calibre.customize import InterfaceActionBase


class AutoEpubSplitterBase(InterfaceActionBase):
    """
    Calibre wrapper class.

    GUI imports must stay out of this file so calibre command-line tools can
    inspect/install the plugin without loading Qt.
    """

    name = "AutoEpubSplitter"
    description = "Automatically split EPUB collections into single books and enrich metadata."
    supported_platforms = ["windows", "osx", "linux"]
    author = "AutoEpubSplitter contributors"
    version = (0, 1, 0)
    minimum_calibre_version = (5, 0, 0)
    actual_plugin = "calibre_plugins.auto_epub_splitter.ui:AutoEpubSplitterAction"

    def is_customizable(self):
        return False

