"""Bound a small route-pan hardware fixture to its measured warm scope.

These checks validate the retained projection. Reproducing raw PCAP/cache
evidence and accepting a measurement remains the private projector's job.
"""

import hashlib
import json
import math
import re
import struct
from pathlib import Path

CASES = (
    "negative-small",
    "positive-small",
    "left-endpoint",
    "right-endpoint",
    "right-near-endpoint",
)
VECTORS = ((-60.0, -1), (-60.0, 1), (-60.0, -100), (-60.0, 100), (-20.0, 99))
STAGES = ("baseline_first", "baseline", "probe", "restoration", "final")
READBACK_SCOPE = "oscmix-desk.input-pan-readback-scope.v3"
PATHS = (
    "/input/13/stereo",
    "/input/14/stereo",
    "/output/1/stereo",
    "/output/2/stereo",
    "/input/1/48v",
    "/input/2/48v",
    "/mix/1/input/13",
)
REGS = (0x400D, 0x404D, 0x400C, 0x404C, 0x200C, 0x200C, 0x200D, 0x200D)
VOLATILE_PATHS = frozenset(
    "/input/%d/%s/meter" % (channel, family)
    for channel in (1, 2)
    for family in ("autolevel", "dynamics")
)


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(value):
    need(
        type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        "strict SHA256 required",
    )


def number(value):
    need(type(value) in (int, float) and math.isfinite(value), "finite plain number required")


def cache_phase(value, expected):
    for key in ("native_count", "output_count", "matrix_coefficients", "independent_copies"):
        need(type(value[key]) is int, "strict cache counts required")
    need(
        value["native_stereo_observed"] == value["output_stereo_observed"] == expected,
        "exact cache freshness phase required",
    )
    need(
        type(value["native_stereo_observed"]) is int
        and type(value["output_stereo_observed"]) is int,
        "strict cache BOOL encoding required",
    )
    need(value["native_count"] == value["output_count"] == 20, "whole40 observed flags required")
    need(
        value["matrix_coefficients"] == 800 and value["independent_copies"] == 2,
        "whole800/two-copy cache required",
    )
    need(len(value["owned_bits"]) == 4, "four physical cells required")
    for bits in value["owned_bits"]:
        need(
            type(bits) is str and re.fullmatch(r"[0-9a-f]{8}", bits) is not None,
            "strict Float32 bits required",
        )
        coefficient = struct.unpack("<f", bytes.fromhex(bits))[0]
        need(
            math.isfinite(coefficient) and 0 <= coefficient <= 2,
            "finite cache coefficient required",
        )
    sha(value["matrix_sha256"])
    sha(value["full_spans_sha256"])


def boundary(value):
    need(
        set(value)
        == {
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
            "complete_message",
            "register_feedback_claim",
            "write_acceptance_claim",
            "assessment",
            "complete_messages_generated",
            "register_events_generated",
        },
        "exact EOF fragment fields required",
    )
    need(
        value["scope"] == "opaque-unassessed-incoming-EOF-fragment"
        and value["assessment"] == "opaque unassessed"
        and value["direction"] == "in",
        "only opaque incoming EOF fragments may be retained",
    )
    for key in (
        "cable",
        "endpoint",
        "start_packet",
        "last_packet",
        "start_timestamp_ns",
        "last_timestamp_ns",
        "bytes",
        "complete_messages_generated",
        "register_events_generated",
    ):
        need(type(value[key]) is int and value[key] >= 0, "strict EOF positions/counts required")
    need(
        value["cable"] == 1
        and value["endpoint"] == 0x86
        and value["start_packet"] <= value["last_packet"]
        and value["start_timestamp_ns"] <= value["last_timestamp_ns"],
        "own IN cable/positions required",
    )
    need(
        all(
            value[key] is False
            for key in ("complete_message", "register_feedback_claim", "write_acceptance_claim")
        )
        and value["complete_messages_generated"] == value["register_events_generated"] == 0,
        "EOF fragments are neither messages nor feedback/write evidence",
    )
    need(
        type(value["hex"]) is str and re.fullmatch(r"(?:[0-9a-f]{2})+", value["hex"]),
        "exact retained EOF bytes required",
    )
    raw = bytes.fromhex(value["hex"])
    need(
        len(raw) == value["bytes"] and raw[0] == 0xF0 and b"\xf7" not in raw,
        "open SysEx byte scope required",
    )
    sha(value["sha256"])
    need(hashlib.sha256(raw).hexdigest() == value["sha256"], "retained EOF byte SHA differs")
    need(value["fragment_locations"], "actual fragment locations required")
    previous = (-1, -1)
    for location in value["fragment_locations"]:
        need(
            set(location) == {"packet_index", "event_offset"}
            and type(location["packet_index"]) is int
            and type(location["event_offset"]) is int
            and value["start_packet"] <= location["packet_index"] <= value["last_packet"]
            and location["event_offset"] >= 0,
            "actual fragment packet/offset required",
        )
        current = location["packet_index"], location["event_offset"]
        need(current >= previous, "ordered fragment locations required")
        previous = current


