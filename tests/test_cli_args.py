"""Command-line overrides are bounded like the file they override."""


def test_an_out_of_range_osc_port_on_the_command_line_is_a_config_error(
        session_mod, tmp_path):
    # `[osc] port` refuses 0 and 70000; the override used to accept both
    # and hand them to the backend, whose bind failure was the first sign.
    from oscmix_desk import cli

    path = tmp_path / "routing.conf"
    path.write_text("[route:x]\nplayback = 1/2\noutput = 1/2\n")
    for port in ("70000", "0", "-1"):
        assert cli.main(["--config", str(path), "--osc-port", port]) \
            == session_mod.EXIT_CONFIG, port
