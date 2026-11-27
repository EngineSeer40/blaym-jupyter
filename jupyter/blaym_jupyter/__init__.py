try:
    from ._version import __version__
except ImportError:
    # Fallback when using the package in dev mode without installing
    # in editable mode with pip. It is highly recommended to install
    # the package from a stable release or in editable mode: https://pip.pypa.io/en/stable/topics/local-project-installs/#editable-installs
    import warnings
    warnings.warn("Importing 'blaym_jupyter' outside a proper installation.")
    __version__ = "dev"


def _jupyter_labextension_paths():
    return [{
        "src": "labextension",
        "dest": "blaym-jupyter"
    }]


def _jupyter_server_extension_points():
    return [{
        "module": "blaym_jupyter"
    }]


def _load_jupyter_server_extension(server_app):
    """Builds the recording host and registers the API handler for the frontend extension.

    Parameters
    ----------
    server_app: jupyterlab.labapp.LabApp
        JupyterLab application instance
    """
    from .adapters.jupyter import install

    install(server_app)
    name = "blaym_jupyter"
    server_app.log.info(f"Registered {name} server extension")
