import os, sys, socket, threading, time, webbrowser
from contextlib import closing
from waitress import serve

# Set flags BEFORE importing app
os.environ.setdefault("FLASK_ENV", "production")
os.environ.setdefault("BG_DESKTOP", "1")

from app import app, APP_PORT, APP_NAME

# Bind to loopback by default so a desktop install isn't reachable from
# the rest of the LAN. Override with BG_BIND_HOST=0.0.0.0 if you really
# want to expose the local server (and read SECURITY.md first).
HOST = os.getenv("BG_BIND_HOST", "127.0.0.1")
PORT = APP_PORT


def _port_available(host: str, port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def run_server(port: int):
    serve(app, host=HOST, port=port, threads=8)


def start_desktop_webview(webview_module):
    start_kwargs = {}
    if sys.platform.startswith("win"):
        start_kwargs["gui"] = "edgechromium"

    try:
        webview_module.start(**start_kwargs)
    except TypeError:
        webview_module.start()
    except Exception as exc:
        if start_kwargs:
            print(f"Preferred webview renderer unavailable ({exc}). Falling back to default renderer.")
            webview_module.start()
        else:
            raise


def main():
    port = PORT
    if not _port_available(HOST, port):
        print(f"Port {port} is already in use. Close the other {APP_NAME} instance and try again.")
        return 1
    t = threading.Thread(target=run_server, args=(port,), daemon=True)
    t.start()

    url = f"http://127.0.0.1:{port}/"

    try:
        import webview
        webview.create_window(
            title=APP_NAME,
            url=url,
            width=1200, height=840,
            min_size=(1024, 720),
            confirm_close=True,
        )
        start_desktop_webview(webview)
    except ImportError:
        webbrowser.open(url)
        try:
            while t.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
