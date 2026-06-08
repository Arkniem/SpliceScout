# -*- coding: utf-8 -*-
"""Run the SpliceScout web server from the system tray (no blocking console window).

launch_Win.bat starts this with pythonw (windowless); a tray icon gives Open / Quit. If the tray
libraries (pystray + Pillow) aren't importable, it falls back to the plain console server, so this is
always safe to run. Reuses server.py's instance-tag + free-port machinery (so the tray app still gets
its own cluster JOB_TAG -- the $SPLICESCOUT_INSTANCE name from the launcher, else an auto sraN -- plus
its own port, exactly like `python server.py`).
"""
import os
import threading
import webbrowser


def _start_server():
    """Claim this instance's slot, bind a free port, serve in a daemon thread. Returns (server, httpd, url, tag)."""
    import atexit
    import server
    os.chdir(os.path.dirname(os.path.abspath(server.__file__)))
    server._INSTANCE_TAG, server._INSTANCE_LOCK_PATH = server._claim_instance_slot(server._resolve_instance_name())
    atexit.register(server._release_instance_slot)
    httpd, port = server._bind_server("127.0.0.1", 8765)
    url = "http://127.0.0.1:%d/" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return server, httpd, url, server._INSTANCE_TAG


def _icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (15, 20, 32, 255))
    d = ImageDraw.Draw(img)
    try:
        d.rounded_rectangle([8, 8, 56, 56], radius=12, fill=(91, 140, 255, 255))
    except Exception:
        d.rectangle([8, 8, 56, 56], fill=(91, 140, 255, 255))
    d.text((24, 18), "S", fill=(255, 255, 255, 255))
    return img


def main():
    try:
        import pystray            # noqa: F401
        from PIL import Image     # noqa: F401
    except Exception:
        import server             # no tray libs -> normal console server
        server.main()
        return

    import pystray
    server, httpd, url, tag = _start_server()
    webbrowser.open(url)

    def do_open(icon=None, item=None):
        webbrowser.open(url)

    def do_quit(icon, item):
        try:
            httpd.shutdown()
        except Exception:
            pass
        server._release_instance_slot()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open SpliceScout (%s)" % tag, do_open, default=True),
        pystray.MenuItem("Open in browser", do_open),
        pystray.MenuItem("Quit", do_quit),
    )
    pystray.Icon("SpliceScout", _icon_image(), "SpliceScout %s  %s" % (tag, url), menu).run()


if __name__ == "__main__":
    main()
