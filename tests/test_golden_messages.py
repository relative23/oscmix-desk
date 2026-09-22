"""What goes on the wire for a desk of every route shape, written out.

A third outside review found that the oracle the reconciler is held
against (`tests/oracle.py`) built its messages with the reconciler's own
`link_messages` and `mix_messages`: a wrong register there would have
been wrong on both sides of the comparison, and the comparison green.

These are the registers, type tags, values and order, as literals. Where
they come from is not the code: the pair register and its balance are
upstream's folding of a linked pair onto its left channel, the 6.02 dB is
the deficit measured on a UCX II for an unlinked route, `hi-z = on` as
`,i 1` and `Internal` as clock source 0 are what the device reports
(`tests/data/refresh-dump.json`), and the two phases with the links first
are ADR 0001. A change to any of them has to be made here as well, by
hand, which is the point.
"""

import oracle
import pytest
from support import routing_conf

from oscmix_desk import reconcile
from oscmix_desk.config import load_config

DESK = """
[route:main]
playback = 1/2
output = 5/6
level = 0.0
volume = 0.0

[route:second-source]
playback = 3/4
output = 5/6
level = -3.0

[route:split]
playback = 7/8
output = 9/10
stereo = false
level = -6.0

[route:split-unity]
playback = 11/12
output = 13/14
stereo = false
level = 0.0

[route:mono]
playback = 15
output = 16
level = -12.0
volume = -20.0

[route:monitoring]
input = 1/2
output = 1/2
level = -9.0

[input:3]
gain = 12.0
hi-z = on

[output:1]
volume = -10.0

[clock]
source = Internal
"""

#: Every pair is linked before any mix is written, once each: outputs 5/6
#: are fed by two routes and linked once. An unlinked output pair says so
#: (0); its source pair is linked all the same. Mono addresses require
#: unlinked pairs; otherwise the pinned C backend also changes neighbours.
MONO_LINKS = [
    ("/playback/15/stereo", "i", (0,)),
    ("/output/15/stereo", "i", (0,)),
]
LINKS = [
    ("/playback/1/stereo", "i", (1,)),
    ("/output/5/stereo", "i", (1,)),
    ("/playback/3/stereo", "i", (1,)),
    ("/playback/7/stereo", "i", (1,)),
    ("/output/9/stereo", "i", (0,)),
    ("/playback/11/stereo", "i", (1,)),
    ("/output/13/stereo", "i", (0,)),
    *MONO_LINKS,
    ("/input/1/stereo", "i", (1,)),
    ("/output/1/stereo", "i", (1,)),
]

#: A linked pair is one register on its left channel, balance 0. An
#: unlinked pair is two, panned hard, 6.02 dB up because that path halves
#: the gain. A split route's 0 dB ceiling consumes that headroom; boosts
#: above unity are refused during config validation.
MIX = [
    ("/mix/5/playback/1", "fi", (0.0, 0)),
    ("/output/5/volume", "f", (0.0,)),
    ("/output/6/volume", "f", (0.0,)),
    ("/mix/5/playback/3", "fi", (-3.0, 0)),
    ("/mix/9/playback/7", "fi", (0.020599913279624, -100)),
    ("/mix/10/playback/7", "fi", (0.020599913279624, 100)),
    ("/mix/13/playback/11", "fi", (6.020599913279624, -100)),
    ("/mix/14/playback/11", "fi", (6.020599913279624, 100)),
    ("/mix/16/playback/15", "fi", (-12.0, 0)),
    ("/output/16/volume", "f", (-20.0,)),
    ("/mix/1/input/1", "fi", (-9.0, 0)),
]

#: Channel and global state, in the order of the file.
CHANNEL = [
    ("/input/3/gain", "f", (12.0,)),
    ("/input/3/hi-z", "i", (1,)),
    ("/output/1/volume", "f", (-10.0,)),
    ("/clock/source", "i", (0,)),
]


def _same(messages, literals):
    """Paths, tags and order exactly; floats to within rounding."""
    assert [(p, t) for p, t, _a in messages] == [(p, t) for p, t, _a in literals]
    for (path, _t, got), (_p, _tt, want) in zip(messages, literals):
        assert got == pytest.approx(want, abs=1e-12), path
        assert [type(v) for v in got] == [type(v) for v in want], path


@pytest.fixture
def config(tmp_path):
    return load_config(routing_conf(tmp_path, DESK))


def test_the_plan_is_these_registers_in_this_order(config):
    plan = reconcile.plan(reconcile.desired(config))
    _same([w.message() for w in plan.links()], LINKS)
    _same([w.message() for w in plan.mix()], MIX)
    _same([w.message() for w in plan.channel()], CHANNEL)
    _same(plan.messages(), LINKS + MIX + CHANNEL)


def test_the_oracle_is_held_to_the_same_literals(config):
    """It builds its own messages since 0.7.0, and this is what keeps that
    honest: it repeats a shared link where the plan does not, and is
    otherwise these registers too."""
    walked = oracle.routing_plan(config.routes)
    # The historical oracle predates 0.7.1's deliberate mono unlinks.
    # Outputs 5/6 linked a second time, for the second route that feeds them.
    historical_links = [message for message in LINKS if message not in MONO_LINKS]
    _same(walked.links, historical_links[:3] + [LINKS[1]] + historical_links[3:])
    _same(walked.mix, MIX)


def test_the_runtime_s_message_shapes_are_these_too(config):
    for route in config.routes:
        added = MONO_LINKS if route.name == 'mono' else []
        _same(reconcile.link_messages(route) + reconcile.mix_messages(route),
              added + oracle.route_messages(route))
