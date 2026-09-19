"""The small desks the reconcile tests share: one route for the tests of
the signal, a route and a pinned volume for the tests of what a
reconcile writes."""


def routes_file(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text("[route:x]\nplayback = 1/2\noutput = 1/2\n")
    return path


CONF = """
[route:main]
playback = 1/2
output = 1/2
level = 0.0

[output:1]
volume = -6.0
"""


def config_of(tmp_path, extra=""):
    from oscmix_desk.config import load_config

    path = tmp_path / "routing.conf"
    path.write_text(CONF + extra)
    return load_config(path)
