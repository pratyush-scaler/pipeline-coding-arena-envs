"""Arena coding/non-coding environment pipeline."""
import os as _os

if _os.environ.get("ARENA_STACK_DUMPS") == "1":
    # `kill -USR1 <pid>` writes every thread's Python stack to stderr, which an unattended run sends to its run.log:
    # a process that goes idle can be read without ptrace, which the Coder hosts refuse (2026-10-30: a design sat
    # idle for 35 minutes after a one-second tool, no connection and no child, and nothing could say where).
    import faulthandler as _faulthandler
    import signal as _signal
    _faulthandler.register(_signal.SIGUSR1, all_threads=True)
