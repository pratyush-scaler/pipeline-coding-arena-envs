"""The email pattern finds addresses, not things shaped like them (build
2026-09-26: a systemd unit excluded a-019; decorators after an escaped newline
in a JSON projection stalled a-027's review)."""
import unittest

from pipeline.privacy import PATTERNS, _scan_text, redact_excerpt

EMAIL = PATTERNS["email"]


class EmailShape(unittest.TestCase):
    def test_non_addresses_are_not_email(self):
        for text in ("/etc/systemd/system/sshd-keygen@.service.wants/sshd-keygen.service",
                     '{"text": "import app\\n@app.get(\\"/health\\")\\n@pytest.fixture"}',
                     "getty@tty1.service", "user@-bad.example"):
            with self.subTest(text=text):
                self.assertEqual(_scan_text("f.txt", text, None), [])
                self.assertEqual(redact_excerpt(text), text)

    def test_real_addresses_are_still_found(self):
        for text, found in (("write to jane.doe@mail.co.uk today", "jane.doe@mail.co.uk"),
                            ('{"text": "Contact:\\nfulvio.rossi@gbf-meccanica.it"}',
                             "fulvio.rossi@gbf-meccanica.it"),
                            ("a+b@sub-domain.example.org", "a+b@sub-domain.example.org")):
            with self.subTest(text=text):
                self.assertEqual([m.group(0) for m in EMAIL.finditer(text)], [found])
        self.assertEqual([f["kind"] for f in _scan_text("x.json", '"\\nrossi@azienda.it"', None)],
                         ["email"])
        self.assertIn("[REDACTED_EMAIL]", redact_excerpt("mail rossi@azienda.it now"))
        self.assertIn("test@example.com", redact_excerpt("mail test@example.com now"))


if __name__ == "__main__":
    unittest.main()