def statusserial(value):
    for key in ("register", "messages", "cycle", "words", "frame_bytes"):
        need(type(value[key]) is int, "strict periodic status counts required")
    need(
        value["scope"] == "source-defined-periodic-status-command"
        and value["register"] == 0x3F00
        and value["cycle"] == 16
        and value["words"] == 1
        and value["frame_bytes"] == 12
        and value["messages"] >= 0
        and value["register_feedback_claim"] is False
        and value["all_fragment_completions_confirmed"] is True,
        "source-defined status command evidence required",
    )
    if value["messages"]:
        need(
            all(type(value[key]) is int and 0 <= value[key] < 16 for key in ("first", "last"))
            and value["last"] == (value["first"] + value["messages"] - 1) % 16,
            "exact cyclic statusserial summary required",
        )
    else:
        need(value["first"] is value["last"] is None, "empty status summary has no serial")


def volatile_watch(watch):
    for value in (watch["seconds"], watch["start_after_end_seconds"]):
        number(value)
    need(
        watch["scope"] == "24s-nonvolatile-DEVICE-watch-zero"
        and watch["seconds"] >= 24
        and 0 <= watch["start_after_end_seconds"] <= 0.25
        and type(watch["nonvolatile_device_events"]) is int
        and watch["nonvolatile_device_events"] == 0
        and watch["exclusive"] is False
        and watch["raw_volatile_messages_retained"] is True,
        "prompt nonvolatile-zero nonexclusive postENDwatch required",
    )
    frames, counts = watch["volatile_raw_device_frames"], watch["volatile_raw_messages_by_path"]
    need(
        type(frames) is int
        and frames >= 0
        and set(counts) == VOLATILE_PATHS
        and all(type(count) is int and count >= 0 for count in counts.values()),
        "exact four qualified volatile path counts required",
    )
    need(
        (frames == 0 and sum(counts.values()) == 0) or 0 < frames <= sum(counts.values()),
        "retained volatile frame/message counts differ",
    )
    sha(watch["raw_control_sha256"])


def source_guards(value):
    need(
        set(value)
        == {
            "static_sources_qualified_outside_METER_callbacks",
            "static_immediately_before_each_WRITE_and_REFRESH",
            "meter_receipt_age_rechecked_after_static",
            "peer_association_scope",
            "global_socket_owner_exclusivity_claim",
            "owned_unit_no_subgroups",
            "inner_meter_seconds",
            "outer_meter_seconds",
            "static_seconds",
            "write_submission_seconds",
        },
        "exact independently reconstructed source guard summary required",
    )
    for field in (
        "static_sources_qualified_outside_METER_callbacks",
        "static_immediately_before_each_WRITE_and_REFRESH",
        "meter_receipt_age_rechecked_after_static",
    ):
        need(value[field] is True, "full static and fresh after-static WRITE guard required")
    need(
        value["peer_association_scope"] == "owned-unit-and-current-kernel-backend-family"
        and value["global_socket_owner_exclusivity_claim"] is False
        and value["owned_unit_no_subgroups"] is True,
        "explicit owned unit and kernel family scope required",
    )
    for field, expected in (
        ("inner_meter_seconds", 3.8),
        ("outer_meter_seconds", 4.0),
        ("static_seconds", 0.2),
        ("write_submission_seconds", 5.2),
    ):
        number(value[field])
        need(value[field] == expected, "unchanged source and transport budget required")


INPUT_SEAL_COMMON = (
    "end_ack_monotonic", "final_refresh_request", "final_refresh_ack_wall_range_ns",
    "final_device_finish_wall_range_ns", "final_live_guard_finish_wall_range_ns",
    "final_live_guard_rows_sha256",
)
INPUT_SEAL_HASHES = (
    "bench_receipt_sha256", "peer_sha256", "control_trace_sha256",
    "final_device_sha256", "own_cache_artifacts_sha256", "watch_review_sha256",
)
INPUT_EOF_FIELDS = frozenset((
    "eof_projection_schema", "outgoing_completion_scope", "unconfirmed_autonomous_eof_tail",
    "input_own_seal", "completed_autonomous_message_counts",
))


def input_own_seal(seal, *, baseline, receipt_sha=None):
    """Retain all fifteen Input witnesses; Common6 is only a validator view."""
    need(type(baseline) is bool and type(seal) is dict
         and set(seal) == {"schema", "kind", "control_writes", *INPUT_SEAL_COMMON,
                           *INPUT_SEAL_HASHES}
         and seal["schema"] == "oscmix-desk.input-pan-own-seal.v1"
         and seal["kind"] == ("own-bench-pan-raw-baseline" if baseline
                               else "warm-input-route-pan-pilot")
         and type(seal["control_writes"]) is int
         and seal["control_writes"] == (0 if baseline else 3),
         "complete fifteen-field Input own seal required")
    for key in (*INPUT_SEAL_HASHES, "final_live_guard_rows_sha256"):
        sha(seal[key])
    if receipt_sha is not None:
        sha(receipt_sha)
        need(seal["bench_receipt_sha256"] == receipt_sha, "Input own seal receipt differs")
    number(seal["end_ack_monotonic"])
    need(seal["end_ack_monotonic"] >= 0
         and type(seal["final_refresh_request"]) is int and seal["final_refresh_request"] > 0,
         "Input own seal END/final request differs")
    for key in INPUT_SEAL_COMMON[2:5]:
        bounds = seal[key]
        need(type(bounds) is list and len(bounds) == 2
             and all(type(value) is int and value >= 0 for value in bounds)
             and bounds[0] <= bounds[1], "Input own seal wall range differs")
    return {key: seal[key] for key in INPUT_SEAL_COMMON}


