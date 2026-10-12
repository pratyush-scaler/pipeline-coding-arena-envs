"""Literal source-policy coverage for HTML; no scripts or payloads execute."""
import hashlib
import tempfile
from pathlib import Path
from types import ModuleType
import unittest

from pipeline.author import runtime
from pipeline.author.grader_runtime import node_test


class ExecutableHTMLScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.grade = ModuleType('html_policy_fixture')
        self.grade.__file__ = str(runtime.GRADER)
        exec(compile(runtime.GRADER.read_bytes(), self.grade.__file__, 'exec'), self.grade.__dict__)

    def scan(self, text, name='index.html', manifest=None, vendored=()):
        p = self.root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
        return self.grade.introspection_problems(self.root, manifest or {}, vendored)

    def test_changed_inline_classic_and_module_scripts_are_scanned(self):
        for kind in ('', ' type="module"', ' type="TEXT/JAVASCRIPT"', ' language="JavaScript1.5"'):
            with self.subTest(kind=kind):
                findings = self.scan('<script' + kind + '>const helpLabel = "/tests/help";</script>')
                self.assertEqual(findings, ['index.html: /tests'])

    def test_html_extensions_and_case_variants_are_covered(self):
        for name in ('index.HTML', 'page.htm', 'view.xhtml'):
            with self.subTest(name=name):
                text = '<SCRIPT TYPE="MoDuLe">const helpLabel = "/tests/help";</SCRIPT>'
                p = self.root / name; p.write_text(text)
                self.assertIn(name + ': /tests', self.grade.introspection_problems(self.root, {}))

    def test_visible_text_comments_and_non_script_data_remain_ordinary_content(self):
        text = '<p>Documentation mentions /tests and ARENA_EXAMPLE.</p><!-- /app/tests -->' \
               '<script type="application/json">{"label":"/tests"}</script>' \
               '<script type="text/plain">Example: /app/tests</script>' \
               '<script>const greeting = "hello";</script>'
        self.assertEqual(self.scan(text), [])

    def test_inline_handlers_and_javascript_urls_use_decoded_attribute_values(self):
        for markup in ('<button onclick="const label = \'&#47;tests/help\';">Help</button>',
                       '<a href="javascript: \'/tests/help\'">Help</a>'):
            with self.subTest(markup=markup):
                self.assertEqual(self.scan(markup), ['index.html: /tests'])

    def test_external_script_source_is_scanned_but_its_documentation_is_not(self):
        self.assertEqual(self.scan('<script src="app.js">Documentation: /tests</script>'), [])
        self.assertEqual(self.scan('<script src="/tests/help.js"></script>'), ['index.html: /tests'])

    def test_first_duplicate_attribute_controls_script_type(self):
        self.assertEqual(self.scan('<script type="module" type="application/json">"/tests"</script>'),
                         ['index.html: /tests'])
        self.assertEqual(self.scan('<script type="application/json" type="module">"/tests"</script>'), [])

    def test_fixture_manifest_exemption_is_preserved_and_dependency_names_grant_none(self):
        text = '<script>const helpLabel = "/tests/help";</script>'
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.assertEqual(self.scan(text, manifest={'index.html': {'sha256': digest}}), [])
        self.assertEqual(self.scan(text + '\n', manifest={'index.html': {'sha256': digest}}), ['index.html: /tests'])
        (self.root / 'index.html').unlink()
        # A directory name is not provenance: changed pages below vendor-like or
        # declared vendored names are scanned (the 2026-10-02 vendor forgery).
        self.assertEqual(self.scan(text, name='vendor/index.html'), ['vendor/index.html: /tests'])
        (self.root / 'vendor/index.html').unlink()
        self.assertEqual(self.scan(text, name='deps/index.html', vendored=['deps']), ['deps/index.html: /tests'])
        (self.root / 'deps/index.html').unlink()
        page = 'vendor/legacy.html'
        self.assertEqual(self.scan(text, name=page, manifest={page: {'sha256': digest}}), [])

    def test_symlink_and_other_language_handling_remain_unchanged(self):
        other = self.root / 'outside.txt'; other.write_text('<script>"/tests"</script>')
        (self.root / 'index.html').symlink_to(other)
        self.assertEqual(self.grade.introspection_problems(self.root, {}), [])
        self.assertEqual(self.scan('const label = "/tests/help";', name='app.js'), ['app.js: /tests'])

    def test_node_specialization_includes_the_shared_html_scan(self):
        grade = ModuleType('node_html_policy_fixture'); grade.__file__ = str(runtime.GRADER)
        exec(compile(node_test.render(runtime.GRADER.read_bytes()), grade.__file__, 'exec'), grade.__dict__)
        p = self.root / 'index.html'; p.write_text('<script>const helpLabel = "/tests/help";</script>')
        self.assertEqual(grade.introspection_problems(self.root, {}), ['index.html: /tests'])


if __name__ == '__main__':
    unittest.main()
