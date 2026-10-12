"""Access defaults are minimized without changing authorization or business facts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline.artifacts import atomic_json
from pipeline.author.build import build as export
from pipeline.ledger.faithfulness import build
from pipeline.ledger.redaction import MANIFEST, audit, minimize, sha, transform
from pipeline.privacy import redact_excerpt, scan, web_access_default_spans
from pipeline.tests.test_production import synthetic_bundle


class WebAccessRedaction(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = synthetic_bundle(self.root / 'task')
        self.fixture = self.bundle / 'environment/repo'
        self.original = ('Budget stays 123.\r\n'
            '    self.WEB_PASSWORD = [SECURITY_DATA]"WEB_PASSWORD", "demo-default")\r\n'
            "    self.WEB_SECRET_KEY = os.getenv('WEB_SECRET_KEY', 'demo-secret')\r\n"
            '    self.admins = [11, 22]\r\n')
        self.public = self.original.replace('demo-default', '[REDACTED_WEB_PASSWORD]').replace(
            'demo-secret', '[REDACTED_WEB_SECRET_KEY]')
        (self.fixture / 'brief.md').write_bytes(self.original.encode())
        trace = {'session_id': 'session', 'messages': [{'role': 'assistant', 'content': [
            {'type': 'tool-write_file', 'input': {'path': str(p.relative_to(self.fixture)),
             'content': p.read_bytes().decode()}} for p in self.fixture.rglob('*') if p.is_file()]}]}
        atomic_json(self.root / 'dump/sessions/session/data.json', {'traces': [trace]})
        self.enterContext(patch.dict(os.environ, ARENA_DUMP=str(self.root / 'dump')))
        spec = json.loads((self.bundle / 'authoring/spec.json').read_text())
        spec.update(source={'session_id': 'session', 'seed_index': 0}, _producer={'model': 'author'})
        export(spec, self.bundle)

    def test_preflight_reports_only_locations_and_role(self):
        findings = scan(self.fixture)
        self.assertEqual(findings, [{'file': 'brief.md', 'kind': 'web-access-default-metadata', 'line': line}
                                    for line in (2, 3)])
        self.assertNotIn('demo-default', json.dumps(findings))
        self.assertNotIn('demo-secret', json.dumps(findings))

    def test_transform_changes_only_literal_values_and_preserves_crlf(self):
        public, changes = transform(self.original, ['web-access-default-metadata'])
        self.assertEqual(public, self.public)
        self.assertEqual(redact_excerpt(self.original), self.public)
        self.assertEqual([r['line'] for r in changes], [2, 3])
        self.assertNotIn('demo-default', json.dumps(changes))
        self.assertNotIn('demo-secret', json.dumps(changes))

    def test_unrelated_roles_usernames_and_dynamic_expressions_are_untouched(self):
        for text in ['admin', 'role = "admin"', 'WEB_USERNAME = "admin"',
                     'self.WEB_PASSWORD = os.getenv("OTHER", "value")',
                     'self.WEB_PASSWORD = os.getenv("WEB_PASSWORD", prefix + "value")',
                     'self.WEB_PASSWORD = os.getenv("WEB_PASSWORD", "value" + suffix)',
                     '# self.WEB_PASSWORD = os.getenv("WEB_PASSWORD", "value")',
                     'self.WEB_PASSWORD = os.getenv("WEB_PASSWORD")']:
            with self.subTest(text=text):
                self.assertEqual(list(web_access_default_spans(text)), [])
                self.assertEqual(redact_excerpt(text), text)
                with self.assertRaisesRegex(ValueError, 'no supported private data'):
                    transform(text, ['web-access-default-metadata'])

    def test_placeholders_do_not_trigger_repeated_redaction(self):
        self.assertEqual(list(web_access_default_spans(self.public)), [])
        self.assertEqual(redact_excerpt(self.public), self.public)
        (self.fixture / 'brief.md').write_bytes(self.public.encode())
        self.assertEqual(scan(self.fixture), [])

    def test_source_bound_minimization_preserves_fidelity_and_is_idempotent(self):
        proof = minimize(self.bundle, 'session')
        self.assertEqual((self.fixture / 'brief.md').read_bytes(), self.public.encode())
        self.assertEqual(minimize(self.bundle, 'session'), proof)
        ledger = build(self.bundle, 'session', 0)
        self.assertEqual(ledger['faithfulness_by_bytes'], 1)
        self.assertLess(ledger['exact_copy_by_bytes'], 1)
        self.assertEqual(ledger['counts']['anonymized'], 1)
        receipt = json.dumps(audit(self.bundle))
        self.assertNotIn('demo-default', receipt)
        self.assertNotIn('demo-secret', receipt)

    def test_unbound_source_edit_is_rejected_before_any_write(self):
        changed = self.original.replace('Budget stays 123', 'Budget stays 999')
        (self.fixture / 'brief.md').write_bytes(changed.encode())
        with self.assertRaisesRegex(ValueError, 'without an exact recovered source'):
            minimize(self.bundle, 'session')
        self.assertEqual((self.fixture / 'brief.md').read_bytes(), changed.encode())
        self.assertFalse((self.bundle / MANIFEST).exists())

    def test_authorization_change_cannot_hide_behind_a_new_public_hash(self):
        minimize(self.bundle, 'session')
        changed = self.public.replace('[11, 22]', '[11, 22, 33]')
        (self.fixture / 'brief.md').write_bytes(changed.encode())
        manifest = json.loads((self.bundle / MANIFEST).read_text())
        manifest['files'][0]['public_sha256'] = sha(changed.encode())
        atomic_json(self.bundle / MANIFEST, manifest)
        with self.assertRaisesRegex(ValueError, 'differs from the verified'):
            build(self.bundle, 'session', 0)

    def test_real_defaults_are_removed_even_when_they_are_short_or_common(self):
        for name, value in [('WEB_PASSWORD', 'admin'), ('WEB_SECRET_KEY', 'change-me-in-production')]:
            text = f'self.{name} = os.getenv("{name}", "{value}")\n'
            result, changes = transform(text, ['web-access-default-metadata'])
            self.assertEqual(result, text.replace(f'"{value}"', f'"[REDACTED_{name}]"'))
            self.assertEqual(len(changes), 1)


if __name__ == '__main__':
    unittest.main()
