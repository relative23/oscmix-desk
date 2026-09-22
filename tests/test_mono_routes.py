"""A mono address must not expand to a previously linked neighbour.

Reproduced with the pinned C backend: /mix/6/input/2 also changed
output 5 from input 1 while both pairs were linked. The same folding
occurs for playback. These tests cover the necessary state transition
and reject configurations whose routes cannot share one link state.
"""
import pytest

from oscmix_desk import ConfigError, load_config
from oscmix_desk.model import Config, Route
from oscmix_desk.reconcile import PHASE_LINK, desired, plan
from oscmix_desk.routing import output_link_state


@pytest.mark.parametrize('kind', ['input', 'playback'])
@pytest.mark.parametrize('source', [1, 2])
@pytest.mark.parametrize('output', [5, 6])
def test_mono_plan_unlinks_both_pairs_before_addressing_one_channel(kind, source, output):
    route = Route(name='one', output=(output,), level=-12.0, **{kind: (source,)})
    config = Config(routes=[route])
    seen = {'/%s/1/stereo' % kind: (1,), '/output/5/stereo': (1,)}
    result = plan(desired(config), seen)
    links = {write.path: write.args for write in result.writes if write.phase == PHASE_LINK}
    assert links == {'/%s/1/stereo' % kind: (0,), '/output/5/stereo': (0,)}
    assert output_link_state(config.routes) == {'/output/5/stereo': 0}
    assert [write.path for write in result.mix()] == ['/mix/%d/%s/%d' % (output, kind, source)]


@pytest.mark.parametrize('kind', ['input', 'playback'])
@pytest.mark.parametrize('channel', [1, 2])
@pytest.mark.parametrize('reverse', [False, True])
def test_mono_and_pair_source_requirements_conflict_before_a_config_is_returned(
        tmp_path, kind, channel, reverse):
    sections = [
        '[route:pair]\n%s = 1/2\noutput = 5/6\n' % kind,
        '[route:one]\n%s = %d\noutput = 7\n' % (kind, channel),
    ]
    path = tmp_path / 'routing.conf'
    path.write_text('\n'.join(reversed(sections) if reverse else sections))
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    assert '%s pair 1/2' % kind in message
    assert '[route:one]' in message
    assert '[route:pair]' in message


@pytest.mark.parametrize('output', [5, 6])
@pytest.mark.parametrize('reverse', [False, True])
def test_mono_output_cannot_share_a_linked_destination(tmp_path, output, reverse):
    sections = [
        '[route:pair]\nplayback = 1/2\noutput = 5/6\n',
        '[route:one]\ninput = 3\noutput = %d\n' % output,
    ]
    path = tmp_path / 'routing.conf'
    path.write_text('\n'.join(reversed(sections) if reverse else sections))
    with pytest.raises(ConfigError, match='output pair 5/6'):
        load_config(path)


def test_independent_mono_channels_share_unlinked_flags(tmp_path):
    path = tmp_path / 'routing.conf'
    path.write_text('[route:left]\ninput = 1\noutput = 5\n\n'
                    '[route:right]\ninput = 2\noutput = 6\n')
    result = plan(desired(load_config(path)))
    assert {write.path: write.args for write in result.links()} == {
        '/input/1/stereo': (0,), '/output/5/stereo': (0,),
    }
    assert [write.path for write in result.mix()] == ['/mix/5/input/1', '/mix/6/input/2']


def test_mono_input_and_stereo_playback_can_share_unlinked_outputs(tmp_path):
    path = tmp_path / 'routing.conf'
    path.write_text('[route:pair]\nplayback = 1/2\noutput = 5/6\nstereo = false\n\n'
                    '[route:one]\ninput = 2\noutput = 6\n')
    result = plan(desired(load_config(path)))
    assert {write.path: write.args for write in result.links()} == {
        '/playback/1/stereo': (1,), '/input/1/stereo': (0,), '/output/5/stereo': (0,),
    }
