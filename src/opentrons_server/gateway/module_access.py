"""Request-scoped authorization for module placement, including plan steps.

None denotes internal startup/restoration, outside an HTTP request. The API
sets an explicit boolean for EVERY request, propagated to its worker thread.
Never use a global mutable flag: concurrent users can have different roles.
"""
from contextvars import ContextVar


module_admin: ContextVar[bool | None] = ContextVar("module_admin", default=None)


class ModulePlacementForbidden(PermissionError):
    pass


def require_module_admin() -> None:
    if module_admin.get() is False:
        raise ModulePlacementForbidden("Only admins can assign, move, or remove modules. Use the API or chat.")
