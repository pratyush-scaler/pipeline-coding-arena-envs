"""Typed access-data minimization cannot launder changed business facts."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pipeline.artifacts import atomic_json
from pipeline.author.build import build as export
from pipeline.ledger.faithfulness import build
from pipeline.ledger.redaction import MANIFEST, SCHEMA, audit, combine, minimize, sha, transform
from pipeline.privacy import ABSENCE_NOTE, SSH_EXAMPLE, health_absence_spans, redact_excerpt, scan, ssh_endpoints
from pipeline.tests.test_production import synthetic_bundle


class AccessRedaction(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bundle = synthetic_bundle(self.root)
        self.fixture = self.bundle / "environment/repo"
        self.original = "Budget stays 123.\r\nAccess: `ssh private-user@10.23.45.67 -p 2222`\r\nColour #123456.\r\n"
        self.public, self.changes = transform(self.original, ["ssh-access-metadata"])
        (self.fixture / "brief.md").write_bytes(self.public.encode())
        originals = {str(p.relative_to(self.fixture)): p.read_text()
                     for p in self.fixture.rglob("*") if p.is_file()}
        originals["brief.md"] = self.original
        trace = {"session_id": "session", "messages": [{"role": "assistant", "content": [
            {"type": "tool-write_file", "input": {"path": name, "content": text}}
            for name, text in originals.items()]}]}
        atomic_json(self.root / "dump/sessions/session/data.json", {"traces": [trace]})
        self.enterContext(patch.dict(os.environ, ARENA_DUMP=str(self.root / "dump")))
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        spec.update(source={"session_id": "session", "seed_index": 0}, _producer={"model": "author"})
        export(spec, self.bundle)
        self.manifest = {"schema": SCHEMA, "files": [{"path": "brief.md",
            "citation": "session::msg0::brief.md", "source_sha256": sha(self.original.encode()),
            "public_sha256": sha(self.public.encode()), "kinds": ["ssh-access-metadata"]}]}
        self.save()

    def save(self):
        atomic_json(self.bundle / MANIFEST, self.manifest)

    def test_redaction_preserves_other_bytes_and_omits_removed_values(self):
        self.assertEqual(self.public, self.original.replace("ssh private-user@10.23.45.67 -p 2222", SSH_EXAMPLE))
        self.assertEqual(redact_excerpt(self.original), self.public)
        self.assertEqual(self.changes[0]["line"], 2)
        receipt = json.dumps(audit(self.bundle))
        self.assertNotIn("10.23.45.67", receipt)
        self.assertNotIn("private-user", receipt)
        self.assertEqual(scan(self.fixture), [])

    def test_privacy_preflight_reports_location_without_access_values(self):
        (self.fixture / "brief.md").write_bytes(self.original.encode())
        self.assertEqual(scan(self.fixture), [{"file": "brief.md", "kind": "ssh-access-metadata", "line": 2}])

    def test_pre_author_minimization_is_source_bound_and_idempotent(self):
        (self.bundle / MANIFEST).unlink()
        (self.fixture / "brief.md").write_bytes(self.original.encode())
        receipt = minimize(self.bundle, "session")
        self.assertEqual((self.fixture / "brief.md").read_bytes(), self.public.encode())
        self.assertEqual(minimize(self.bundle, "session"), receipt)
        self.assertEqual(build(self.bundle, "session", 0)["faithfulness_by_bytes"], 1)

    def test_unbound_private_material_is_not_changed_or_sent_to_an_author(self):
        (self.bundle / MANIFEST).unlink()
        changed = self.original.replace("Budget stays 123", "Budget stays 999")
        (self.fixture / "brief.md").write_bytes(changed.encode())
        with self.assertRaisesRegex(ValueError, "without an exact recovered source"):
            minimize(self.bundle, "session")
        self.assertEqual((self.fixture / "brief.md").read_bytes(), changed.encode())
        self.assertFalse((self.bundle / MANIFEST).exists())

    def test_documentation_local_and_non_command_addresses_are_not_rewritten(self):
        text = "ssh sample@192.0.2.4\nssh sample@198.51.100.4\nssh sample@203.0.113.4\nssh local@127.0.0.1\nIP 10.23.45.67\nssh invalid@999.1.2.3\n"
        self.assertEqual(list(ssh_endpoints(text)), [])
        self.assertEqual(redact_excerpt(text), text)

    def test_author_correction_surfaces_mask_access_data_before_truncation(self):
        from pipeline.author.agent_designer import _handlers, _opening
        user = "Keep both lines.\n\n" + "x" * 258 + " ssh private-user@10.23.45.67 -p 2222"
        extraction = SimpleNamespace(session_id="session", seed_index=0,
            moments=[{"msg": 0, "signal": "steerability", "user": user,
                      "prior_assistant": "x" * 2380 + " ssh private-user@10.23.45.67 -p 2222"}])
        row = {"session_id": "session", "seed_index": 0, "tier": 1,
               "classification": {"failure_mode": "ssh private-user@10.23.45.67 -p 2222"}}
        corrected = _handlers(self.bundle, extraction, {"messages": []})["list_corrections"]()
        self.assertTrue(corrected["corrections"][0]["user"].startswith("Keep both lines.\n\n"))
        for payload in (json.dumps(corrected), _opening(row, extraction, self.bundle)):
            self.assertNotIn("private-user", payload)
            self.assertNotIn("10.23.45.67", payload)
        self.assertIn("private-user", extraction.moments[0]["user"])

    def test_verified_redaction_preserves_fidelity_without_exact_copy_credit(self):
        ledger = build(self.bundle, "session", 0)
        self.assertEqual(ledger["faithfulness_by_bytes"], 1)
        self.assertLess(ledger["exact_copy_by_bytes"], 1)
        self.assertEqual(ledger["counts"]["anonymized"], 1)
        (self.bundle / MANIFEST).unlink()
        self.assertEqual(build(self.bundle, "session", 0)["counts"]["anonymized"], 0)

    def test_business_edit_cannot_be_hidden_by_updating_the_public_hash(self):
        changed = self.public.replace("Budget stays 123", "Budget stays 999")
        (self.fixture / "brief.md").write_bytes(changed.encode())
        self.manifest["files"][0]["public_sha256"] = sha(changed.encode())
        self.save()
        with self.assertRaisesRegex(ValueError, "differs from the verified"):
            build(self.bundle, "session", 0)

    def test_untrusted_source_and_author_supplied_replacements_are_rejected(self):
        original = copy.deepcopy(self.manifest)
        mutations = [
            lambda m: m["files"][0].update(citation="other::msg0::brief.md"),
            lambda m: m["files"][0].update(source_sha256="a" * 64),
            lambda m: m["files"][0].update(replacement="approved"),
            lambda m: m["files"][0].update(path="../brief.md"),
            lambda m: m["files"].append(copy.deepcopy(m["files"][0])),
            lambda m: m["files"][0].update(kinds=["arbitrary-text"]),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(mutation=i):
                self.manifest = copy.deepcopy(original)
                mutate(self.manifest)
                self.save()
                with self.assertRaises((ValueError, OSError)):
                    build(self.bundle, "session", 0)
        for kinds in ([], [[]], [None], ["ssh-access-metadata"] * 2):
            with self.subTest(kinds=kinds), self.assertRaises(ValueError):
                transform(self.original, kinds)

    def test_privacy_policies_cannot_silently_compose_over_the_same_file(self):
        proof = {"policy": SCHEMA, "manifest_sha256": "a" * 64, "files": {"brief.md": {}}}
        with self.assertRaisesRegex(ValueError, "explicit composition"):
            combine(proof, proof)
        other = {**proof, "files": {"schedule.md": {}}}
        self.assertEqual(set(combine(proof, other)["files"]), {"brief.md", "schedule.md"})

    def test_review_receipt_is_private_sanitized_and_bound_to_trusted_source(self):
        from pipeline.review import review, review_problems
        atomic_json(self.bundle / "provenance/ledger.json", build(self.bundle, "session", 0))
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        def critic(_prompt, public, work, model, _tag, max_steps):
            receipt = (public / "privacy-audit.json").read_text()
            self.assertNotIn("10.23.45.67", receipt)
            provenance = (public / "source-provenance.json").read_text()
            self.assertNotIn("10.23.45.67", provenance)
            self.assertNotIn("private-user", provenance)
            self.assertFalse((self.fixture / "privacy-audit.json").exists())
            shutil.copytree(public, work / "workspace")
            atomic_json(work / "conversation.json", [])
            atomic_json(work / "request-config.json", {"model": model, "max_steps": max_steps})
            atomic_json(work / "deliverables/review.json", {"verdict": "pass", "checks": [
                {"id": c["id"], "verdict": "pass", "reason": "The task's source facts remain intact under the typed access redaction.",
                 "citations": [{"file": "fixture/sample.csv", "quote": "2024-01-05,120.00,rent\n2024-01-20,35.50,groceries"}]}
                for c in spec["checks"]], "ambiguities": [], "substantive_gaps": [], "shortcut_risks": [],
                "privacy_findings": [], "difficulty_assessment": "Synthetic verification only; no empirical difficulty assertion.",
                "novelty_assessment": "Synthetic verification only; no corpus novelty assertion.", "untested_surfaces": ["No calibration."],
                # one exhaustive round (2026-10-27): every inventoried function and script gets a verdict
                "helper_audit": [{"name": e["name"], "verdict": "pass"} for e in
                                 json.loads((public / "suite-inventory.json").read_text())["entries"]]})
            return SimpleNamespace(error=None, stop_reason="completed", tool_calls=1), None
        with patch("pipeline.calibrate.run._rollout_for", return_value=critic), \
             patch("pipeline.corpus.review_context", return_value={"peers": []}):
            report = review(self.bundle, "critic")
            self.assertEqual(report["errors"], [])
            rel = "workspace/privacy-audit.json"
            path = self.bundle / report["work_dir"] / rel
            atomic_json(path, {"fabricated": "source proof"})
            report["artifacts"][rel] = sha(path.read_bytes())
            self.assertIn("privacy reviewer evidence differs from the trusted source transformation",
                          review_problems(self.bundle, report))

    def test_provenance_receipt_replays_redaction_without_raw_trace_content(self):
        from pipeline.review import source_provenance
        atomic_json(self.bundle / "provenance/ledger.json", build(self.bundle, "session", 0))
        receipt = source_provenance(self.bundle)
        entry = next(f for f in receipt["files"] if f["path"] == "brief.md")
        self.assertEqual(entry["origin"], "anonymized")
        self.assertEqual(entry["sha256"], sha(self.public.encode()))
        self.assertEqual(entry["citation_sha256"], sha(b"session::msg0::brief.md"))
        text = json.dumps(receipt)
        for private in ("10.23.45.67", "private-user", "Budget stays 123", "session::msg0"):
            self.assertNotIn(private, text)
        self.assertFalse((self.fixture / "source-provenance.json").exists())

    def test_stale_provenance_stops_before_any_reviewer_request(self):
        from pipeline.review import review
        ledger = build(self.bundle, "session", 0)
        ledger["entries"][0]["origin"] = "authored"
        atomic_json(self.bundle / "provenance/ledger.json", ledger)
        with patch("pipeline.calibrate.run._rollout_for") as provider:
            with self.assertRaisesRegex(ValueError, "source provenance is unverified"):
                review(self.bundle, "critic")
            provider.assert_not_called()

    def test_receipt_cannot_reuse_a_ledger_after_the_original_trace_changes(self):
        from pipeline.review import source_provenance
        atomic_json(self.bundle / "provenance/ledger.json", build(self.bundle, "session", 0))
        path = self.root / "dump/sessions/session/data.json"
        trace = json.loads(path.read_text())
        trace["traces"][0]["messages"].append({"role": "user", "content": [
            {"type": "text", "text": "Additional source evidence."}]})
        atomic_json(path, trace)
        with self.assertRaisesRegex(ValueError, "source provenance is unverified"):
            source_provenance(self.bundle)


class HealthAbsenceRedaction(unittest.TestCase):
    def test_plain_and_embedded_arrays_preserve_syntax_counts_and_business_facts(self):
        source = 'const data = {total: 5, absentees: ["Sample Person - sick leave", "Example Worker - medical leave", "vacant"]};'
        public = source.replace('Sample Person - sick leave', ABSENCE_NOTE).replace('Example Worker - medical leave', ABSENCE_NOTE)
        for original, expected in ((source, public),
                                   (json.dumps({'functionCode': source}), json.dumps({'functionCode': public})),
                                   (json.dumps({'absentees': ['Sample Person - sick leave'], 'total': 5}),
                                    json.dumps({'absentees': [ABSENCE_NOTE], 'total': 5}))):
            with self.subTest(embedded='functionCode' in original):
                result, changes = transform(original, ['health-absence-metadata'])
                self.assertEqual(result, expected)
                self.assertEqual(redact_excerpt(original), expected)
                self.assertEqual(list(health_absence_spans(result)), [])
                self.assertNotIn('Sample Person', json.dumps(changes))
                if original.startswith('{'):
                    json.loads(result)

    def test_policy_does_not_rewrite_general_rules_unrelated_arrays_or_expressions(self):
        texts = ['Sick leave requires notice. Sample Person - sick leave.',
                 'rules: ["Sample Person - sick leave"]',
                 'absentees: [lookup("Sample Person - sick leave")]',
                 'absentees: ["Sample Person - sick leave" + suffix]',
                 'absentees: ["Sample Person - annual leave"]']
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(list(health_absence_spans(text)), [])
                with self.assertRaisesRegex(ValueError, 'no supported private data'):
                    transform(text, ['health-absence-metadata'])

    def test_previously_redacted_entry_does_not_hide_a_new_health_note(self):
        original = f'absentees: ["{ABSENCE_NOTE}", "Example Worker - medical leave"]'
        transformed, edits = transform(original, ['health-absence-metadata'])
        self.assertEqual(len(edits), 1)
        self.assertEqual(transformed, f'absentees: ["{ABSENCE_NOTE}", "{ABSENCE_NOTE}"]')

    def test_source_bound_replay_preserves_fidelity_and_rejects_business_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = synthetic_bundle(root)
            fixture = bundle / 'environment/repo'
            original = 'Budget stays 123.\nabsentees: ["Sample Person - sick leave"]\n'
            (fixture / 'brief.md').write_text(original)
            trace = {'session_id': 'session', 'messages': [{'role': 'assistant', 'content': [
                {'type': 'tool-write_file', 'input': {'path': str(p.relative_to(fixture)), 'content': p.read_text()}}
                for p in fixture.rglob('*') if p.is_file()]}]}
            atomic_json(root / 'dump/sessions/session/data.json', {'traces': [trace]})
            spec = json.loads((bundle / 'authoring/spec.json').read_text())
            spec.update(source={'session_id': 'session', 'seed_index': 0}, _producer={'model': 'author'})
            export(spec, bundle)
            with patch.dict(os.environ, ARENA_DUMP=str(root / 'dump')):
                self.assertEqual(scan(fixture), [{'file': 'brief.md', 'kind': 'health-absence-metadata', 'line': 2}])
                receipt = minimize(bundle, 'session')
                self.assertNotIn('Sample Person', json.dumps(receipt))
                self.assertEqual(scan(fixture), [])
                self.assertEqual(minimize(bundle, 'session'), receipt)
                ledger = build(bundle, 'session', 0)
                self.assertEqual(ledger['faithfulness_by_bytes'], 1)
                self.assertLess(ledger['exact_copy_by_bytes'], 1)
                self.assertEqual(ledger['counts']['anonymized'], 1)
                changed = (fixture / 'brief.md').read_text().replace('123', '999')
                (fixture / 'brief.md').write_text(changed)
                manifest = json.loads((bundle / MANIFEST).read_text())
                manifest['files'][0]['public_sha256'] = sha(changed.encode())
                atomic_json(bundle / MANIFEST, manifest)
                with self.assertRaisesRegex(ValueError, 'differs from the verified'):
                    build(bundle, 'session', 0)


if __name__ == "__main__":
    unittest.main()
