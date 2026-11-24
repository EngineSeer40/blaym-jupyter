"""The framework-free service API (S5, decision G).

`handle(request, credential, recording)` maps one HTTP-shaped request to `Service` and
`Registry` calls and answers an `ApiResponse`. It imports the standard library, the contracts,
`service`, `ports` (the blob errors) and `recording`; never a web framework, never a store.
A Jupyter handler (`adapters/jupyter`) and, later, a standalone process both call it.
"""

from .dispatch import ApiRequest, ApiResponse, Recording, handle
from .routes import BASE_PATH, ROUTES, Route

__all__ = ["ApiRequest", "ApiResponse", "BASE_PATH", "ROUTES", "Recording", "Route", "handle"]
