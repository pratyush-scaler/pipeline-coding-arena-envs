"""A long shell variant runs from a file in the container (2026-10-30: `sh -c` with a script past the kernel's
128 KiB single-argument limit failed with E2BIG, so no counterexample of the bash test design ran)."""
import unittest

from pipeline.author import sandbox


class FakeContainer:
    def __init__(self):
        self.files, self.commands = {}, []

    def put_bytes(self, data, guest, mode=0o644):
        self.files[guest] = data

    def exec(self, cmd, **kwargs):
        self.commands.append(cmd)
        return {"exit_code": 0, "stdout": "", "stderr": ""}


class ShellVariants(unittest.TestCase):
    def test_a_short_script_is_passed_inline(self):
        box = FakeContainer()
        sandbox.Variant("shell", shell="echo ok").apply(box, "01-fix")
        self.assertEqual(box.commands, ["echo ok"])
        self.assertEqual(box.files, {})

    def test_a_long_script_is_written_into_the_container_and_run_from_there(self):
        box = FakeContainer()
        script = "set -e\n" + "echo " + "x" * (sandbox.SHELL_ARG_MAX + 1)
        sandbox.Variant("shell", shell=script).apply(box, "01-fix")
        [(guest, data)] = box.files.items()
        self.assertEqual(data, script.encode("utf-8"))
        self.assertEqual(box.commands, [f"sh {guest}"])
        self.assertLess(sandbox.SHELL_ARG_MAX, 131072)          # under MAX_ARG_STRLEN


if __name__ == "__main__":
    unittest.main()