def input_eof_scope(value, pcap_sha, completed_status, *, baseline, receipt_sha=None, common=None):
    """Validate explicit Input scope without classifying an unconfirmed OUT as completed.

    Scope3 requires all five fields, including captures with no EOF tail.
    Historical unqualified compact schemas have no public acceptance branch.
    """
    present = INPUT_EOF_FIELDS & set(value)
    need(present == INPUT_EOF_FIELDS, "complete explicit Input EOF scope required")
    need(value["eof_projection_schema"] == "oscmix-desk.input-pan-eof-projection.v2",
         "versioned Input EOF projection required")
    sha(pcap_sha)
    counts = value["completed_autonomous_message_counts"]
    need(type(counts) is dict and set(counts) == {"statusserial", "meter"}
         and all(type(count) is int and count >= 0 for count in counts.values())
         and counts["statusserial"] == completed_status["messages"],
         "Input completed autonomous inventory differs")
    view_seal = input_own_seal(value["input_own_seal"], baseline=baseline,
                               receipt_sha=receipt_sha)
    tail = value["unconfirmed_autonomous_eof_tail"]
    view_tail = None
    if tail is not None:
        need(type(tail) is dict and tail["own_seal"] == value["input_own_seal"]
             and tail["original_pcap_sha256"] == pcap_sha,
             "Input full own seal/original PCAP differs")
        # Keep the original fifteen-field seal and its runtime hash in the fixture.
        # Only this temporary copy satisfies PB398's generic Common6 validator API.
        view_tail = dict(tail, own_seal=view_seal)
    if common is None:
        common = globals().get("_PB398_COMMON")
        if common is None:
            import playback_mute_evidence as common
    common.validate_outgoing_completion_scope(dict(
        outgoing_completion_scope=value["outgoing_completion_scope"],
        unconfirmed_autonomous_eof_tail=view_tail,
        message_counts=dict(autonomous_statusserial=counts["statusserial"],
                            autonomous_meter_requests=counts["meter"]),
        status_serial_final=completed_status["last"],
    ))


SEALED_WORDS = {'left-endpoint': [3222077440, 1078788096, 1074528289, 3226206208,
    2685173160, 537722780, 537754998, 537755648], 'negative-small': [3222077440,
        3226271776, 1074528289, 3226206208, 2685173160, 537722780, 537755047, 2685239396],
            'positive-small': [3222077440, 1078788129, 3222011936, 3226206208, 2685173159,
                537722780, 537755048, 2685239396], 'restore': [3222077440, 1078788114,
                    1074528274, 3226206208, 2685173110, 537722780, 537754998, 2685239396],
                        'right-endpoint': [3222077440, 1078788129, 1074528256, 3226206208,
                            2685173110, 2685173760, 537755048, 2685239396],
                                'right-near-endpoint': [3222077440, 3226275021,
                                    1074528289, 3226206208, 2685173160, 537722780,
                                        2685239096, 2685239396]}


def evidence_digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def original_words(message):
    payload = bytes.fromhex(message["hex"])
    need(message["kind"] == "rme" and message["sub"] == 0
         and payload[:6] == bytes.fromhex("f000200d1000") and payload[-1:] == b"\xf7"
         and len(payload) >= 12 and (len(payload) - 7) % 5 == 0,
         "complete original register message required")
    words = []
    for offset in range(6, len(payload) - 1, 5):
        part = payload[offset:offset + 5]
        need(all(value < 128 for value in part) and part[4] < 16,
             "canonical original five-byte word required")
        word = sum(value << (7 * index) for index, value in enumerate(part))
        need(word.bit_count() % 2 == 1, "original register parity differs")
        words.append(word)
    return words


def completed_pairs(value):
    need(type(value) is list and bool(value), "all original OUT fragment completions required")
    previous = -1
    for pair in value:
        need(type(pair) is dict and set(pair) == {"submit", "complete", "urb"}
             and all(type(pair[key]) is int and pair[key] >= 0 for key in pair)
             and previous < pair["submit"] < pair["complete"],
             "original OUT completion pair differs")
        previous = pair["submit"]


