"""Synthetic explicit unconfirmed-tail schemas; no actual hardware acceptance."""

import copy
import hashlib

import pytest
from playback_mute_evidence import (
    evidence_digest,
    tail_usb_payload,
    validate_autonomous_eof_tail,
    validate_outgoing,
    validate_outgoing_completion_scope,
)


def example(kinds=("meter", "statusserial")):
    wall = 1_000_000_000_000
    at = wall + 6_000_000_000
    packet = dict(
        index=2, record_offset=200, timestamp_ns=at, id=101, kind="S", transfer=3,
        endpoint=7, device=2, bus=5, flag_setup=45, flag_data=0,
        usb_timestamp_ns=at, status=-115, extra_header_hex="00" * 24, is_midi=True,
    )
    messages, events, refs, payload = [], [], [], bytearray()
    for index, kind in enumerate(kinds, 1):
        words = []
        if kind == "meter":
            data, sub = bytes.fromhex("f000200d1002f7"), 2
        else:
            word = 0x3F000006
            word |= 0 if bin(word).count("1") % 2 else 0x80000000
            data = bytes.fromhex("f000200d1000") + bytes(
                (word >> shift) & 127 for shift in (0, 7, 14, 21, 28)
            ) + b"\xf7"
            sub = 0
            words.append(dict(
                message_index=index, word_index=0, packet_index=2, timestamp_ns=at,
                direction="out", cable=1, endpoint=7, urb_id=101, word=word,
                reg=0x3F00, val=6, parity=True, kind="register", decoded_value=6,
            ))
            events.extend(words)
        message = dict(
            direction="out", cable=1, start_packet=2, end_packet=2,
            timestamp_ns=at, sub=sub, kind="rme", hex=data.hex(),
        )
        messages.append(message)
        refs.append(dict(
            message_index=index, kind=kind, message=message,
            message_sha256=evidence_digest(message),
            original_sysex_sha256=hashlib.sha256(data).hexdigest(), words=words,
        ))
        payload.extend(tail_usb_payload(data, 1))
    packet.update(length=len(payload), len_cap=len(payload), payload_hex=payload.hex())
    stages = [dict(
        label=label, started_at=3.0 + n / 10, finished_at=3.01 + n / 10,
        elapsed=0.01, passed=True,
    ) for n, label in enumerate((
        "live:interface", "live:PCM", "live:streams", "live:backend-status",
        "live:process", "live:USB",
    ))]
    pairs = [dict(submit=0, complete=1, urb=100)]
    tail = dict(
        schema="oscmix-desk.unconfirmed-autonomous-eof-tail.v1",
        completion_confirmed=False, transfer_success_claim=False,
        coefficient_or_control_effect_claim=False,
        packet=packet, packet_sha256=evidence_digest(packet),
        original_pcap_sha256="0" * 64, original_pcap_bytes=280 + len(payload),
        record_ends_exactly_at_original_eof=True, cable=1, messages=refs,
        full_prior_out_completion_pairs_sha256=evidence_digest(pairs),
        full_prior_out_completion_pair_count=1, full_runtime_tail_sha256="1" * 64,
        own_seal=dict(
            end_ack_monotonic=1.0, final_refresh_request=3,
            final_refresh_ack_wall_range_ns=[wall + 2_099_000_000, wall + 2_201_000_100],
            final_device_finish_wall_range_ns=[wall + 3_999_000_000, wall + 4_001_000_100],
            final_live_guard_finish_wall_range_ns=[wall + 3_509_000_000, wall + 3_511_000_100],
            final_live_guard_rows_sha256=evidence_digest(stages),
        ),
    )
    scope = dict(
        schema="oscmix-desk.outgoing-completion-scope.v2",
        transfer_scope="bound-midi-out-endpoint", confirmed_bound_out_urbs=1,
        confirmed_bound_out_pairs_sha256=evidence_digest(pairs),
        controlled_transfers_complete=True, native_callback_transfers_complete=True,
        refresh_transfers_complete=True, all_outgoing_transfers_completed=False,
        completed_autonomous_messages=1, unconfirmed_autonomous_messages=len(kinds),
    )
    counts = dict(autonomous_statusserial=1, autonomous_meter_requests=0,
                  own_final_refresh=1, unchanged_native_zero_corrections=0)
    value = dict(
        message_counts=counts, fragment_completion_counts=dict(counts),
        assessed_groups_sha256=dict.fromkeys(counts, "0" * 64),
        status_register=0x3F00, status_frame_bytes=12, status_serial_cycle=16,
        status_serial_initial=5, status_serial_final=5, meter_request_hex="f000200d1002f7",
        refresh_register=0x3E04, refresh_value=0x67CD, native_correction_words=2,
        native_correction_values=[0, 0],
        native_correction_phase="own-final-REFRESH-after-END-watch",
        outgoing_register_words=122 + kinds.count("statusserial"),
        sealed_playback_coefficient_words=120,
        assessed_outgoing_messages=5 + len(kinds), all_outgoing_messages_assessed=True,
        coefficient_device_readback=False, source_proof_sha256="0" * 64,
        outgoing_completion_scope=scope, unconfirmed_autonomous_eof_tail=tail,
    )
    return value, stages, pairs


