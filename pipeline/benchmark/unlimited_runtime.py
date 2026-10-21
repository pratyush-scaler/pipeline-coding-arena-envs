"""Explicit no-deadline entrypoint for the pinned native benchmark runtime.

This process-local override disables Harbor's agent, step, setup, build and
verifier phase deadlines. It leaves the installed runtime and task bundles
unchanged. Individual transport/health-check fault detection is independent
of these phase deadlines.
"""
from importlib.metadata import version

#: Must equal `harbor.EXPECTED_VERSIONS["harbor"]` (a unit test holds them
#: together). Kept literal: this module runs in the native interpreter, which
#: does not import the pipeline's runner.
PINNED_HARBOR = "0.24.0"


def install():
    if version("harbor") != PINNED_HARBOR:
        raise RuntimeError(f"The unlimited runtime requires Harbor {PINNED_HARBOR}")
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