PORTABLE_SOURCE_AUTHORITY = {
    'callback_results_match_historical': True,
    'current_backend_elf_sha256': (
        '11fd89c9c92e794757bbfc44b8ae7715b7dc1a5d58612ca6f2d8da83a23c4d23'
    ),
    'current_callback_variants': 3,
    'current_observation_sources': 11,
    'current_project_build_id': 'ed304b2afb25adceb20b3cdc83e4d1d4ef640d7a655ed04c2605fcde805dae2c',
    'current_qualification_sha256': (
        'd5a7063f55fd89d168d586d5a53ba231e724f4bb55f4b69c4b3c8ccaf9e5fced'
    ),
    'current_source_manifest_sha256': (
        '11b83d33ce300aa367784c6f4fd3610957ac6998c53a5fcdaa2eebf4b1fbfbfd'
    ),
    'historical_build_relabelled': False,
    'historical_source_authority': {
        'actual_callback242_result_sha256': (
            'ce511afde6e6950c7fd7ea65221ba809d35fe7c14d91c3b94c8cc608f0bc86a6'
        ),
        'actual_callback_proof_sha256': (
            '37d3a79fe7005f61460629623f7b16eed6c3983a40680b89f4644fae955085a3'
        ),
        'handoff_sha256': '0157991ff6c1696876a00bbf1e29439e09f7ecdd4d3c294552c4525e73863dee',
        'project_build_id': '2d732838e7f39ebebc5f667e961c97a256358221c5a408a33de382ad33099509',
        'reconstructed_export_source_sha256': (
            '8f5def69d0e2a66bda932f99446165109b1d931f20455ee7a3b3424da71836c7'
        ),
        'requested_export_source_sha256': (
            '616d1aaa34f9df4dcf9ff59d25117e3a88270cdbf181db547c5e3320b12e14f8'
        ),
        'source_manifest_sha256': (
            'a3bb03ac767d85f76a7e3c1682f5595e5652a37a5c3825c50d2bb32ee10f9885'
        ),
        'source_sha256': '54518ae559a76740347cf6e40369a908c6495e9a35e1142f273f12235796380f',
    },
    'observation_proof_sha256': '957f651fb4b3a4b614462e3f017a8f46d9bdbef386a3f7c1deeb1f095be60fb0',
    'outgoing_proof_sha256': '3a4c2d8abce57f21c94a3de11348b3e815f24210c661db8370198cfee173b72c',
    'schema': 'oscmix-desk.input-pan-portable-source-authority.v1',
}


def source_authority_bridge(value):
    """A portable C14 commitment retains the distinct historical callback identity."""
    expected = PORTABLE_SOURCE_AUTHORITY
    need(type(value) is dict
         and set(value) == set(expected) | {"runtime_authority_bridge_sha256"}
         and evidence_digest({key: value[key] for key in expected}) == evidence_digest(expected),
         "exact current C14 and distinct historical source authority required")
    sha(value["runtime_authority_bridge_sha256"])


def solicited_words(name):
    """Probe8 and restore6 original halves; whole own RAW restoration remains separate."""
    need(name in (*CASES, "restore"), "only five sealed cases and own restore")
    words = list(SEALED_WORDS[name][4:])
    for source, target in ((0x200C, 0x204C), (0x200D, 0x204D)):
        for pan in ((False,) if name == "restore" else (False, True)):
            first = next(word for word in words if word >> 16 & 0x7FFF == source
                         and bool(word & 0x8000) is pan)
            word = target << 16 | first & 0xFFFF
            words.append(word | (0 if word.bit_count() % 2 else 0x80000000))
    return words


