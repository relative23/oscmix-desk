"""The route-by-route walk the reconciler replaced, kept as a test oracle.

``routing_plan`` is what the apply sent before 0.4.0: every link of
every route, then every mix write. ``reconcile.plan`` is held to produce
the same sequence minus repeats (tests/test_reconcile.py), and
``route_messages`` is the per-route shape the contract tests and the
verifier tests reason about. Nothing in the runtime calls either since
0.6.2 -- they lived in ``routing.py`` as public functions with no
caller, which is a public surface pretending to be one -- so they live
here, next to the tests that are their only reason to exist.
"""

from typing import List, NamedTuple, Sequence, Tuple

from oscmix_desk.config import Route
from oscmix_desk.reconcile import link_messages, mix_messages

Message = Tuple[str, str, Tuple[object, ...]]


def route_messages(route: Route) -> List[Message]:
    """Every OSC message a route declares, in dependency order."""
    return link_messages(route) + mix_messages(route)


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
        plan.links.extend(link_messages(route))
    for route in routes:
        plan.mix.extend(mix_messages(route))
    return plan
