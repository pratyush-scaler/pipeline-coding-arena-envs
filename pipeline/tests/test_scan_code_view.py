"""Comments and inert docstrings cannot run, so the literal scans must not refuse them (Arena 2026-10-11).

R01/R06: `// ARENA_` in a changed JS file; R04/R08: `eval(` in a new Python comment; R13: `# import builtins`;
R12: `compile(` bound by `from re import compile`. Every reach the scans exist for -- a string the code uses, real
code after a regex or inside a template, an unlexable file -- must still be refused.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline.author.grader_runtime import grade

BENIGN = {
    "hangar.js": "// ARENA_WIDTH is the playfield width\nconst W = 900;\n",
    "server.js": "const url = 'http://localhost:3000'; // ARENA_ note: not harness access\nmodule.exports = url;\n",
    "block.js": "/* ARENA_SCALE, documented here only */\nexport const s = 2;\n",
    "regex.js": "const re = /\\/\\//g; // ARENA_ after a regex holding slashes\nexport default re;\n",
    "tmpl.js": "const t = `a${1 + 1}b`; // ARENA_ after a template\nexport { t };\n",
    "smali_dump.py": "# The old version used eval( on user input; this one never does.\nimport os\nprint(os.sep)\n",
    "fusion.py": "def f(x):\n    \"\"\"Never eval( or exec( anything here.\"\"\"\n    return x + 1\n",
    "orkit.py": "# import builtins  -- removed on purpose\nimport json\nprint(json.dumps({}))\n",
    "logcheck.py": "from re import compile\nPAT = compile(r\"^\\d{4}-\\d{2}-\\d{2}$\")\nprint(bool(PAT.match('2026-10-11')))\n",
    "run.sh": "#!/bin/bash\n# ARENA_ mentioned in a shell comment\necho ok\n",
}

REFUSED = {
    "env.js": "const s = process.env[\"ARENA_STEP\"];\n",
    "fake_comment.js": "const s = \"// ARENA_STEP\";\n",
    "after_regex.js": "const re = /a/; const p = require('fs').readFileSync('/tests/checks.json');\n",
    "in_template.js": "const x = 1; const p = `${x}/tests/checks.json`;\n",
    "unlexable.js": "const s = 'unterminated; // ARENA_STEP\n",
    "eval_code.py": "x = eval('1+1')\n",
    "builtins_code.py": "import builtins\n",
    "plain_compile.py": "code = compile('1', 'f', 'eval')\n",
    "rebound_compile.py": "from re import compile\ndef compile(s):\n    return s\nc = compile('x')\n",
    "env_string.py": "import os\nstep = os.environ['ARENA_STEP']\n",
    "shell_reach.sh": "#!/bin/bash\ncat /tests/checks.json\n",
}


class CodeViewScan(unittest.TestCase):
    def scan(self, name: str, body: str) -> list[str]:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / name).write_text(body, encoding="utf-8")
        return grade.introspection_problems(root, {})

    def test_comments_and_docstrings_are_not_refused(self):
        for name, body in BENIGN.items():
            with self.subTest(name=name):
                self.assertEqual(self.scan(name, body), [])

    def test_real_reaches_are_still_refused(self):
        for name, body in REFUSED.items():
            with self.subTest(name=name):
                self.assertTrue(self.scan(name, body), name)

    def test_blanking_keeps_every_byte_offset(self):
        text = "x = 1  # é eval(\ny = '//'  # ARENA_\n"
        view = grade.code_view(text, ".py")
        self.assertEqual(len(view.encode("utf-8")), len(text.encode("utf-8")))   # byte for byte
        self.assertIn("y = '//'", view)
        self.assertNotIn("eval(", view)

    def test_bare_cr_files_are_scanned_raw(self):
        self.assertEqual(grade._python_inert_spans("a = 1\r# eval(\r"), [])


if __name__ == "__main__":
    unittest.main()
