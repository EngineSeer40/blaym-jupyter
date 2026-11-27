"""Settings of the Jupyter host (traitlets, `c.BlaymConfig.<name>` in a Jupyter config file)."""

from __future__ import annotations

from pathlib import Path

from blaym_notebook_types import StoreLimits
from jupyter_core.paths import jupyter_data_dir
from traitlets import Dict, Int, Unicode, default
from traitlets.config import Configurable

from blaym_notebook.recording.runs import RunRecorderSettings


class BlaymConfig(Configurable):
    root = Unicode(
        help="The Blaym data folder: history store, blobs and the per-notebook records. "
             "Default: <jupyter data dir>/blaym.",
    ).tag(config=True)

    @default("root")
    def _default_root(self) -> str:
        return str(Path(jupyter_data_dir()) / "blaym")

    limits = Dict(
        help="Store limits, by the contract's names (inlineOutputBytes, truncatedPreviewBytes, "
             "maxStoredOutputBytes, indexedTextBytesPerItem, chatMessageMaxBytes, snapshotInterval, "
             "blobGzipLevel). Unset names keep the defaults.",
    ).tag(config=True)

    run_link_wait_ms = Int(5_000, help="How long a run waits for its code to arrive.").tag(config=True)
    run_link_max_wait_ms = Int(30_000, help="The same wait while a browser is known to be alive.").tag(config=True)
    alive_window_ms = Int(90_000, help="A recorder instance heard within this window is alive.").tag(config=True)
    reply_grace_ms = Int(10_000, help="How long output is accepted after a run's reply.").tag(config=True)
    max_queued_events = Int(1_000, help="Bound on events queued for writing.").tag(config=True)
    max_pending_blob_mb = Int(64, help="Bound on blob bytes queued for writing, in MB.").tag(config=True)
    max_held_messages = Int(1_000, help="Bound on kernel messages held for a run that waits for its code.").tag(config=True)

    def store_limits(self) -> StoreLimits | None:
        """The contract's `StoreLimits` (raises `ValidationError` for an unknown or bad name)."""
        return StoreLimits.model_validate(dict(self.limits)) if self.limits else None

    def run_settings(self) -> RunRecorderSettings:
        return RunRecorderSettings(
            run_link_wait_ms=self.run_link_wait_ms,
            run_link_max_wait_ms=self.run_link_max_wait_ms,
            reply_grace_ms=self.reply_grace_ms,
            max_held_messages=self.max_held_messages,
            max_held_bytes=self.max_pending_blob_mb * 1024 * 1024,
        )
