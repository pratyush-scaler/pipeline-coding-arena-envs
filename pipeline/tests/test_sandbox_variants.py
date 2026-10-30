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


class RunObservability(unittest.TestCase):
    def test_coder_runs_record_transport_telemetry_and_dump_stacks_on_usr1(self):
        from pipeline.benchmark import coder_run
        self.assertIn('env.setdefault("ARENA_TRANSPORT_OBSERVATIONS", str(run / "transport.jsonl"))', coder_run.LAUNCHER)
        self.assertIn('env.setdefault("ARENA_STACK_DUMPS", "1")', coder_run.LAUNCHER)
        compile(coder_run.LAUNCHER, "launcher", "exec")

    def test_a_usr1_signal_dumps_every_thread(self):
        import os, subprocess, sys
        code = ("import os, signal, sys, time, pipeline; os.kill(os.getpid(), signal.SIGUSR1); time.sleep(0.2)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "ARENA_STACK_DUMPS": "1"}, cwd=os.getcwd())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("most recent call first", out.stderr)
