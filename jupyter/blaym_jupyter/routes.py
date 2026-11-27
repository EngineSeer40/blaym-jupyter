from jupyter_server.utils import url_path_join

from blaym_notebook.api import BASE_PATH
from blaym_jupyter.adapters.jupyter.handlers import BlaymApiHandler


def setup_route_handlers(web_app, host):
    """Every route of `api/routes.py` goes to one handler under `<base_url>/blaym-jupyter/api/`."""
    base_url = web_app.settings["base_url"]
    pattern = url_path_join(base_url, BASE_PATH, "(.*)")
    web_app.add_handlers(".*$", [(pattern, BlaymApiHandler, {"host": host})])
