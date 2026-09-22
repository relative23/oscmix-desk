"""The route-by-route walk the reconciler replaced, kept as a test oracle.

``routing_plan`` is what the apply sent before 0.4.0: every link of
every route, then every mix write. ``reconcile.plan`` is held to produce
the same sequence minus repeats (tests/test_reconcile.py), and
``route_messages`` is the per-route shape the contract tests and the
verifier tests reason about. Nothing in the runtime calls either since
0.6.2 -- they lived in ``routing.py`` as public functions with no
caller, which is a public surface pretending to be one -- so they live
here, next to the tests that are their only reason to exist.

**It builds its own messages.** Until 0.7.0 it imported
``link_messages`` and ``mix_messages`` from the reconciler it is the
oracle for, so a wrong register there was wrong on both sides of every
comparison (third outside review). What is below is written from what
the device and upstream do, not from that code, and
``tests/test_golden_messages.py`` holds both to the same literals.
"""

from typing import List, NamedTuple, Sequence, Tuple

from oscmix_desk.model import Route

Message = Tuple[str, str, Tuple[object, ...]]

#: An unlinked pair is fed through the pair balance, which halves the
#: gain: 20 * log10(2), measured on a UCX II as an exact 6 dB deficit.
HALVED = 6.020599913279624


def _links(route: Route) -> List[Message]:
    """The source pair is linked; the output pair says which it is."""
    if len(route.output) == 1:
        return []
    kind, source = route.source
    return [("/%s/%d/stereo" % (kind, source[0]), "i", (1,)),
            ("/output/%d/stereo" % route.output[0], "i",
             (int(bool(route.stereo)),))]


def _mix(route: Route) -> List[Message]:
    """One pair register for a linked pair, two hard-panned ones for an
    unlinked pair, one plain one for a mono route; then the volumes."""
    kind, source = route.source
    first = source[0]
    if len(route.output) == 1 or route.stereo:
        written = [(route.output[0], route.level, 0)]
    else:
        # Mute is the backend's explicit zero, not another gain to offset.
        level = (-65.0 if route.level <= -65.0
                 else min(route.level, 0.0) + HALVED)
        written = [(route.output[0], level, -100), (route.output[1], level, 100)]
    messages: List[Message] = [
        ("/mix/%d/%s/%d" % (out, kind, first), "fi", (level, pan))
        for out, level, pan in written]
    if route.volume is not None:
        messages += [("/output/%d/volume" % out, "f", (route.volume,))
                     for out in route.output]
    return messages


def route_messages(route: Route) -> List[Message]:
    """Every OSC message a route declares, in dependency order."""
    return _links(route) + _mix(route)


class RoutingPlan(NamedTuple):
    """The datagrams a routing consists of, split at the link barrier."""

    links: List[Message]
    mix: List[Message]

    def messages(self) -> List[Message]:
        return [*self.links, *self.mix]


def routing_plan(routes: Sequence[Route]) -> RoutingPlan:
    """All links of all routes, then all mix writes: the pre-0.4.0 order."""
    plan = RoutingPlan([], [])
    for route in routes:
        plan.links.extend(_links(route))
    for route in routes:
        plan.mix.extend(_mix(route))
    return plan