@pytest.mark.parametrize("kinds", [("meter",), ("statusserial",), ("meter", "statusserial")])
def test_only_explicit_unconfirmed_final_autonomous_shapes_are_portable(kinds):
    value, _, _ = example(kinds)
    validate_outgoing(value)
    assert value["outgoing_completion_scope"]["all_outgoing_transfers_completed"] is False
    assert value["unconfirmed_autonomous_eof_tail"]["transfer_success_claim"] is False


@pytest.mark.parametrize("fault", [
    "schema", "confirmed", "transfer-success", "effect", "pcap-sha", "pcap-size",
    "eof-claim", "packet-index-bool", "packet-id-bool", "packet-direction", "packet-status",
    "packet-bus", "packet-endpoint", "packet-identity-sha", "packet-boundary", "packet-len",
    "packet-header", "packet-clock", "extra-midi", "cable-bool", "message-sha", "sysex-sha",
    "message-direction", "message-sub", "message-cable", "message-packet", "message-time",
    "word-parity", "word-reg", "word-val", "word-urb", "word-ref", "word-kind",
    "word-decoded", "reverse-order", "duplicate-meter", "missing-message", "message-index",
    "request-bool", "end-inf", "guard-sha", "seal-at-tail", "reverse-seal-window",
    "pairs-count-bool", "pairs-sha", "runtime-sha", "extra-field",
])
def test_unconfirmed_tail_never_weakens_original_bytes_seal_or_transfer_claims(fault):
    value, _, _ = example()
    tail = value["unconfirmed_autonomous_eof_tail"]
    packet, message = tail["packet"], tail["messages"][-1]["message"]
    event = tail["messages"][-1]["words"][0]
    if fault == "schema":
        tail["schema"] = "legacy"
    elif fault in ("confirmed", "transfer-success", "effect"):
        tail[{"confirmed": "completion_confirmed", "transfer-success": "transfer_success_claim",
              "effect": "coefficient_or_control_effect_claim"}[fault]] = True
    elif fault == "pcap-sha":
        tail["original_pcap_sha256"] = "bad"
    elif fault == "pcap-size":
        tail["original_pcap_bytes"] += 1
    elif fault == "eof-claim":
        tail["record_ends_exactly_at_original_eof"] = False
    elif fault.startswith("packet-"):
        key, replacement = {
            "packet-index-bool": ("index", True), "packet-id-bool": ("id", True),
            "packet-direction": ("kind", "C"), "packet-status": ("status", -1),
            "packet-bus": ("bus", 6), "packet-endpoint": ("endpoint", 129),
            "packet-boundary": ("record_offset", 199), "packet-len": ("len_cap", 1),
            "packet-header": ("extra_header_hex", "00"), "packet-clock": ("usb_timestamp_ns", 0),
            "packet-identity-sha": ("id", 999),
        }[fault]
        packet[key] = replacement
        if fault != "packet-identity-sha":
            tail["packet_sha256"] = evidence_digest(packet)
    elif fault == "extra-midi":
        packet["payload_hex"] += "00000000"
        packet["len_cap"] += 4
        packet["length"] += 4
        tail["original_pcap_bytes"] += 4
        tail["packet_sha256"] = evidence_digest(packet)
    elif fault == "cable-bool":
        tail["cable"] = True
    elif fault in ("message-sha", "sysex-sha"):
        key = "message_sha256" if fault == "message-sha" else "original_sysex_sha256"
        tail["messages"][-1][key] = "0" * 64
    elif fault.startswith("message-") and fault != "message-index":
        key, replacement = {
            "message-direction": ("direction", "in"), "message-sub": ("sub", 2),
            "message-cable": ("cable", 2), "message-packet": ("end_packet", 1),
            "message-time": ("timestamp_ns", 1),
        }[fault]
        message[key] = replacement
        tail["messages"][-1]["message_sha256"] = evidence_digest(message)
    elif fault.startswith("word-"):
        key, replacement = {
            "word-parity": ("word", event["word"] ^ (1 << 31)), "word-reg": ("reg", 0x3E04),
            "word-val": ("val", 7), "word-urb": ("urb_id", 99), "word-ref": ("message_index", 99),
            "word-kind": ("kind", "coefficient"), "word-decoded": ("decoded_value", 7),
        }[fault]
        event[key] = replacement
    elif fault == "reverse-order":
        tail["messages"].reverse()
    elif fault == "duplicate-meter":
        tail["messages"][1] = copy.deepcopy(tail["messages"][0])
    elif fault == "missing-message":
        tail["messages"].pop()
    elif fault == "message-index":
        tail["messages"][0]["message_index"] += 9
    elif fault == "request-bool":
        tail["own_seal"]["final_refresh_request"] = True
    elif fault == "end-inf":
        tail["own_seal"]["end_ack_monotonic"] = float("inf")
    elif fault == "guard-sha":
        tail["own_seal"]["final_live_guard_rows_sha256"] = "bad"
    elif fault == "seal-at-tail":
        tail["own_seal"]["final_device_finish_wall_range_ns"][1] = packet["timestamp_ns"]
    elif fault == "reverse-seal-window":
        tail["own_seal"]["final_refresh_ack_wall_range_ns"].reverse()
    elif fault in ("pairs-count-bool", "pairs-sha", "runtime-sha"):
        key, replacement = {
            "pairs-count-bool": ("full_prior_out_completion_pair_count", True),
            "pairs-sha": ("full_prior_out_completion_pairs_sha256", "bad"),
            "runtime-sha": ("full_runtime_tail_sha256", "bad"),
        }[fault]
        tail[key] = replacement
    else:
        tail["other"] = False
    with pytest.raises(ValueError, match=r"tail|autonomous|unconfirmed|SHA256|scope"):
        validate_autonomous_eof_tail(tail, 5)