def solicited_metadata_readback(case):
    """Check retained original words and separate common-probe/restoration commitments."""
    proof = case["usb"]["solicited_metadata_readback"]
    runtime_fields = {
        "schema", "canonical_path", "canonical_value", "other_original_device_messages_unchanged",
        "original_device_messages_sha256", "completed_probe_writes", "common_probe", "restoration",
        "per_write_unsolicited_echo_claim", "coefficient_device_readback_claim",
        "empirical_second_row_metadata",
    }
    extra = {"runtime_wire_proof_sha256", "case_REFRESH_count", "original_messages",
             "read_trace_ownership", "completed_restoration_write"}
    need(type(proof) is dict and set(proof) == runtime_fields | extra
         and proof["schema"] == "oscmix-desk.input-pan-solicited-metadata-readback.v3"
         and proof["canonical_path"] == "/mix/1/input/13"
         and proof["canonical_value"] == ["fi", [case["requested_db"], case["requested_pan"]]]
         and proof["other_original_device_messages_unchanged"] == 2231
         and type(proof["other_original_device_messages_unchanged"]) is int
         and proof["case_REFRESH_count"] == 5 and type(proof["case_REFRESH_count"]) is int
         and proof["per_write_unsolicited_echo_claim"] is False
         and proof["coefficient_device_readback_claim"] is False,
         "strict common-probe Scope3 proof required")
    need(type(proof["empirical_second_row_metadata"]) is dict
         and evidence_digest(proof["empirical_second_row_metadata"]) == evidence_digest(dict(
            scope="warm linked input13/14 to output1/2; five sealed cases only",
            register_pairs=[[0x200C, 0x204C], [0x200D, 0x204D]],
            equality_checked_in_own_solicited_probe=True,
            restoration_against_own_baseline=True,
            second_row_pan_equal_canonical_in_probe=True,
            second_row_pan_restore_assessed_by="wholeassessedRAWrestoration",
            probe_metadata_halves=8, restoration_metadata_halves=6,
            source_proven_firmware_rule=False, additional_host_write_claim=False,
        )), "exact limited empirical second-row scope required")
    runtime = {key: proof[key] for key in runtime_fields}
    runtime["schema"] = "oscmix-desk.input-common-probe-readback.v3"
    need(evidence_digest(runtime) == proof["runtime_wire_proof_sha256"],
         "original runtime common-probe commitment differs")
    need(proof["original_device_messages_sha256"]
         == case["device_states"]["baseline"]["messages_sha256"],
         "common-probe original map differs")
    originals, used = {}, set()
    for entry in proof["original_messages"]:
        need(type(entry) is dict and set(entry) == {"message", "original"}
             and type(entry["message"]) is int and entry["message"] >= 0
             and entry["message"] not in originals, "original message inventory differs")
        original = entry["original"]
        need(type(original) is dict
             and type(original["start_packet"]) is int
             and type(original["end_packet"]) is int
             and 0 <= original["start_packet"] <= original["end_packet"],
             "original message packet interval differs")
        originals[entry["message"]] = entry["original"]
    need(list(originals) == sorted(originals), "ordered original message inventory required")
    need(set(proof["read_trace_ownership"]) == {"common_probe", "restoration"},
         "separate owned read indices required")
    bulks = case["usb"]["bulks"]
    need(type(proof["completed_probe_writes"]) is list
        and len(proof["completed_probe_writes"]) == 2,
         "both individual completed probe WRITEs required")
    cable = None
    for index, bulk in enumerate(bulks):
        words = [int(word, 16) for word in bulk["register_words_hex"]]
        need(words == SEALED_WORDS["restore" if index == 2 else case["id"]],
             "source-defined eight-word WRITE differs")
        scope = "common_probe" if index < 2 else "restoration"
        need(bulk["metadata_verification"] == dict(scope=scope,
            proof_sha256=evidence_digest(proof[scope])),
             "individual WRITE bound to wrong solicited readback")
        if index < 3:
            entry = (proof["completed_probe_writes"][index] if index < 2
                     else proof["completed_restoration_write"])
            need(set(entry) == {"phase", "words", "message", "completions", "message_sha256"}
                 and entry["phase"] == bulk["phase"] and entry["words"] == 8
                 and type(entry["words"]) is int and type(entry["message"]) is int,
                 "completed probe WRITE identity differs")
            message = originals[entry["message"]]
            used.add(entry["message"])
            need(message["direction"] == "out" and len(bytes.fromhex(message["hex"])) == 47
                 and original_words(message) == words
                 and evidence_digest(message) == entry["message_sha256"]
                 and type(message["cable"]) is int,
                 "original individually completed probe WRITE bytes differ")
            completed_pairs(entry["completions"])
            need(len(entry["completions"]) == bulk["successful_completions"],
                 "individual probe fragment completion count differs")
            need(entry["completions"][0]["submit"] == message["start_packet"]
                 and entry["completions"][-1]["submit"] == message["end_packet"],
                 "individual WRITE completions belong to other original packets")
            cable = message["cable"] if cable is None else cable
            need(type(cable) is int and 0 <= cable <= 15 and message["cable"] == cable,
                 "one original native cable required")
    previous_finish = None
    read_keys = {
        "request", "acknowledgement", "receipt_boundary", "started_at", "finished_at",
            "wall_range_ns",
        "incoming_raw_refs", "incoming_raw_refs_sha256", "refresh_message",
            "refresh_message_sha256",
        "refresh_completions", "full_device_messages", "full_device_messages_sha256",
            "raw_control_frames_sha256",
    }
    for scope, stage, word_case in (("common_probe", "probe", case["id"]), ("restoration",
        "restoration", "restore")):
        read = proof[scope]
        state = case["device_states"][stage]
        need(set(read) == read_keys and type(read["full_device_messages"]) is int
             and read["full_device_messages"] == state["complete_paths"] == 2232
             and read["full_device_messages_sha256"] == state["messages_sha256"]
             and type(read["receipt_boundary"]) is int
             and read["receipt_boundary"] == state["receipt_boundary"],
             "whole solicited DEVICE map/boundary differs")
        number(read["started_at"])
        number(read["finished_at"])
        need(0 <= read["started_at"] <= read["finished_at"]
             and (previous_finish is None or previous_finish <= read["started_at"]),
             "common-probe/restoration time order differs")
        previous_finish = read["finished_at"]
        request, ack = read["request"], read["acknowledgement"]
        need(request["direction"] == "send" and request["kind"] == 6
            and request["submitted"] is True
             and type(request["request"]) is int and request["request"] > 0
             and ack["direction"] == "receive" and ack["kind"] == (6 | 0x80000000)
             and ack["request"] == request["request"] and ack["code"] == 0
             and type(ack["code"]) is int and type(request["token"]) is int
             and request["token"] > 0 and ack["sequence"] == request["token"],
             "solicited own successful REFRESH request/ACK differs")
        number(request["at"])
        number(ack["at"])
        # Read intervals are monotonic; request/ACK "at" are relative to the trace origin.
        origin = proof["read_trace_ownership"][scope].get("trace_origin_monotonic")
        number(origin)
        need(0 <= origin <= read["started_at"] <= origin + request["at"] <= origin + ack["at"]
             <= read["finished_at"], "REFRESH request/ACK exceeds its own read interval")
        bounds = read["wall_range_ns"]
        need(type(bounds) is list and len(bounds) == 2
             and all(type(value) is int and value >= 0 for value in bounds)
             and bounds[0] <= bounds[1], "solicited physical window differs")
        ownership = proof["read_trace_ownership"][scope]
        need(set(ownership) == {"trace_start_index", "trace_end_index",
            "full_control_trace_length", "trace_origin_monotonic",
                                 "full_control_trace_sha256", "read_slice_sha256"}
             and all(type(ownership[key]) is int for key in ("trace_start_index",
                 "trace_end_index", "full_control_trace_length"))
             and 0 <= ownership["trace_start_index"] < ownership["trace_end_index"]
             <= ownership["full_control_trace_length"]
             and ownership["read_slice_sha256"] == read["raw_control_frames_sha256"],
             "owned half-open CONTROL trace commitments differ")
        sha(ownership["full_control_trace_sha256"])
        sha(ownership["read_slice_sha256"])
        message = originals[read["refresh_message"]]
        used.add(read["refresh_message"])
        words = original_words(message)
        need(message["direction"] == "out" and type(message["cable"]) is int
             and message["cable"] == cable and len(words) == 1
             and (words[0] >> 16 & 0x7FFF) == 0x3E04 and (words[0] & 0xFFFF) == 0x67CD
             and evidence_digest(message) == read["refresh_message_sha256"]
             and type(message["timestamp_ns"]) is int
             and bounds[0] <= message["timestamp_ns"] <= bounds[1],
             "original physical REFRESH bytes/window differ")
        completed_pairs(read["refresh_completions"])
        need(read["refresh_completions"][0]["submit"] == message["start_packet"]
             and read["refresh_completions"][-1]["submit"] == message["end_packet"],
             "REFRESH completions belong to other original packets")
        refs = read["incoming_raw_refs"]
        need(type(refs) is list and evidence_digest(refs) == read["incoming_raw_refs_sha256"],
             "complete original solicited half list commitment differs")
        wanted = {(word >> 16 & 0x7FFF, "mix_pan" if word & 0x8000 else "mix_db"): word
                  for word in solicited_words(word_case)}
        found, indices = set(), []
        for entry in refs:
            need(set(entry) == {"event_index", "event", "event_sha256", "message",
                "message_sha256"}
                 and type(entry["event_index"]) is int and entry["event_index"] >= 0,
                 "original solicited event reference differs")
            indices.append(entry["event_index"])
            event = entry["event"]
            original = originals[entry["message"]]
            used.add(entry["message"])
            originals_words = original_words(original)
            key = event["reg"], event["kind"]
            need(key in wanted and event["direction"] == original["direction"] == "in"
                 and event["message_index"] == entry["message"]
                 and event["parity"] is True
                 and type(event["word_index"]) is int
                     and 0 <= event["word_index"] < len(originals_words)
                 and originals_words[event["word_index"]] == event["word"] == wanted[key]
                 and type(event["val"]) is int and event["val"] == (wanted[key] & 0xFFFF)
                 and type(event["cable"]) is int and type(original["cable"]) is int
                 and event["cable"] == original["cable"] == cable
                 and type(event["timestamp_ns"]) is int
                 and type(original["timestamp_ns"]) is int
                 and event["timestamp_ns"] == original["timestamp_ns"]
                 and message["timestamp_ns"] <= event["timestamp_ns"] <= bounds[1]
                 and evidence_digest(event) == entry["event_sha256"]
                 and evidence_digest(original) == entry["message_sha256"],
                 "original solicited native DB/PAN value/parity/cable/window differs")
            found.add(key)
        need(indices == sorted(set(indices)) and found == set(wanted),
             "all case-specific original solicited halves required without dedupe")
    need(set(originals) == used,
         "full original message inventory has omissions/extras")
    need(proof["read_trace_ownership"]["common_probe"]["full_control_trace_sha256"]
         == proof["read_trace_ownership"]["restoration"]["full_control_trace_sha256"]
         == case["usb"]["input_own_seal"]["control_trace_sha256"]
         and proof["read_trace_ownership"]["common_probe"]["full_control_trace_length"]
         == proof["read_trace_ownership"]["restoration"]["full_control_trace_length"]
         and proof["read_trace_ownership"]["common_probe"]["trace_end_index"]
         <= proof["read_trace_ownership"]["restoration"]["trace_start_index"]
         and proof["read_trace_ownership"]["common_probe"]["trace_origin_monotonic"]
         == proof["read_trace_ownership"]["restoration"]["trace_origin_monotonic"],
         "both readbacks require one ordered original CONTROL trace")


