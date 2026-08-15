from __future__ import annotations

import sys
import unittest
from pathlib import Path


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_app.api.frontend import (  # noqa: E402
    CSS_PATH,
    CSS_PLACEHOLDER,
    HTML_PATH,
    JAVASCRIPT_PATH,
    JAVASCRIPT_PATHS,
    JAVASCRIPT_PLACEHOLDER,
    load_html_template,
)


class FrontendAssetTests(unittest.TestCase):
    def test_packaged_sources_compose_without_unresolved_placeholders(self) -> None:
        template = load_html_template()

        self.assertTrue(HTML_PATH.is_file())
        self.assertTrue(CSS_PATH.is_file())
        self.assertTrue(JAVASCRIPT_PATH.is_file())
        self.assertTrue(all(path.is_file() for path in JAVASCRIPT_PATHS))
        self.assertNotIn(CSS_PLACEHOLDER, template)
        self.assertNotIn(JAVASCRIPT_PLACEHOLDER, template)
        self.assertIn("<!doctype html>", template.casefold())
        self.assertIn('id="root"', template)
        self.assertIn("window.__OGENT_CONFIG__", template)
        self.assertIn("Document map", template)
        self.assertIn("Exact Word View", template)
        self.assertIn("Start a new chat?", template)


if __name__ == "__main__":
    unittest.main()
