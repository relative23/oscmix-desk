"""Canonical API names retain legacy calls without ambiguous precedence."""

import errno
import os

import pytest
from control_peer import ScriptedControl

from oscmix_desk import (
    BackendControlError,
    CommandLine,
    Control,
    Machine,
    Overrides,
    ReceivePortError,
    effective_config,
    load_config,
    load_profile,
)


@pytest.fixture
def files(tmp_path):
    main = tmp_path / 'routing.conf'
    main.write_text('[device]\nname = Fireface UCX II\n'
                    '[osc]\nport = 8800\n'
                    '[route:main]\nplayback = 1/2\noutput = 25/26\n')
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    (profiles / 'tracking.conf').write_text(
        '[route:tracking]\nplayback = 3/4\noutput = 25/26\n')
    (tmp_path / 'active-profile').write_text('tracking\n')
    return main


@pytest.mark.parametrize('operation', ['config', 'profile', 'effective'])
@pytest.mark.parametrize('style', ['preferred', 'legacy', 'positional'])
def test_override_aliases_select_the_same_validation_model_and_record_the_file(
        files, operation, style):
    assert CommandLine is Overrides
    override = Overrides('Fireface 802', 9000)
    kwargs = {'strict': True}
    positional = []
    if style == 'preferred':
        kwargs['overrides'] = override
    elif style == 'legacy':
        kwargs['said'] = override
    else:
        positional = [override]
    if operation == 'config':
        config = load_config(files, None, *positional, **kwargs)
    elif operation == 'profile':
        config = load_profile('tracking', files, *positional, **kwargs)
    else:
        config, active = effective_config(files, *positional, **kwargs)
        assert active == 'tracking'
    assert config.device_name == 'Fireface 802'
    assert config.osc_port == 9000
    assert config.overrides == override
    assert config.loaded.device_name == 'Fireface UCX II'
    assert config.loaded.osc_port == 8800
    assert config.routes[0].output == (25, 26)


@pytest.mark.parametrize('operation', ['config', 'profile', 'effective', 'machine'])
@pytest.mark.parametrize('same_value', [False, True])
def test_two_override_inputs_are_refused_before_file_lookup(monkeypatch, operation, same_value):
    import oscmix_desk.config as config_module
    import oscmix_desk.profiles as profiles

    def forbidden(*args, **kwargs):
        pytest.fail('invalid Python arguments must be refused before file discovery or reads')

    monkeypatch.setattr(config_module, 'regular_file', forbidden)
    monkeypatch.setattr(profiles, 'discover_config_path', forbidden)
    monkeypatch.setattr(profiles, 'profile_path', forbidden)
    override = Overrides('Fireface 802')
    legacy = override if same_value else CommandLine('Fireface UCX II')
    machine = Machine('Fireface UCX II', '2a39:3fd9', '', 7222, 8222)
    actions = {'config': (load_config, (None,)),
               'profile': (load_profile, ('tracking',)),
               'effective': (effective_config, (None,)),
               'machine': (machine.under, ())}
    action, args = actions[operation]
    with pytest.raises(TypeError, match='not both'):
        action(*args, overrides=override, said=legacy)


def test_machine_accepts_both_names_and_an_explicit_empty_override():
    machine = Machine('Fireface UCX II', '2a39:3fd9', '', 7222, 8222)
    override = Overrides('Fireface 802', 9000)
    assert machine.under(override) == machine.under(said=CommandLine('Fireface 802', 9000))
    assert machine.under().device_name == machine.device_name
    assert load_config(None).overrides == Overrides()
    assert load_config(None, overrides=Overrides()).overrides == CommandLine()


def test_legacy_exception_catches_a_real_closed_control_connection(tmp_path):
    assert ReceivePortError is BackendControlError
    peer = ScriptedControl(tmp_path / 'control.sock')
    connection = None
    try:
        connection = Control(peer.path, os.getpid())
        connection.close()
        with pytest.raises(ReceivePortError) as raised:
            connection.begin()
        assert isinstance(raised.value, BackendControlError)
        assert raised.value.errno == errno.ENOTCONN
    finally:
        if connection is not None:
            connection.close()
        peer.close()
