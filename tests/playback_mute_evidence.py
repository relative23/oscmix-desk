"""Validate retained Playback-Mute evidence without private files or live I/O.

Ten linked-pair probes address twenty mute paths directly. Only each odd
channel receives ON; each even channel receives its own idempotent OFF write.
Cache and completed host USB transfers provide no physical mute readback.
"""

import hashlib
import json
import math
import re
import struct

PAIRS = tuple(range(1, 21, 2))
INSTRUMENT_FILES = frozenset(
    (
        "scope.py",
        "control.py",
        "pilot.py",
        "bindings.py",
        "adapters.py",
        "run.py",
        "review.py",
        "test_pilot.py",
        "inputs.json",
    )
)
NO_CLAIMS = (
    "device_mute_readback",
    "coefficient_device_readback",
    "acoustic_safety_claim",
    "atomic_cache_snapshot_claim",
    "cold_start_qualified",
)

USB_SCOPE = dict(
    all_raw_registers_stable_claim=False,
    capture_boundary_completeness_claim=False,
    capture_boundary_feedback_claim=False,
    complete_messages_only_used_as_register_evidence=True,
)

WATCH_VOLATILE_PATHS = frozenset(
    "/input/%d/%s/meter" % (channel, family)
    for channel in (1, 2)
    for family in ("dynamics", "autolevel")
)
OUTGOING_GROUPS = frozenset(
    (
        "autonomous_statusserial",
        "autonomous_meter_requests",
        "own_final_refresh",
        "unchanged_native_zero_corrections",
    )
)


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(value):
    need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "invalid SHA256")
    return value


def matrix_sha(matrix):
    need(len(matrix) == 20 and all(len(row) == 40 for row in matrix), "full800 required")
    words = [word for row in matrix for word in row]
    need(
        all(isinstance(word, str) and re.fullmatch(r"[0-9a-f]{8}", word) for word in words),
        "invalid Float32 bit representation",
    )
    data = bytes.fromhex("".join(words))
    need(
        all(
            math.isfinite(value) and 0 <= value <= 2 for (value,) in struct.iter_unpack("<f", data)
        ),
        "invalid own coefficient",
    )
    return hashlib.sha256(data).hexdigest()


def validate_boundaries(boundaries, packet_count):
    """Keep EOF fragments opaque; validate only retained framing and commitments."""
    need(
        type(boundaries) is list and type(packet_count) is int and packet_count > 0,
        "USB capture boundary container differs",
    )
    keys = {
        "scope",
        "direction",
        "cable",
        "endpoint",
        "start_packet",
        "last_packet",
        "start_timestamp_ns",
        "last_timestamp_ns",
        "fragment_locations",
        "bytes",
        "hex",
        "sha256",
        "assessment",
        "complete_message",
        "complete_messages_generated",
        "register_events_generated",
        "register_feedback_claim",
        "write_acceptance_claim",
    }
    for boundary in boundaries:
        need(
            set(boundary) == keys
            and boundary["scope"] == "opaque-unassessed-incoming-EOF-fragment"
            and boundary["assessment"] == "opaque unassessed"
            and boundary["direction"] == "in"
            and type(boundary["cable"]) is int
            and boundary["cable"] == 1
            and type(boundary["endpoint"]) is int
            and 128 <= boundary["endpoint"] <= 255,
            "boundary is not qualified incoming EOF",
        )
        need(
            all(
                boundary[key] is False
                for key in (
                    "complete_message",
                    "register_feedback_claim",
                    "write_acceptance_claim",
                )
            )
            and all(
                type(boundary[key]) is int and boundary[key] == 0
                for key in ("complete_messages_generated", "register_events_generated")
            ),
            "opaque fragment promoted to message/feedback",
        )
        count, raw_hex = boundary["bytes"], boundary["hex"]
        need(
            type(count) is int
            and 0 < count <= 64 * 1024 * 1024
            and isinstance(raw_hex, str)
            and re.fullmatch(r"[0-9a-f]+", raw_hex)
            and len(raw_hex) == 2 * count,
            "boundary raw byte count differs",
        )
        need(
            hashlib.sha256(bytes.fromhex(raw_hex)).hexdigest() == sha(boundary["sha256"]),
            "boundary byte SHA differs",
        )
        start, last = boundary["start_packet"], boundary["last_packet"]
        need(
            type(start) is int and type(last) is int and 0 <= start <= last < packet_count,
            "boundary packet positions differ",
        )
        first_time, last_time = boundary["start_timestamp_ns"], boundary["last_timestamp_ns"]
        need(
            type(first_time) is int and type(last_time) is int and 0 <= first_time <= last_time,
            "boundary timestamp positions differ",
        )
        locations = boundary["fragment_locations"]
        need(type(locations) is list and locations, "boundary fragment positions absent")
        pairs = []
        for location in locations:
            need(
                set(location) == {"packet_index", "event_offset"}
                and type(location["packet_index"]) is int
                and start <= location["packet_index"] <= last
                and type(location["event_offset"]) is int
                and location["event_offset"] >= 0
                and location["event_offset"] % 4 == 0,
                "boundary fragment location differs",
            )
            pairs.append((location["packet_index"], location["event_offset"]))
        need(
            pairs == sorted(set(pairs)) and pairs[0][0] == start and pairs[-1][0] == last,
            "boundary location ordering differs",
        )


