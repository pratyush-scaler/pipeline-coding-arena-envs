"""The verifier's added test dependencies must not invalidate the agent lock."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from pipeline.author import toolchain


class NodeVerifierLock(unittest.TestCase):
    def test_reconciliation_is_confined_to_verifier_recipes(self):
        for name in ("node", "browser"):
            with self.subTest(toolchain=name):
                doc = toolchain.record(name, language="javascript")
                agent = toolchain.agent_dockerfile(doc)
                verifier = toolchain.verifier_dockerfile(doc)
                self.assertIn("npm ci --no-audit --no-fund --ignore-scripts", agent)
                self.assertNotIn("--package-lock-only", agent)
                self.assertLess(verifier.index("npm install --package-lock-only"), verifier.index("npm ci"))

    @unittest.skipUnless(shutil.which("npm") and shutil.which("node"), "needs npm and node for offline regression")
    def test_private_lock_reconciliation_preserves_locked_application(self):
        with tempfile.TemporaryDirectory(prefix="arena-node-lock-") as temporary:
            root = Path(temporary)
            source = root / "agent-manifest"
            source.mkdir()
            user_config = root / "empty-user.npmrc"
            global_config = root / "empty-global.npmrc"
            user_config.write_text("")
            global_config.write_text("")
            environment = {**os.environ, "NPM_CONFIG_OFFLINE": "true", "NPM_CONFIG_CACHE": str(root / "cache"),
                           "NPM_CONFIG_USERCONFIG": str(user_config), "NPM_CONFIG_GLOBALCONFIG": str(global_config)}

            def run(argv, directory, success=True):
                result = subprocess.run(argv, cwd=directory, env=environment, text=True,
                                        capture_output=True, timeout=90)
                if success:
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                return result

            for name, version in (("locked-app", "1.2.3"), ("test-tool", "4.5.6")):
                directory = source / "vendor" / name
                directory.mkdir(parents=True)
                package = {"name": name, "version": version, "main": "index.js"}
                if name == "test-tool":
                    package["bin"] = {"test-tool": "bin.js"}
                    (directory / "bin.js").write_text("#!/usr/bin/env node\nconsole.log('test-tool 4.5.6');\n")
                    (directory / "bin.js").chmod(0o755)
                (directory / "package.json").write_text(json.dumps(package))
                (directory / "index.js").write_text(f"module.exports = '{version}';\n")

            manifest = {"name": "offline-node-lock", "version": "1.0.0", "private": True,
                        "dependencies": {"locked-app": "file:vendor/locked-app"}}
            (source / "package.json").write_text(json.dumps(manifest))
            flags = ["--offline", "--ignore-scripts", "--no-audit", "--no-fund"]
            run(["npm", "install", "--package-lock-only", *flags], source)
            original = {name: (source / name).read_bytes() for name in ("package.json", "package-lock.json")}
            locked_app = json.loads(original["package-lock.json"])["packages"]["vendor/locked-app"]
            verifier = root / "verifier-manifest"
            shutil.copytree(source, verifier)
            manifest["devDependencies"] = {"test-tool": "file:vendor/test-tool"}
            (verifier / "package.json").write_text(json.dumps(manifest))
            self.assertNotEqual(run(["npm", "ci", *flags], verifier, success=False).returncode, 0)

            # Run the actual generator recipe through its install boundary;
            # moving directories and cleaning the global npm cache are Docker-only.
            recipe = toolchain.TOOLCHAINS["node"].verifier_install.split(" && mv node_modules", 1)[0]
            recipe = recipe.replace("cd /opt/deps/manifest", "cd " + shlex.quote(str(verifier)), 1)
            run(["sh", "-c", recipe], verifier)
            installed_lock = json.loads((verifier / "package-lock.json").read_text())
            self.assertEqual(installed_lock["packages"]["vendor/locked-app"], locked_app)
            app = run(["node", "-e", "console.log(require('locked-app'))"], verifier)
            self.assertEqual(app.stdout.strip(), "1.2.3")
            binary = run([str(verifier / "node_modules/.bin/test-tool")], verifier)
            self.assertEqual(binary.stdout.strip(), "test-tool 4.5.6")

            # The original agent manifest still installs strictly and contains no test tool.
            run(["npm", "ci", *flags], source)
            self.assertFalse((source / "node_modules/test-tool").exists())
            self.assertEqual({name: (source / name).read_bytes() for name in original}, original)


if __name__ == "__main__":
    unittest.main()
