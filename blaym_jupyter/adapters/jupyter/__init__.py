"""The JupyterLab / Jupyter server adapter (S5). The only part of the package that imports
`jupyter_server`, `tornado` and `traitlets`; loaded by the extension hook, never by the core."""

from __future__ import annotations

SETTINGS_KEY = "blaym_host"
KERNELS_KEY = "blaym_kernels"


def install(server_app):
    """Build the host, register the API handler, and arrange the host's shutdown. Returns the host."""
    from blaym_jupyter.routes import setup_route_handlers

    from .config import BlaymConfig
    from .host import BlaymHost
    from .kernels import install_kernel_adapter

    host = BlaymHost(BlaymConfig(parent=server_app), root_dir=server_app.root_dir)
    server_app.web_app.settings[SETTINGS_KEY] = host
    setup_route_handlers(server_app.web_app, host)
    kernels = install_kernel_adapter(server_app, host)
    server_app.web_app.settings[KERNELS_KEY] = kernels

    original = server_app.cleanup_kernels

    async def cleanup_kernels():  # the server stops kernels first, so the recorders see them go
        try:
            await original()
            gather = getattr(server_app.event_logger, "gather_listeners", None)
            if gather is not None:  # the shutdown events of the kernels just stopped
                await gather()
        finally:
            try:
                await kernels.close()  # every kernel is gone: runs are finished before the host closes
            finally:
                host.close()

    server_app.cleanup_kernels = cleanup_kernels
    return host
