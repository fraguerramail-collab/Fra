import os

from werkzeug.serving import run_simple

from app.multiapp import MultiProfileDispatcher

instance_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance")
app = MultiProfileDispatcher(instance_path)

if __name__ == "__main__":
    run_simple("0.0.0.0", 5000, app, use_reloader=True, use_debugger=True)
