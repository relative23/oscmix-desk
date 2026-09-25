"""Config values shared by routing tests."""

def make_route(session_mod, **kwargs):
    defaults = dict(name="monitors", playback=(1, 2), output=(5, 6),
                    level=0.0, volume=None, stereo=True)
    defaults.update(kwargs)
    return session_mod.Route(**defaults)

def make_config(session_mod, routes, port, recv_port):
    return session_mod.Config(routes=routes, osc_port=port,
                              osc_recv_port=recv_port)