def validate_case(case, name, vector):
    source_authority_bridge(case["source_authority_bridge"])
    need(case["id"] == name, "five sealed cases in order required")
    db, pan = vector
    need(
        type(case["requested_db"]) is float
        and case["requested_db"] == db
        and type(case["requested_pan"]) is int
        and case["requested_pan"] == pan,
        "sealed vector required",
    )
    need(
        case["route"]
        == dict(
            input_pair=[13, 14],
            output_pair=[1, 2],
            target="/mix/1/input/13",
            even_alias="/mix/2/input/14",
        ),
        "own physical route required",
    )
    need(
        tuple(case["device_states"]) == STAGES
        and case["readback_scope"] == READBACK_SCOPE
        and case["probe_full_device_refresh"] is True,
        "explicit solicited Scope3 DEVICE probe required",
    )
    previous = {}
    for stage in STAGES:
        state = case["device_states"][stage]
        need(set(state["reports"]) == set(PATHS), "selected canonical/link/48V paths required")
        need(
            type(state["receipt_boundary"]) is int and state["receipt_boundary"] >= 0,
            "fresh DEVICE boundary required",
        )
        need(
            type(state["complete_paths"]) is int and state["complete_paths"] == 2232,
            "whole2232 DEVICE state required",
        )
        sha(state["messages_sha256"])
        for path, report in state["reports"].items():
            need(
                report["origin"] == "DEVICE"
                and type(report["sequence"]) is int
                and report["sequence"] > max(state["receipt_boundary"], previous.get(path, 0)),
                "fresh independent DEVICE delivery required",
            )
            previous[path] = report["sequence"]
            if path == "/mix/1/input/13":
                need(
                    report["tags"] == "fi"
                    and report["args"] == ([db, pan] if stage == "probe" else ["-inf", 0])
                    and type(report["args"][1]) is int,
                    "restored canonical DEVICE route required",
                )
            else:
                need(
                    report["tags"] == "i"
                    and len(report["args"]) == 1
                    and type(report["args"][0]) is int
                    and report["args"][0] == int(path.endswith("/stereo")),
                    "linked/48V-off DEVICE evidence required",
                )
    writes = case["writes"]
    restore = struct.unpack(">f", struct.pack(">f", -64.99))[0]
    expected_writes = (
        ("probe-odd", "/mix/1/input/13", [db, pan]),
        ("probe-even", "/mix/2/input/14", [db, pan]),
        ("restore", "/mix/1/input/13", [restore, 0]),
    )
    need(len(writes) == 3, "three owned writes required")
    for write, (phase, path, args) in zip(writes, expected_writes, strict=True):
        need(
            write
            == dict(
                phase=phase, path=path, tags="fi", args=args, submitted=True, acknowledged=True
            ),
            "exact three acknowledged writes required",
        )
        need(
            type(write["args"][0]) is float and type(write["args"][1]) is int,
            "strict fi wire types required",
        )
    for name, expected in (("baseline", 1), ("probe", 0), ("restoration", 1)):
        cache_phase(case["cache"][name], expected)
    need(
        case["cache"]["baseline"]["owned_bits"]
        == case["cache"]["restoration"]["owned_bits"]
        == ["00000000"] * 4,
        "four own original positive-zero cells restored required",
    )
    need(
        case["cache"]["baseline"]["matrix_sha256"] == case["cache"]["restoration"]["matrix_sha256"]
        and case["cache"]["baseline"]["full_spans_sha256"]
        == case["cache"]["restoration"]["full_spans_sha256"],
        "whole cache restored exactly required",
    )
    need(
        case["cache"]["playback_and_other_span_bytes_preserved"] is True
        and case["cache"]["mix_observed_pointees_assessed"] is False,
        "honest cache byte scope required",
    )
    allowed = {
        "negative-small": (
            ["6f12833a", "00000000", "00000000", "e4c2813a"],
            ["6f12833a", "00000000", "00000000", "5492813a"],
        ),
        "positive-small": (
            ["e4c2813a", "00000000", "00000000", "6f12833a"],
            ["5492813a", "00000000", "00000000", "6f12833a"],
        ),
        "left-endpoint": (["6f12833a", "00000000", "00000000", "00000000"],),
        "right-endpoint": (["00000000", "00000000", "00000000", "6f12833a"],),
        "right-near-endpoint": (["6f12833a", "00000000", "00000000", "cdcccc3d"],),
    }
    need(
        case["cache"]["probe"]["owned_bits"] in allowed[case["id"]],
        "exact source-prepared or source-reconstructed cache geometry required",
    )
    wire = case["usb"]
    need(
        wire["duration_seconds"] == 60
        and wire["coefficient_device_readback"] is False
        and wire["full_stream_completeness_claim"] is False,
        "bounded transfer evidence required",
    )
    need(
        wire["capture_boundary_completeness_claim"] is False
        and wire["capture_boundary_feedback_claim"] is False
        and wire["complete_messages_only_used_as_register_evidence"] is True,
        "honest complete-message/EOF scope required",
    )
    need(type(wire["capture_boundaries"]) is list, "ordered EOF fragment list required")
    for fragment in wire["capture_boundaries"]:
        boundary(fragment)
    need(
        len(wire["bulks"]) == 3 and wire["final_coefficient_codes"] == [0] * 4,
        "three completed bulks and four final-zero transfers required",
    )
    for entry, (phase, _path, _args) in zip(wire["bulks"], expected_writes, strict=True):
        need(type(entry["frame_bytes"]) is int, "strict bulk byte count required")
        need(
            entry["phase"] == phase
            and entry["frame_bytes"] == 47
            and entry["successful_completions"] > 0
            and type(entry["successful_completions"]) is int,
            "actual47-byte bulk completion required",
        )
        words = entry["register_words_hex"]
        need(
            len(words) == 8
            and all(type(w) is str and re.fullmatch(r"[0-9a-f]{8}", w) for w in words),
            "eight exact wire words required",
        )
        numbers = [int(word, 16) for word in words]
        need(
            tuple(word >> 16 & 0x7FFF for word in numbers) == REGS
            and all(word.bit_count() % 2 == 1 for word in numbers),
            "wire register order/parity required",
        )
        need("complete_raw_db_pan_echo" not in entry,
             "per-WRITE unsolicited echo is outside Scope3")
    need(
        wire["bulks"][0]["register_words_hex"] == wire["bulks"][1]["register_words_hex"],
        "direct even alias must use identical canonical words",
    )
    need(
        wire["all_outgoing_registers_assessed"] is True
        and wire["native_corrections_assessed"] is True,
        "do not claim only four outgoing registers",
    )
    solicited_metadata_readback(case)
    statusserial(wire["statusserial"])
    input_eof_scope(wire, case["evidence_sha256"]["pcap"], wire["statusserial"], baseline=False)
    need(
        case["full_device_restored_before_end"] is True
        and case["whole_assessed_raw_restored"] is True
        and case["end_count"] == 1
        and type(case["end_count"]) is int
        and case["end_acknowledged"] is True,
        "whole restoration and soleEND required",
    )
    volatile_watch(case["watch"])
    need(
        case["quiet_peak_dbfs"] == -40 and case["fresh_meter_paths_per_write"] == 60,
        "individual fresh quiet meter windows required",
    )
    need(type(case["fresh_meter_paths_per_write"]) is int, "strict meter path count required")
    source_guards(case["source_guards"])
    sha(case["guard_journal_sha256"])
    for value in case["evidence_sha256"].values():
        sha(value)
    need(
        set(case["evidence_sha256"]) == {"raw", "review", "wrapper", "pcap", "decoded"},
        "all raw/review/artifact bindings required",
    )


