"""Login shells see the image's own PATH (Arena C-002/C-032).

Codex runs every command as `/bin/sh -lc`; Debian's /etc/profile resets PATH,
so the venv, Go and Cargo vanished for 5/5 Astra episodes on three tasks.
Executed 2026-10-07 in the pinned python and go bases: without the layer
`sh -lc`/`bash -lc` resolve /usr/local/bin/python and no `go`; with it all
four shells resolve /opt/deps/venv/bin/python and /usr/local/go/bin/go.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline.author import agent_designer, toolchain


class LoginShellPathLayer(unittest.TestCase):
    def test_an_existing_record_generates_the_same_bytes(self):
        doc = toolchain.record("python", language="python")
        self.assertNotIn("login_shell_path", doc)
        self.assertNotIn("/etc/profile.d", toolchain.agent_dockerfile(doc))

    def test_the_layer_follows_every_env_line_and_precedes_the_repo(self):
        for name in ("python", "go", "rust", "node"):
            with self.subTest(name=name):
                doc = toolchain.record(name, language=name, login_shell_path=True)
                self.assertIs(doc["login_shell_path"], True)
                text = toolchain.agent_dockerfile(doc)
                layer = text.index(toolchain.LOGIN_SHELL_PATH_LAYER)
                env_lines = [i for i in range(len(text)) if text.startswith("\nENV ", i)]
                self.assertTrue(all(i < layer for i in env_lines))
                self.assertLess(layer, text.index("COPY repo/"))
                self.assertNotIn("/etc/profile.d", toolchain.verifier_dockerfile(doc))


class NewDraftsDefaultTheLayerOn(unittest.TestCase):
    def setUp(self):
        self.bundle = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bundle, True)
        (self.bundle / "environment/repo").mkdir(parents=True)
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.bundle, moments=[])
        self.draft = self.bundle / "draft.json"
        self.handlers = agent_designer._handlers(self.bundle, extraction, {"messages": []}, self.draft,
                                                 log=self.bundle / "authoring/log")

    def staged(self):
        return json.loads(self.draft.read_text()).get("toolchain")

    def test_a_staged_toolchain_gets_the_layer(self):
        self.handlers["stage_spec"](path=["toolchain"], value={"name": "python"})
        self.assertEqual(self.staged(), {"name": "python", "login_shell_path": True})

    def test_an_explicit_false_is_kept(self):
        self.handlers["stage_spec"](path=["toolchain"], value={"name": "go", "login_shell_path": False})
        self.assertIs(self.staged()["login_shell_path"], False)

    def test_other_sections_do_not_touch_the_toolchain(self):
        self.handlers["stage_spec"](path=["name"], value="x-task")
        self.assertIsNone(self.staged())


if __name__ == "__main__":
    unittest.main()
