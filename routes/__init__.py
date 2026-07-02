"""Route registration package (Stage 2 refactor). See each module's docstring."""
from . import api_core, api_legacy, api_v95_books


def register_all(flask_app, host):
    api_core.register(flask_app, host)
    api_v95_books.register(flask_app, host)
    api_legacy.register(flask_app, host)