def validate_fixture(value):
    need(value["schema"] == 1 and type(value["schema"]) is int, "schema1 required")
    need(
        value["device"] == "Fireface UCX II"
        and value["serial"] == "24216011"
        and value["firmware"] == dict(usb_revision="3.01", dsp_version=36),
        "measured UCX2 required",
    )
    claims = value["claims"]
    need(
        claims
        == dict(
            warm_linked_physical_route=True,
            acoustic_safety=False,
            cold_completeness=False,
            atomic_transition=False,
            mono_routing=False,
            all_routes_exact=False,
            coefficient_device_readback=False,
            opaque_dsp_assessed=False,
        ),
        "bounded honest claims required",
    )
    need(all(type(flag) is bool for flag in claims.values()), "strict claim booleans required")
    backend = value["backend"]
    for key, field in (("binary_sha256", "current_backend_elf_sha256"),
                       ("series_sha256", "current_project_build_id"),
                       ("source_manifest_sha256", "current_source_manifest_sha256")):
        need(backend[key] == PORTABLE_SOURCE_AUTHORITY[field],
             "portable fixture must describe the same current C14 backend")
    need(
        backend["protocol"] == "ODK1"
        and backend["upstream"] == "f2fdd5ec78338848754aad32cc07f3440de63395",
        "backend protocol/source required",
    )
    for key in ("binary_sha256", "series_sha256", "source_manifest_sha256", "source_sha256"):
        sha(backend[key])
    for item in value["acceptance_sha256"].values():
        sha(item)
    need(
        set(value["acceptance_sha256"])
        == {
            "root_acceptance",
            "root_result",
            "root_post_result",
            "bench_receipt",
            "baseline_audit",
            "batch_audit",
            "source_proof",
            "outgoing_lifecycle_source_proof",
            "watch_inventory",
        },
        "complete acceptance binding required",
    )
    baseline = value["raw_baseline"]
    for key in (
        "full_device_windows",
        "full_assessed_raw_windows",
        "physical_mix_db_pan_pairs",
        "control_writes",
    ):
        need(type(baseline[key]) is int, "strict baseline counts required")
    need(
        baseline["metadata_registers"] == [0x200C, 0x200D]
        and baseline["db_deci_db"] == [-650, -650]
        and baseline["pan"] == [-100, 100]
        and baseline["full_device_windows"] == 2
        and baseline["full_assessed_raw_windows"] == 2
        and baseline["physical_mix_db_pan_pairs"] == 400
        and baseline["control_writes"] == 0,
        "own raw-pan-preserving baseline required",
    )
    for key in ("raw_sha256", "review_sha256", "pcap_sha256"):
        sha(baseline[key])
    volatile_watch(baseline["watch"])
    statusserial(baseline["statusserial"])
    input_eof_scope(baseline, baseline["pcap_sha256"], baseline["statusserial"], baseline=True,
                    receipt_sha=value["acceptance_sha256"]["bench_receipt"])
    need(len(value["cases"]) == 5, "exact five vectors required")
    for item, name, vector in zip(value["cases"], CASES, VECTORS, strict=True):
        validate_case(item, name, vector)
        input_eof_scope(item["usb"], item["evidence_sha256"]["pcap"],
                        item["usb"]["statusserial"], baseline=False,
                        receipt_sha=value["acceptance_sha256"]["bench_receipt"])
    encoded = json.dumps(value, allow_nan=False)
    need(
        all(
            word not in encoded
            for word in (
                "/home/",
                "build/0.9",
                "docs/plans",
                "synthetic",
                "candidate-receipt.json",
            )
        ),
        "private paths and synthetic evidence forbidden",
    )


def test_retained_input_route_pan_fixture_has_only_its_measured_scope():
    artifact = Path(__file__).parent / "data/input-route-pan-probes-ucx2.json"
    validate_fixture(json.loads(artifact.read_bytes()))
