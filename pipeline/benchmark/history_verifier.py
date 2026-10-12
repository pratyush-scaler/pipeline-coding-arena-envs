"""Pinned Harbor verifier hook: private earlier artifacts, then stock grading.

Harbor is only installed inside the pinned verifier image, and this module is
the entry point Harbor imports there. On a dev box -- and in the module-import
sweep `./arena audit` runs -- `harbor` is absent, so the import is guarded. The
guard must not let a missing Harbor look like a working verifier: `HARBOR_ERROR`
carries the original failure and `HistoryVerifier` refuses to construct, rather
than inheriting from a stub that would silently accept a real trial.
"""
from __future__ import annotations

from pathlib import Path
import tempfile

try:  # pragma: no cover - exercised only where Harbor is installed
    from harbor.models.task.verifier_mode import (
        resolve_step_verifier_mode,
        resolve_task_verifier_mode,
    )
    from harbor.verifier.verifier import Verifier

    HARBOR_ERROR: ImportError | None = None
except ImportError as exc:  # Harbor absent: importable, not runnable.
    HARBOR_ERROR = exc

    def _unavailable(*_args, **_kwargs):
        raise ImportError(
            "Harbor is not installed; HistoryVerifier runs only inside the "
            f"pinned separate-verifier image ({HARBOR_ERROR})"
        ) from HARBOR_ERROR

    resolve_step_verifier_mode = _unavailable  # type: ignore[assignment]
    resolve_task_verifier_mode = _unavailable  # type: ignore[assignment]

    class Verifier:  # type: ignore[no-redef]
        """Placeholder base that refuses to be instantiated."""

        def __init__(self, *_args, **_kwargs):
            _unavailable()

from .step_history import HISTORY_ROOT, HistoryEvidenceError, build_history


class HistoryVerifier(Verifier):
    async def verify(self):
        task = self.task.config
        step = next((item for item in task.steps or [] if item.name == self.step_name), None)
        mode = resolve_step_verifier_mode(task, step) if step is not None else resolve_task_verifier_mode(task)
        if mode.value != "separate" or not self._skip_tests_upload:
            raise HistoryEvidenceError("Prior-step history requires the pinned separate-verifier image")
        with tempfile.TemporaryDirectory(prefix="arena-verifier-history-") as temporary:
            packet = Path(temporary).resolve() / "history"
            build_history(task.model_dump(mode="json"), self.step_name, self.trial_paths.trial_dir, packet)
            # Docker cp may preserve the source root's mode. The outer temporary
            # directory stays private on the host; the verifier-side byte copy
            # must remain readable when task.verifier.user is not root.
            for path in packet.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            packet.chmod(0o755)
            await self.environment.empty_dirs([HISTORY_ROOT], chmod=True)
            await self.environment.upload_dir(source_dir=packet, target_dir=HISTORY_ROOT)
        return await super().verify()
