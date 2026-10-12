"""A review citation is verified exactly, and indentation is not content.

c-002 (2026-09-28): a review of eighteen genuine findings was voided because
one quote of two `checks.json` lines carried a different indentation depth
from the file. Collapsing whitespace runs keeps every non-space character in
order, so the match is still exact -- never fuzzy.
"""
import unittest

from pipeline.review import _quoted

TEXT = ('{\n  "checks": [\n    {\n      "runner": {\n        "max_new_files": 8,\n'
        '        "type": "tree-guard"\n      }\n    }\n  ]\n}\n')


class QuotesAreExactUpToIndentation(unittest.TestCase):
    def test_verbatim_quote(self):
        self.assertTrue(_quoted('"max_new_files": 8,\n        "type": "tree-guard"', TEXT, "private-grader/checks.json"))

    def test_the_same_lines_at_another_depth(self):
        self.assertTrue(_quoted('"max_new_files": 8,\n          "type": "tree-guard"', TEXT, "private-grader/checks.json"))
        self.assertTrue(_quoted('"max_new_files": 8, "type": "tree-guard"', TEXT, "private-grader/checks.json"))

    def test_a_changed_character_is_still_rejected(self):
        self.assertFalse(_quoted('"max_new_files": 9,\n        "type": "tree-guard"', TEXT, "private-grader/checks.json"))
        self.assertFalse(_quoted('"max_new_files": 8,\n        "type": "hash-guard"', TEXT, "private-grader/checks.json"))

    def test_reordered_or_dropped_tokens_are_rejected(self):
        self.assertFalse(_quoted('"type": "tree-guard" "max_new_files": 8,', TEXT, "private-grader/checks.json"))
        self.assertFalse(_quoted('"max_new_files": "type": "tree-guard"', TEXT, "private-grader/checks.json"))

    def test_an_empty_quote_never_matches(self):
        self.assertFalse(_quoted("   \n  ", TEXT, "x.py"))

    def test_json_escaping_tolerance_survives(self):
        text = '{"matches": "a\\\\s+b"}'
        self.assertTrue(_quoted('"matches": "a\\s+b"', text, "checks.json"))
        self.assertFalse(_quoted('"matches": "a\\s+b"', text, "notes.md"))


class EscapeSpelledQuotes(unittest.TestCase):
    """c-018-express-chat-app v10 (2026-10-09): four verbatim quotes of JS sources holding the
    two characters `\\n`, `\\r\\n` or `\\u2026` were written as JSON escapes, decoded to a line
    break or `…`, and voided a review whose every check had passed."""
    SERVER = ("        text = (cut > 600 ? text.slice(0, cut + 1) : text.slice(0, 2200)) + '\\n…';\n"
              "      }\n")
    SUITE = ("  const text = `${kept}\\r\\n\\r\\n차니가 무언가 말하려는 순간`;\n"
             "    const ok = c.accept.some((p) => reply.startsWith(p) && reply.slice(p.length).trim() === '\\u2026');\n")

    def test_a_decoded_newline_matches_its_spelling(self):
        quote = "text = (cut > 600 ? text.slice(0, cut + 1) : text.slice(0, 2200)) + '\n…';"
        self.assertTrue(_quoted(quote, self.SERVER, "fixture/ai-chat/server.js"))

    def test_decoded_crlf_and_hangul_match(self):
        self.assertTrue(_quoted("const text = `${kept}\r\n\r\n차니가 무언가 말하려는 순간`;", self.SUITE,
                                "private-grader/suites/o_end_crlf.test.cjs"))

    def test_a_decoded_unicode_escape_matches_its_spelling(self):
        quote = "reply.slice(p.length).trim() === '…');"
        self.assertTrue(_quoted(quote, self.SUITE, "private-grader/suites/o_cap_fallback.test.cjs"))

    def test_a_real_line_break_still_matches(self):
        self.assertTrue(_quoted("text.slice(0, 2200)) + '\\n…';\n      }", self.SERVER, "fixture/server.js"))

    def test_a_changed_character_is_still_rejected(self):
        self.assertFalse(_quoted("text = (cut > 601 ? text.slice(0, cut + 1) : text.slice(0, 2200)) + '\n…';",
                                 self.SERVER, "fixture/server.js"))
        self.assertFalse(_quoted("reply.slice(p.length).trim() === '‥');", self.SUITE, "x.cjs"))
        self.assertFalse(_quoted("const text = `${kept}\r\n\r\n차니가 무언가 말하는 순간`;", self.SUITE, "x.cjs"))

    def test_a_line_break_does_not_stand_for_other_text(self):
        self.assertFalse(_quoted("text.slice(0, 2200)) +\n'…';", "text.slice(0, 2200)) + 'x…';", "x.js"))


if __name__ == "__main__":
    unittest.main()