@pytest.mark.parametrize(("key", "replacement"), [
    ("schema", "legacy"), ("transfer_scope", "all-usb"), ("confirmed_bound_out_urbs", True),
    ("confirmed_bound_out_pairs_sha256", "bad"), ("controlled_transfers_complete", False),
    ("native_callback_transfers_complete", False), ("refresh_transfers_complete", False),
    ("all_outgoing_transfers_completed", True), ("completed_autonomous_messages", 3),
    ("unconfirmed_autonomous_messages", 0),
])
def test_tail_scope_cannot_alias_completed_groups_or_omit_tail_counts(key, replacement):
    value, _, _ = example()
    value["outgoing_completion_scope"][key] = replacement
    with pytest.raises(ValueError, match=r"tail|autonomous|unconfirmed|SHA256|scope"):
        validate_outgoing(value)


def test_completed_groups_keep_old_counts_and_explicit_scope_is_optional_without_tail():
    value, _, _ = example()
    value.pop("outgoing_completion_scope")
    value.pop("unconfirmed_autonomous_eof_tail")
    value["assessed_outgoing_messages"] -= 2
    value["outgoing_register_words"] -= 1
    validate_outgoing(value)
    value["unconfirmed_autonomous_eof_tail"] = example()[0]["unconfirmed_autonomous_eof_tail"]
    with pytest.raises(ValueError, match="versioned"):
        validate_outgoing_completion_scope(value)


@pytest.mark.parametrize("field", ["assessed_outgoing_messages", "outgoing_register_words"])
def test_tail_is_counted_exactly_once_without_becoming_completed(field):
    value, _, _ = example()
    value[field] -= 1
    with pytest.raises(ValueError, match="OUT assessment"):
        validate_outgoing(value)
