"""The interface a television's own browser gets, and the line it must not cross.

The set this was built against runs QtWebKit 4.8 (2012). It was asked what it
supports, and the answer was: ES5, no CSS custom properties, no calc(), no
flexbox. A single modern token in these files is not a degraded page -- it is a
blank screen on the television, with nothing to say why. So the rule is checked
here rather than remembered.
"""

import re
import tempfile
import unittest
from pathlib import Path

from pipertv.app import create_app

STATIC = Path(__file__).resolve().parent.parent / "pipertv" / "static"

# What that engine answered "no" to, as it appears in source. Syntax first:
# one of these is not a missing feature but a script that never runs at all.
TOO_NEW_SYNTAX = [
    (r"=>", "arrow functions"),
    (r"\blet\s+\w", "let"),
    (r"\bconst\s+\w", "const"),
    (r"`", "template strings"),
    (r"\bclass\s+\w+\s*\{", "classes"),
    (r"\.\.\.", "spread"),
    (r"\basync\b", "async functions"),
    (r"\bawait\b", "await"),
    (r"\?\.", "optional chaining"),
    (r"\?\?", "nullish coalescing"),
]
# Then what simply is not there. Naming one to ask whether it exists is how the
# check decides which interface a browser gets, so only the page itself is held
# to this list.
TOO_NEW_API = [
    (r"\bfetch\s*\(", "fetch"),
    (r"\bnew Promise\b|\bPromise\.", "Promise"),
    (r"\bObject\.assign\b", "Object.assign"),
    (r"\.replaceChildren\b", "replaceChildren"),
    (r"\.append\s*\(", "Element.append"),
    (r"\bnew URLSearchParams\b", "URLSearchParams"),
    (r"\bnew AbortController\b", "AbortController"),
    (r"\.find\s*\(", "Array.find"),
]
TOO_NEW_JS = TOO_NEW_SYNTAX + TOO_NEW_API
TOO_NEW_CSS = [
    (r"var\(--", "custom properties"),
    (r"\bcalc\(", "calc()"),
    (r"display\s*:\s*flex", "flexbox"),
    (r"display\s*:\s*grid", "grid"),
    (r"\binset\s*:", "the inset shorthand"),
    (r":is\(|:where\(|:has\(", "modern selectors"),
]


def source(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def code(name: str) -> str:
    """The file without its comments: the rule is about what runs, not prose.

    Explaining why `Promise` is out of reach would otherwise fail the test that
    keeps it out.
    """
    text = re.sub(r"/\*.*?\*/", " ", source(name), flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", " ", text)


class OldEngineTests(unittest.TestCase):
    def test_the_television_s_script_uses_nothing_newer_than_es5(self):
        text = code("classic.js")
        for pattern, what in TOO_NEW_JS:
            self.assertIsNone(re.search(pattern, text),
                              f"classic.js uses {what}, which this television cannot parse")

    def test_the_television_s_stylesheet_uses_nothing_newer_than_css2(self):
        text = code("classic.css")
        for pattern, what in TOO_NEW_CSS:
            self.assertIsNone(re.search(pattern, text),
                              f"classic.css uses {what}, which this television ignores")

    def test_the_check_that_sends_a_browser_there_is_itself_readable_by_it(self):
        # The one file every browser runs, including the one being tested for.
        text = code("tv-check.js")
        for pattern, what in TOO_NEW_SYNTAX:
            self.assertIsNone(re.search(pattern, text),
                              f"tv-check.js uses {what}, so the browser it is for cannot run it")

    def test_the_colour_keys_are_the_ones_every_television_agrees_on(self):
        # HbbTV numbered them, which is why they can be relied on when OK and
        # back are not given to the page at all.
        text = source("classic.js")
        for number, colour in ((403, "red"), (404, "green"), (405, "yellow"), (406, "blue")):
            self.assertRegex(text, rf'{number}:\s*"{colour}"')

    def test_the_page_can_be_driven_without_ok(self):
        # This television's OK never reached the page, so something else has to
        # open a service.
        text = source("classic.js")
        self.assertRegex(text, r'case "ok": case "green": open\(\);')


class ServingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        app = create_app(data=Path(self.temporary.name) / "recordings.json", demo=True)
        self.client = app.test_client()

    def test_every_part_of_the_television_s_page_is_served(self):
        for path, kind in (("/tv/classic", "text/html"), ("/classic.js", "javascript"),
                           ("/classic.css", "text/css"), ("/tv-check.js", "javascript")):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(kind, response.headers["Content-Type"], path)

    def test_the_check_runs_before_the_page_it_guards(self):
        page = source("tv.html")
        self.assertLess(page.index("tv-check.js"), page.index('src="/tv.js"'))
        self.assertNotRegex(page[page.index("tv-check.js") - 120:page.index("tv-check.js") + 40],
                            r"defer")

    def test_the_classic_page_asks_for_nothing_but_its_own_two_files(self):
        # The policy this app sends allows only its own origin, and a
        # television has no business fetching a font from anywhere.
        page = source("classic.html")
        for link in re.findall(r'(?:src|href)="([^"]+)"', page):
            self.assertTrue(link.startswith("/"), link)


if __name__ == "__main__":
    unittest.main()
