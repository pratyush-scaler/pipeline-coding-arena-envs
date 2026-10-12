"""Reviewed Node-only specialization of the common pinned grader.

Non-Node bundles continue to ship the exact common runtime bytes.
"""

BOOTSTRAP = r'''
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';

const config = JSON.parse(readFileSync(0, 'utf8'));
if (!Array.isArray(config.nodes) || config.nodes.length === 0 ||
    new Set(config.nodes).size !== config.nodes.length || !config.nonce) {
  throw new Error('No unique private test selection and run nonce');
}
const temporary = mkdtempSync(join(tmpdir(), 'arena-node-'));
try {
  const wrappers = config.nodes.map((selected, index) => {
    const wrapper = join(temporary, `selection-${index}.mjs`);
    // A separate native worker per selected file preserves Node's normal
    // process isolation. The parent configuration and report nonce are not
    // present in the child wrapper, argv or environment.
    const source = "import { describe } from 'node:test';\n" +
      "import assert from 'node:assert';\n" +
      "import strictAssert from 'node:assert/strict';\n" +
      "Object.freeze(assert); Object.freeze(strictAssert);\n" +
      "import { pathToFileURL } from 'node:url';\n" +
      `describe(${JSON.stringify(selected)}, async () => { ` +
      `await import(pathToFileURL(${JSON.stringify(selected)}).href); });\n`;
    writeFileSync(wrapper, source, { mode: 0o600 });
    return wrapper;
  });
  // Invoke the normal native CLI explicitly, without inheriting the outer
  // bootstrap's eval/module flags. The CLI's official JUnit reporter receives
  // child stdout as test events; child text is never the report channel.
  const result = spawnSync(process.execPath,
    ['--test', '--test-concurrency=1', '--test-reporter=junit', ...wrappers],
    { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
  if (result.error || result.signal || result.status === null) {
    throw result.error || new Error('Native test runner did not terminate normally');
  }
  const xml = result.stdout;
  writeFileSync(config.report, xml, { mode: 0o600 });
  writeFileSync(config.proof, JSON.stringify({
    schema: 'arena-coding/node-selection-report/1', nonce: config.nonce,
    nodes: config.nodes, report_sha256: createHash('sha256').update(xml).digest('hex')
  }), { mode: 0o600 });
  if (result.stderr) process.stderr.write(result.stderr);
  process.exitCode = result.status;
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
'''

HELPER = r'''
def _node_report_counts(report_dir: Path, nonce: str, select: list[str]) -> dict | None:
    """The parent's native report, bound to this private selection and nonce."""
    import xml.etree.ElementTree as ET
    report = report_dir / "junit.xml"
    try:
        expected = [_suite(item) for item in select]
        proof = _load_json(report_dir / "node-selection.json")
        if (not nonce or not expected or len(set(expected)) != len(expected)
                or not isinstance(proof, dict)
                or proof.get("schema") != "arena-coding/node-selection-report/1"
                or proof.get("nonce") != nonce or proof.get("nodes") != expected
                or proof.get("report_sha256") != _sha256(report)):
            return None
        root = ET.parse(report).getroot()
    except (OSError, ValueError, ET.ParseError):
        return None
    total = {"passed": 0, "failed": 0, "skipped": 0}
    for selected in expected:
        wrappers = [s for s in root.iter("testsuite") if s.get("name") == selected]
        if len(wrappers) != 1:
            return None
        cases = list(wrappers[0].iter("testcase"))
        # An imported empty module can produce a native file-completion case;
        # it cannot produce a real case inside the selected private envelope.
        if not cases:
            return None
        suite_failures = sum(int(s.get("failures", 0) or 0) + int(s.get("errors", 0) or 0)
                             for s in wrappers[0].iter("testsuite"))
        failed = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
        skipped = sum(c.find("skipped") is not None for c in cases)
        total["failed"] += max(failed, suite_failures)
        total["skipped"] += skipped
        total["passed"] += sum(c.find("failure") is None and c.find("error") is None
                               and c.find("skipped") is None for c in cases)
    return total

'''

def render(source: bytes) -> bytes:
    BASE = source.decode('utf-8')
    if 'NODE_TEST_BOOTSTRAP =' in BASE or 'def _node_report_counts(' in BASE:
        raise ValueError('The common runtime already contains a Node specialization')
    shadow = BASE.replace('\ndef test_counts(', '\n' + HELPER + '\ndef test_counts(', 1)
    old_command = '    if kind == "node-test":\n        junit = ["--test-reporter=junit", f"--test-reporter-destination={report_dir / \'junit.xml\'}"] if reports else []\n        return ["node", "--test", *junit, *[_suite(p) for p in runner.get("select", [])]], False\n'
    new_command = '    if kind == "node-test":\n        if reports:\n            return ["node", "--input-type=module", "-e", NODE_TEST_BOOTSTRAP], False\n        return ["node", "--test", *[_suite(p) for p in runner.get("select", [])]], False\n'
    assert old_command in shadow
    shadow = shadow.replace(old_command, new_command, 1)
    start = shadow.index('    if kind == "node-test":\n', shadow.index('def test_counts('))
    end = shadow.index('    if kind == "jest":\n', start)
    shadow = shadow[:start] + '    if kind == "node-test":\n        return _node_report_counts(report_dir, nonce, select)\n' + shadow[end:]
    needle = '        elif runner["type"] == "unittest":\n            stdin_payload = json.dumps({"nodes": select, "report": str(report_dir / "unittest.json"),\n                                        "nonce": nonce, "suites": str(HERE / "suites")}) + "\\n"\n'
    replacement = needle + '        elif runner["type"] == "node-test":\n            stdin_payload = json.dumps({"nodes": [_suite(item) for item in select],\n                                        "report": str(report_dir / "junit.xml"),\n                                        "proof": str(report_dir / "node-selection.json"),\n                                        "nonce": nonce}) + "\\n"\n'
    assert needle in shadow
    shadow = shadow.replace(needle, replacement, 1)
    shadow = shadow.replace('\ndef command_for(', '\nNODE_TEST_BOOTSTRAP = ' + repr(BOOTSTRAP) + '\n\ndef command_for(', 1)
    assert 'NODE_TEST_BOOTSTRAP =' in shadow
    compile(shadow, 'reviewed-node-grader.py', 'exec')
    return shadow.encode('utf-8')
