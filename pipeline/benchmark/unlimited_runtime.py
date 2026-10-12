"""Explicit no-deadline entrypoint for the pinned native benchmark runtime.

This process-local override disables Harbor's agent, step, setup, build and
verifier phase deadlines. It leaves the installed runtime and task bundles
unchanged. Individual transport/health-check fault detection is independent
of these phase deadlines.
"""
from importlib.metadata import version


def install():
    if version("harbor") != "0.22.0":
        raise RuntimeError("The unlimited runtime requires Harbor 0.22.0")
    from harbor.trial.trial import Trial

    def no_deadline(self, *, base_sec, max_sec=None, multiplier=None):
        return None

    Trial._resolve_timeout_sec = no_deadline


def main():
    install()
    from harbor.cli.main import app
    app()


if __name__ == "__main__":
    main()