def validate_watch(case):
    counts = case["watch_volatile_device_paths"]
    need(
        type(case["watch_static_device_paths"]) is int
        and case["watch_static_device_paths"] == 0
        and case["watch_inventory_qualified"] is True
        and isinstance(counts, dict)
        and set(counts) == WATCH_VOLATILE_PATHS
        and all(type(count) is int and count >= 0 for count in counts.values())
        and type(case["watch_volatile_device_messages"]) is int
        and case["watch_volatile_device_messages"] == sum(counts.values())
        and type(case["watch_raw_control_frames"]) is int
        and case["watch_raw_control_frames"] >= 0
        and ((sum(counts.values()) == 0) or case["watch_raw_control_frames"] > 0),
        "static/volatile DEVICE watch scope differs",
    )
    sha(case["watch_raw_frames_sha256"])


def validate_native_refresh_proof(assessment):
    """Every extra native frame needs a complete200-cell causal summary."""
    count = assessment["message_counts"]["unchanged_native_zero_corrections"]
    need(type(count) is int and count >= 0, "native frame count must be a plain integer")
    proof = assessment.get("native_refresh_proof")
    if proof is None:
        need(count in (0, 400), "extra native frames lack complete causal proof")
        return
    need(
        type(proof) is dict
        and set(proof) == {
            "schema", "coordinate_convention", "source_scope", "cable", "window_ns",
            "cell_count", "db_events", "pan_events", "pan_repeats", "cause_events",
            "matched_out_frames", "cells", "cells_sha256", "full_out_frames_sha256",
            "full_1to1_refs_sha256",
        }
        and proof["schema"] == "oscmix-desk.native-zero-refresh-summary.v1"
        and proof["coordinate_convention"]
        == "zero-based-output-even-0..18/input-source-0..19"
        and proof["source_scope"] == "native-input-only"
        and type(proof["cable"]) is int and 0 <= proof["cable"] <= 15,
        "native causal summary schema/coordinate scope differs",
    )
    window = proof["window_ns"]
    need(
        type(window) is list and len(window) == 2
        and all(type(value) is int and 0 <= value <= 2**63 - 1 for value in window)
        and window[0] <= window[1],
        "native final REFRESH window differs",
    )
    cells = proof["cells"]
    need(type(cells) is list and len(cells) == 200, "native exact200 cells required")
    expected = [(output, source) for output in range(0, 20, 2) for source in range(20)]
    for coordinates, cell in zip(expected, cells):
        need(
            type(cell) is dict and set(cell)
            == {"output", "source", "db_events", "pan_events", "pan_repeats", "pan_word"}
            and all(type(cell[key]) is int for key in cell)
            and (cell["output"], cell["source"]) == coordinates
            and cell["db_events"] == 1
            and 1 <= cell["pan_events"] <= 99999
            and cell["pan_repeats"] == cell["pan_events"] - 1,
            "native cell geometry/DB/PAN repeat counts differ",
        )
        value = cell["pan_word"]
        reg = 0x2000 | cell["output"] << 6 | cell["source"]
        pan = value & 0xFFFF
        need(
            0 <= value <= 0xFFFFFFFF and bin(value).count("1") % 2 == 1
            and (value >> 16) & 0x7FFF == reg and pan & 0x8000
            and -100 <= ((pan & 0x7FFF) ^ 0x4000) - 0x4000 <= 100,
            "native identical PAN raw word/parity differs",
        )
    repeats = sum(cell["pan_repeats"] for cell in cells)
    for key, expected_value in (
        ("cell_count", 200), ("db_events", 200), ("pan_events", 200 + repeats),
        ("pan_repeats", repeats), ("cause_events", 400 + repeats),
        ("matched_out_frames", 400 + repeats),
    ):
        need(
            type(proof[key]) is int and proof[key] == expected_value,
            "native total causal count differs: " + key,
        )
    need(
        count == proof["matched_out_frames"] <= 100000
        and proof["full_out_frames_sha256"]
        == assessment["assessed_groups_sha256"]["unchanged_native_zero_corrections"],
        "native OUT count/full reference commitment differs",
    )
    for key in ("cells_sha256", "full_out_frames_sha256", "full_1to1_refs_sha256"):
        sha(proof[key])
    need(
        hashlib.sha256(json.dumps(
            cells, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest() == proof["cells_sha256"],
        "native compact cell commitment differs",
    )


def evidence_digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def tail_hex(value):
    need(type(value) is str and re.fullmatch(r"(?:[0-9a-f]{2})*", value) is not None,
         "tail canonical byte encoding differs")
    return bytes.fromhex(value)


def tail_usb_payload(message, cable):
    result = bytearray()
    for offset in range(0, len(message), 3):
        body = message[offset:offset + 3]
        cin = 4 if offset + 3 < len(message) else 4 + len(body)
        result.extend(bytes([cable << 4 | cin]) + body.ljust(3, b"\0"))
    return bytes(result)


def validate_autonomous_eof_tail(tail, completed_status_final):
    """Validate an explicit unconfirmed last autonomous submit, never a success claim."""
    need(type(tail) is dict and set(tail) == {
        "schema", "completion_confirmed", "transfer_success_claim",
        "coefficient_or_control_effect_claim", "packet", "packet_sha256",
        "original_pcap_sha256", "original_pcap_bytes", "record_ends_exactly_at_original_eof",
        "cable", "messages", "full_prior_out_completion_pairs_sha256", "own_seal",
        "full_prior_out_completion_pair_count", "full_runtime_tail_sha256",
    } and tail["schema"] == "oscmix-desk.unconfirmed-autonomous-eof-tail.v1"
         and tail["completion_confirmed"] is False
         and tail["transfer_success_claim"] is False
         and tail["coefficient_or_control_effect_claim"] is False
         and tail["record_ends_exactly_at_original_eof"] is True,
         "unconfirmed autonomous EOF tail scope differs")
    for key in ("packet_sha256", "original_pcap_sha256",
                "full_prior_out_completion_pairs_sha256", "full_runtime_tail_sha256"):
        sha(tail[key])
    need(type(tail["full_prior_out_completion_pair_count"]) is int
         and tail["full_prior_out_completion_pair_count"] >= 0
         and type(tail["original_pcap_bytes"]) is int
         and 24 <= tail["original_pcap_bytes"] <= 64 * 1024 * 1024
         and type(tail["cable"]) is int and 0 <= tail["cable"] <= 15,
         "tail full PCAP/prior completion inventory differs")
    packet = tail["packet"]
    need(type(packet) is dict and set(packet) == {
        "index", "record_offset", "timestamp_ns", "id", "kind", "transfer", "endpoint",
        "device", "bus", "flag_setup", "flag_data", "usb_timestamp_ns", "status",
        "length", "len_cap", "extra_header_hex", "payload_hex", "is_midi",
    } and all(type(packet[key]) is int for key in (
        "index", "record_offset", "timestamp_ns", "id", "transfer", "endpoint", "device",
        "bus", "flag_setup", "flag_data", "usb_timestamp_ns", "status", "length", "len_cap",
    )) and packet["kind"] == "S" and packet["is_midi"] is True
         and packet["index"] >= 0 and packet["record_offset"] >= 24
         and 0 <= packet["id"] < 2**64 and packet["transfer"] == 3
         and 1 <= packet["endpoint"] <= 15 and packet["bus"] == 5
         and 1 <= packet["device"] <= 127 and packet["status"] in (0, -115)
         and packet["flag_setup"] in (0, 45) and packet["flag_data"] == 0
         and packet["timestamp_ns"] > 0
         and abs(packet["usb_timestamp_ns"] - packet["timestamp_ns"]) < 1000
         and evidence_digest(packet) == tail["packet_sha256"],
         "tail original bound OUT submit differs")
    extra, payload = tail_hex(packet["extra_header_hex"]), tail_hex(packet["payload_hex"])
    need(len(extra) in (8, 24) and packet["length"] == packet["len_cap"] == len(payload)
         and packet["record_offset"] + 16 + 40 + len(extra) + len(payload)
         == tail["original_pcap_bytes"], "tail final original PCAP boundary differs")
    messages = tail["messages"]
    need(type(messages) is list and len(messages) in (1, 2),
         "tail autonomous message cardinality differs")
    kinds, indices, encoded, register_words = [], [], bytearray(), 0
    for item in messages:
        need(type(item) is dict and set(item) == {
            "message_index", "kind", "message", "message_sha256", "original_sysex_sha256", "words",
        } and type(item["message_index"]) is int and item["message_index"] >= 0,
             "tail original message reference differs")
        message, words = item["message"], item["words"]
        need(type(message) is dict and set(message) == {
            "direction", "cable", "start_packet", "end_packet", "timestamp_ns",
            "sub", "kind", "hex",
        } and message["direction"] == "out" and message["kind"] == "rme"
             and type(message["cable"]) is int and message["cable"] == tail["cable"]
             and all(type(message[key]) is int for key in (
                 "start_packet", "end_packet", "timestamp_ns", "sub",
             )) and message["start_packet"] == message["end_packet"] == packet["index"]
             and message["timestamp_ns"] == packet["timestamp_ns"]
             and evidence_digest(message) == item["message_sha256"] and type(words) is list,
             "tail complete original SysEx reference differs")
        data = tail_hex(message["hex"])
        need(hashlib.sha256(data).hexdigest() == sha(item["original_sysex_sha256"]),
             "tail original SysEx commitment differs")
        if item["kind"] == "meter":
            need(message["sub"] == 2 and data == bytes.fromhex("f000200d1002f7") and not words,
                 "unconfirmed meter contains register or other bytes")
        else:
            need(item["kind"] == "statusserial" and message["sub"] == 0 and len(words) == 1,
                 "tail may contain only autonomous meter/statusserial")
            event = words[0]
            need(type(event) is dict and set(event) == {
                "message_index", "word_index", "packet_index", "timestamp_ns",
                "direction", "cable",
                "endpoint", "urb_id", "word", "reg", "val", "parity", "kind", "decoded_value",
            } and all(type(event[key]) is int for key in (
                "message_index", "word_index", "packet_index", "timestamp_ns", "cable", "endpoint",
                "urb_id", "word", "reg", "val", "decoded_value",
            )) and event["message_index"] == item["message_index"] and event["word_index"] == 0
                 and event["packet_index"] == packet["index"]
                 and event["timestamp_ns"] == packet["timestamp_ns"]
                 and event["direction"] == "out" and event["cable"] == tail["cable"]
                 and event["endpoint"] == packet["endpoint"] and event["urb_id"] == packet["id"]
                 and event["reg"] == 0x3F00 and event["kind"] == "register"
                 and event["parity"] is True and 0 <= event["val"] < 16
                 and event["decoded_value"] == event["val"]
                 and 0 <= event["word"] < 2**32 and event["word"].bit_count() % 2 == 1
                 and (event["word"] >> 16 & 0x7FFF) == 0x3F00
                 and event["word"] & 0xFFFF == event["val"]
                 and type(completed_status_final) is int and 0 <= completed_status_final < 16
                 and event["val"] == (completed_status_final + 1) % 16,
                 "tail original statusserial word/parity/cycle differs")
            five = bytes((event["word"] >> shift) & 127 for shift in (0, 7, 14, 21, 28))
            need(data == bytes.fromhex("f000200d1000") + five + b"\xf7",
                 "tail original statusserial raw five bytes differ")
            register_words += 1
        kinds.append(item["kind"])
        indices.append(item["message_index"])
        encoded.extend(tail_usb_payload(data, tail["cable"]))
    need(kinds in (["meter"], ["statusserial"], ["meter", "statusserial"])
         and indices == list(range(indices[0], indices[0] + len(indices)))
         and bytes(encoded) == payload, "tail order/inventory/full original MIDI bytes differ")
    seal = tail["own_seal"]
    need(type(seal) is dict and set(seal) == {
        "end_ack_monotonic", "final_refresh_request", "final_refresh_ack_wall_range_ns",
        "final_device_finish_wall_range_ns", "final_live_guard_finish_wall_range_ns",
        "final_live_guard_rows_sha256",
    } and type(seal["end_ack_monotonic"]) in (int, float)
         and math.isfinite(seal["end_ack_monotonic"]) and seal["end_ack_monotonic"] >= 0
         and type(seal["final_refresh_request"]) is int and seal["final_refresh_request"] > 0,
         "tail complete own seal metadata differs")
    sha(seal["final_live_guard_rows_sha256"])
    for key in ("final_refresh_ack_wall_range_ns", "final_device_finish_wall_range_ns",
                "final_live_guard_finish_wall_range_ns"):
        bounds = seal[key]
        need(type(bounds) is list and len(bounds) == 2
             and all(type(value) is int and value >= 0 for value in bounds)
             and bounds[0] <= bounds[1] < packet["timestamp_ns"],
             "unconfirmed autonomous traffic is not strictly after complete own seal")
    return len(messages), register_words


def validate_outgoing_completion_scope(assessment):
    tail = assessment.get("unconfirmed_autonomous_eof_tail")
    scope = assessment.get("outgoing_completion_scope")
    if scope is None:
        need(tail is None, "unconfirmed tail lacks its versioned completion scope")
        return 0, 0
    counts = assessment["message_counts"]
    need(type(scope) is dict and set(scope) == {
        "schema", "transfer_scope", "confirmed_bound_out_urbs", "confirmed_bound_out_pairs_sha256",
        "controlled_transfers_complete", "native_callback_transfers_complete",
        "refresh_transfers_complete",
        "all_outgoing_transfers_completed", "completed_autonomous_messages",
        "unconfirmed_autonomous_messages",
    } and scope["schema"] == "oscmix-desk.outgoing-completion-scope.v2"
         and scope["transfer_scope"] == "bound-midi-out-endpoint"
         and type(scope["confirmed_bound_out_urbs"]) is int
         and scope["confirmed_bound_out_urbs"] >= 0
         and scope["controlled_transfers_complete"] is True
         and scope["native_callback_transfers_complete"] is True
         and scope["refresh_transfers_complete"] is True
         and scope["all_outgoing_transfers_completed"] is (tail is None)
         and type(scope["completed_autonomous_messages"]) is int
         and scope["completed_autonomous_messages"]
         == counts["autonomous_statusserial"] + counts["autonomous_meter_requests"]
         and type(scope["unconfirmed_autonomous_messages"]) is int,
         "explicit outgoing completion scope differs")
    sha(scope["confirmed_bound_out_pairs_sha256"])
    tail_counts = (0, 0) if tail is None else validate_autonomous_eof_tail(
        tail, assessment["status_serial_final"],
    )
    need(scope["unconfirmed_autonomous_messages"] == tail_counts[0],
         "unconfirmed autonomous inventory differs")
    if tail is not None:
        need(tail["full_prior_out_completion_pair_count"] == scope["confirmed_bound_out_urbs"]
             and tail["full_prior_out_completion_pairs_sha256"]
             == scope["confirmed_bound_out_pairs_sha256"],
             "tail prior completed transfer commitment differs")
    return tail_counts


def validate_outgoing(assessment):
    tail_messages, tail_register_words = validate_outgoing_completion_scope(assessment)
    counts, completions, hashes = (
        assessment[key]
        for key in ("message_counts", "fragment_completion_counts", "assessed_groups_sha256")
    )
    need(
        set(counts) == set(completions) == set(hashes) == OUTGOING_GROUPS
        and all(type(count) is int and count >= 0 for count in counts.values())
        and all(
            type(completions[name]) is int and completions[name] >= counts[name] for name in counts
        )
        and counts["autonomous_statusserial"] > 0
        and counts["own_final_refresh"] in (0, 1)
        and (counts["unchanged_native_zero_corrections"] == 0 or counts["own_final_refresh"] == 1)
        and assessment["status_register"] == 0x3F00
        and assessment["status_frame_bytes"] == 12
        and assessment["status_serial_cycle"] == 16
        and type(assessment["status_serial_initial"]) is int
        and 0 <= assessment["status_serial_initial"] < 16
        and type(assessment["status_serial_final"]) is int
        and assessment["status_serial_final"]
        == (assessment["status_serial_initial"] + counts["autonomous_statusserial"] - 1) % 16
        and assessment["meter_request_hex"] == "f000200d1002f7"
        and assessment["refresh_register"] == 0x3E04
        and assessment["refresh_value"] == 0x67CD
        and assessment["native_correction_words"] == 2
        and assessment["native_correction_values"] == [0, 0]
        and assessment["native_correction_phase"] == "own-final-REFRESH-after-END-watch"
        and assessment["outgoing_register_words"]
        == 120
        + tail_register_words
        + counts["autonomous_statusserial"]
        + counts["own_final_refresh"]
        + 2 * counts["unchanged_native_zero_corrections"]
        and assessment["sealed_playback_coefficient_words"] == 120
        and type(assessment["assessed_outgoing_messages"]) is int
        and assessment["assessed_outgoing_messages"] == 3 + sum(counts.values()) + tail_messages
        and assessment["all_outgoing_messages_assessed"] is True
        and assessment["coefficient_device_readback"] is False,
        "autonomous/REFRESH/native OUT assessment differs",
    )
    validate_native_refresh_proof(assessment)
    for value in hashes.values():
        sha(value)
    sha(assessment["source_proof_sha256"])


def validate_guards(guards):
    need(
        guards.get("peer_association_scope")
        == "owned-unit-and-current-kernel-backend-family"
        and guards.get("global_socket_owner_exclusivity_claim") is False
        and guards.get("owned_unit_no_subgroups") is True
        and type(guards.get("fresh_scoped_peer_checks")) is int
        and guards["fresh_scoped_peer_checks"] > 0,
        "explicit scoped peer qualification absent",
    )
    sha(guards["scoped_peer_facts_sha256"])
    for key in (
        "static_sources_qualified_outside_METER_callbacks",
        "static_immediately_before_each_WRITE_and_REFRESH",
        "meter_receipt_age_rechecked_after_static",
    ):
        need(guards[key] is True, "full static/live/receipt guard assessment absent")
    # Preserve the legacy decimal rendering and the exact qualified runtime
    # arithmetic. No neighboring float, rounding or tolerance is admitted.
    for key, expected in (
        ("inner_meter_seconds", (3.8,)),
        ("outer_meter_seconds", (4.0,)),
        ("static_seconds", (0.2,)),
        ("write_submission_seconds", (5.2,)),
        ("upfront_writer_cost", (85.38, 0.2 + 2 * (20.1 + 0.2) + 42.38 + 2.0 + 0.2)),
        ("narrowed_capture_cost", (42.38,)),
        ("cleanup_reserve", (25.22,)),
    ):
        need(
            type(guards[key]) in (int, float)
            and math.isfinite(guards[key]) and guards[key] in expected,
            "source qualification cost/budget differs",
        )
    need(
        type(guards["actual_static_qualifications"]) is int
        and guards["actual_static_qualifications"] >= 9,
        "actual source rehashes missing",
    )
    elapsed = guards["actual_max_static_seconds"]
    need(
        type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 0.2 + 1e-9,
        "actual static qualification exceeded its bound",
    )
    sha(guards["raw_stages_sha256"])
    sha(guards["raw_meter_attempts_sha256"])


def validate_case(case):
    left, right = case["pair"]
    need(type(left) is int and left in PAIRS and right == left + 1, "invalid linked pair")
    need(case["independently_on_addressed_channel"] == left, "ON scope overstated")
    expected = (("probe", left, 1), ("restore-target", left, 0), ("restore-partner", right, 0))
    need(len(case["writes"]) == 3, "exactly three sealed writes required")
    for write, (phase, channel, value) in zip(case["writes"], expected, strict=True):
        need(
            write
            == dict(
                phase=phase,
                path=f"/playback/{channel}/mute",
                tags="i",
                args=[value],
                submitted=True,
                acknowledged=True,
            ),
            "write differs",
        )
        need(type(write["args"][0]) is int, "integer mute argument required")
    need(case["end_acknowledged"] is True and case["restored"] is True, "lease/restore absent")
    need(
        type(case["writer_seconds"]) in (int, float)
        and math.isfinite(case["writer_seconds"])
        and 0 < case["writer_seconds"] <= 87,
        "writer limit violated",
    )
    need(
        case["begin_count"] == 1 and case["end_count"] == 1 and case["lease_limit_seconds"] == 90,
        "sole lease lifecycle differs",
    )
    need(
        type(case["watch_seconds"]) in (int, float)
        and math.isfinite(case["watch_seconds"])
        and case["watch_seconds"] >= 24
        and case["watch_static_device_paths"] == 0
        and case["watch_after_end"] is True
        and case["final_after_watch"] is True,
        "strict watch absent",
    )
    validate_watch(case)
    validate_guards(case["source_guards"])
    validate_outgoing(case["assessed_outgoing"])
    phases = case["cache"]
    need(set(phases) == {"baseline", "probe", "restoration"}, "cache phases missing")
    for name, view in phases.items():
        need(
            view["independent_copies"] == 2 and view["coefficient_count"] == 800,
            "full independent cache copies absent",
        )
        need(
            matrix_sha(view["matrix_bits"]) == sha(view["matrix_bits_sha256"]),
            "full matrix SHA differs",
        )
        sha(view["raw_sha256"])
        need(
            view["native_observed"]
            == dict.fromkeys(("inputs", "outputs"), [int(name == "baseline")] * 20),
            "exact40 native freshness lifecycle differs",
        )
        need(
            len(view["flags"]) == 20
            and all(
                len(row) == 3
                and all(type(v) is int for v in row)
                and row[0] in (0, 1)
                and row[1] in (0, 1)
                and -100 <= row[2] <= 100
                for row in view["flags"]
            ),
            "full20 valid flags required",
        )
    base = phases["baseline"]
    need(
        all(view["matrix_bits_sha256"] == base["matrix_bits_sha256"] for view in phases.values()),
        "matrix changed",
    )
    probe = [row.copy() for row in base["flags"]]
    for index in (left - 1, right - 1):
        need(probe[index] == [1, 0, 100], "selected own linked baseline invalid")
        probe[index][1] = 1
    need(
        phases["probe"]["flags"] == probe and phases["restoration"]["flags"] == base["flags"],
        "flags changed outside mute/restore",
    )
    device = case["device"]
    need(set(device) == {"baseline_first", "baseline", "final"}, "DEVICE phases absent")
    for view in device.values():
        sha(view["messages_sha256"])
        sha(view["raw_sha256"])
        need(
            view["path_count"] == 2232
            and view["phantom_power"]
            and all(value == ["i", [0]] for value in view["phantom_power"].values()),
            "DEVICE/48V projection invalid",
        )
    need(len({view["messages_sha256"] for view in device.values()}) == 1, "DEVICE differs")
    need(
        [view["phase"] for view in case["wire_phases"]] == [item[0] for item in expected],
        "wire phases missing",
    )
    for view in case["wire_phases"]:
        need(
            view["coefficient_words"] == 40
            and isinstance(view["fragment_completions"], list)
            and view["fragment_completions"]
            and view["physical_device_readback"] is False,
            "host completion contract invalid",
        )
        for completion in view["fragment_completions"]:
            need(
                type(completion["submit"]) is int
                and type(completion["complete"]) is int
                and 0 <= completion["submit"] < completion["complete"],
                "URB completion differs",
            )
    need(len(case["quiet_meters"]) == 3, "three meter guards required")
    for meter in case["quiet_meters"]:
        need(
            meter["path_count"] == 60
            and meter["minimum_sequence_count"] >= 2
            and meter["peak_limit_dbfs"] == -40.0,
            "fresh60 meter scope differs",
        )
        sha(meter["raw_sha256"])
        sha(meter["live_stages_sha256"])
        for key, bound in (
            ("outer_actual_seconds", 4.0),
            ("inner_actual_seconds", 3.8),
            ("pre_static_actual_seconds", 0.2),
        ):
            need(
                type(meter[key]) in (int, float)
                and math.isfinite(meter[key])
                and 0 <= meter[key] <= bound + 1e-9,
                "actual meter/source duration differs",
            )
    validate_boundaries(case["capture_boundaries"], case["usb_capture_packet_count"])
    need(case["usb_scope"] == USB_SCOPE, "unsupported full-stream/boundary claim")
    need(case["claims"] == dict.fromkeys(NO_CLAIMS, False), "unsupported safety/readback claim")
    required = {
        "raw.json",
        "review.json",
        "result.json",
        "root_result",
        "root_post",
        "root_acceptance",
        "batch_review",
        "batch_result",
        "bench_receipt",
        "inputs",
    }
    need(set(case["evidence_sha256"]) == required, "evidence commitments incomplete")
    need(set(case["source_sha256"]) == INSTRUMENT_FILES, "instrument source map incomplete")
    need(
        {"capture.pcap", "decoded.json", "binding.json", "binding-after.json", "manifest.json"}
        <= set(case["usb_file_sha256"]),
        "USB evidence commitments incomplete",
    )
    deps = case["dependency_source_sha256"]
    need(
        set(deps) == {"usb", "cache", "readonly_helper", "outgoing_proof"}
        and set(deps["usb"])
        == {"binding.py", "decoder.py", "review.py", "review_capture.py", "run.py"}
        and set(deps["cache"]) == {"inspect-cache.py", "review.py", "layout-proof.json"}
        and set(deps["readonly_helper"]) == {"batch_review_py"}
        and set(deps["outgoing_proof"]) == {"outgoing.c", "qualify.py"},
        "dependency source map incomplete",
    )
    for group in (
        case["evidence_sha256"],
        case["source_sha256"],
        case["usb_file_sha256"],
        *deps.values(),
    ):
        need(group, "empty source/evidence commitment")
        for value in group.values():
            sha(value)


def validate(fixture, *, require_complete=False):
    need(
        fixture["schema"] == 1
        and fixture["kind"] == "playback-mute-probes-ucx2"
        and fixture["device"] == "Fireface UCX II"
        and fixture["serial"] == "24216011",
        "wrong fixture identity",
    )
    backend = fixture["backend"]
    need(
        backend["protocol"] == "ODK1"
        and re.fullmatch(r"[0-9a-f]{40}", backend["upstream"])
        and re.fullmatch(r"[0-9a-f]{40}", backend["gnu_build_id"]),
        "backend identity invalid",
    )
    for key in ("binary_sha256", "bridge_sha256", "series_sha256", "source_manifest_sha256"):
        sha(backend[key])
    need(
        {"oscmix.c", "control.c", "device_ffucxii.c", "desk-build.h"}
        <= set(backend["source_sha256"]),
        "binary source hashes absent",
    )
    for value in backend["source_sha256"].values():
        sha(value)
    for case in fixture["cases"]:
        validate_case(case)
    pairs = [case["pair"][0] for case in fixture["cases"]]
    need(pairs == sorted(set(pairs)), "duplicate/unsorted cases")
    need(fixture["independent_on_measurements"] == len(pairs), "independent ON count overstated")
    need(
        fixture["directly_addressed_paths"]
        == sorted(f"/playback/{n}/mute" for case in fixture["cases"] for n in case["pair"]),
        "direct coverage differs",
    )
    missing = [[n, n + 1] for n in PAIRS if n not in pairs]
    need(
        fixture["missing_pairs"] == missing and fixture["matrix_scope_complete"] is (not missing),
        "missing hardware hidden",
    )
    need(
        fixture["policy"]
        == dict(verify_class="re-established", persistence="remember", cold_start_qualified=False),
        "register policy differs",
    )
    need(fixture["claims"] == dict.fromkeys(NO_CLAIMS, False), "unsupported top-level claim")
    need(fixture["representative_pcm_effect"] is None, "PCM needs separate accepted fixture")
    if require_complete:
        need(not missing, "Playback hardware evidence incomplete")


def check_model(device, register_at):
    for channel in range(1, 21):
        register = register_at(device, f"/playback/{channel}/mute")
        need(
            register is not None
            and register.tags == "i"
            and register.verify.value == "re-established"
            and register.policy.value == "remember",
            "runtime register differs",
        )


def covered_paths(value):
    """A complete validated fixture contributes actual submitted write paths."""
    validate(value, require_complete=True)
    return {write["path"] for case in value["cases"] for write in case["writes"]}


def check_manifest(value, manifest_bytes):
    """Retained evidence is tied to the exact shipped backend patch manifest."""
    import json

    backend = value["backend"]
    need(
        hashlib.sha256(manifest_bytes).hexdigest() == backend["series_sha256"],
        "shipped backend series differs",
    )
    manifest = json.loads(manifest_bytes)
    need(
        manifest["protocol"] == backend["protocol"]
        and manifest["upstream"] == backend["upstream"],
        "shipped backend identity differs",
    )
