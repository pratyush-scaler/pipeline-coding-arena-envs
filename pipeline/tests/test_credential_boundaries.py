"""Credential scans distinguish tokens from suffixes of ordinary identifiers."""
from pathlib import Path
import tempfile
import unittest

from pipeline.privacy import redact_excerpt, scan


class CredentialBoundaries(unittest.TestCase):
    def test_long_risk_task_and_mask_identifiers_are_not_credentials(self):
        document = '\n'.join([
            '<h3 id="d7-risk-warnings-specific-to-intraday">Risk warnings</h3>',
            '<div class="task-concurrency-schedule-controller-build">Schedule</div>',
            'mask-composite-css-style-evaluation',
        ])
        self.assertEqual(redact_excerpt(document), document)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'paper.html').write_text(document)
            self.assertEqual(scan(root), [])

    def test_actual_key_shapes_remain_detected_in_common_wrappers(self):
        keys = ['sk-' + 'a' * 32, 'sk-ant-api03-' + 'b' * 48,
                'sk-proj-' + 'c' * 48, 'AKIA' + 'A' * 16,
                '-----BEGIN PRIVATE KEY-----']
        templates = ['{}', 'key="{}"', '{{"token":"{}"}}',
                     '<code>{}</code>', 'Authorization: Bearer {}']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'source.txt'
            for key in keys:
                for template in templates:
                    with self.subTest(key_shape=key[:7], template=template):
                        text = template.format(key)
                        path.write_text(text)
                        findings = scan(root)
                        self.assertTrue(any(f['kind'] == 'credential' for f in findings))
                        self.assertNotIn(key, redact_excerpt(text))
                        self.assertNotIn(key, str(findings))

    def test_explicit_provider_prefixes_still_match_inside_surrounding_text(self):
        for key in ('sk-ant-api03-' + 'b' * 48, 'sk-proj-' + 'c' * 48):
            with self.subTest(key_shape=key[:8]):
                text = 'embedded_prefix' + key
                self.assertEqual(redact_excerpt(text), 'embedded_prefix[REDACTED_CREDENTIAL]')


if __name__ == '__main__':
    unittest.main()
