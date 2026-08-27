from .routes import album_bp

# Vue/JSON clients use the same blueprint and authorization primitives as the
# existing server-rendered album UI. Import after routes so all shared helpers
# are initialized before API decorators are registered.
from . import api as _api  # noqa: F401,E402
