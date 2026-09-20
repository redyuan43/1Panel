"""Optional V100 observer; default Router builds have no extra dependency."""
import os

if os.environ.get("V100_OBSERVER") == "1":
    from v100_observer.router import ObserverMiddleware, observe_dispatch, observe_response

    def install_observer(app):
        app.add_middleware(ObserverMiddleware)
else:
    def observe_dispatch(*args, **kwargs):
        return None

    def observe_response(*args, **kwargs):
        return None

    def install_observer(app):
        return None
