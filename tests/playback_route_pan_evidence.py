"""Portable retained Playback-balance evidence; never opens hardware."""

import hashlib
import math
import re
import statistics
import struct

from playback_mute_evidence import validate_outgoing_completion_scope

CASES = ("minus100", "plus100", "plus50")
PANS = (-100, 100, 50)
STAGES = ("baseline", "probed", "restored")
SELECTED = ("/playback/1/level", "/playback/2/level", "/output/1/level", "/output/2/level")
BITS = {
    -100: ["0000803f", "00000000", "00000000", "00000000"],
    100: ["00000000", "00000000", "00000000", "0000803f"],
    50: ["0000003f", "00000000", "00000000", "0000803f"],
}
UNITY = ["0000803f", "00000000", "00000000", "0000803f"]
NO_CLAIMS = (
    "acoustic_output",
    "all_playback_routes",
    "cold_start",
    "atomic_transition",
    "coefficient_device_readback",
    "playback_pan_device_readback",
    "opaque_dsp_assessed",
)


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(value):
    need(
        type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        "strict actual SHA256 required",
    )


def sha_map(values, required=()):
    need(type(values) is dict and set(required) <= set(values), "required SHA bindings absent")
    for name, value in values.items():
        need(
            type(name) is str
            and name
            and not name.startswith("/")
            and ".." not in name.split("/"),
            "safe named SHA bindings required",
        )
        sha(value)


def number(value):
    need(type(value) in (int, float) and math.isfinite(value), "finite plain number required")
    return value


def meter(value):
    return -math.inf if value == "-inf" else number(value)


def quiet(value):
    need(
        type(value["path_count"]) is int
        and value["path_count"] == 60
        and type(value["minimum_distinct_deliveries"]) is int
        and value["minimum_distinct_deliveries"] >= 3
        and meter(value["peak_max_dbfs"]) <= -40
        and type(value["clip_max"]) is int
        and value["clip_max"] == 0,
        "all60 paths need distinct fresh quiet unclipped meters",
    )
    sha(value["raw_sha256"])
    first = number(value["delivery_first_at"])
    last = number(value["delivery_last_at"])
    oldest = number(value["oldest_path_last_receipt_at"])
    need(
        first <= oldest <= last and value["receipt_time_is_device_time"] is False,
        "actual local METERS receipt interval required",
    )


def dsp_stage(value, pan, baseline=None):
    adaptive_meter(value)
    need(type(pan) is int and pan in (0, *PANS), "sealed DSP balance only")
    need(
        value["pan"] == pan
        and type(value["pan"]) is int
        and value["settle_seconds"] == 1.5
        and number(value["effect_receipt_first"]) >= number(value["boundary"]) + 1.5,
        "settled RUNNING/WRITE-ACK meter boundary required",
    )
    first, last = value["sequence_first"], value["sequence_last"]
    need(
        type(first) is int
        and type(last) is int
        and 0 < first <= last
        and type(value["minimum_distinct_deliveries"]) is int
        and value["minimum_distinct_deliveries"] >= 3,
        "fresh DSP sequences required",
    )
    quiet(value["quiet_all60"])
    reference = value["output_reference_dbfs"]
    need(
        type(reference) is list
        and len(reference) == 2
        and all(-65 <= number(peak) <= -55 for peak in reference),
        "actual own unity output reference required",
    )
    if baseline is not None:
        need(reference == baseline["output_reference_dbfs"], "foreign unity baseline reference")
    need(set(value["selected"]) == set(SELECTED), "exact representative four DSP paths")
    for path, samples in value["selected"].items():
        need(
            type(samples) is list and len(samples) >= value["minimum_distinct_deliveries"],
            "settled meter deliveries missing",
        )
        sequences = []
        playback = path.startswith("/playback/")
        for sample in samples:
            sequence = sample["sequence"]
            need(
                type(sequence) is int
                and first <= sequence <= last
                and number(sample["receipt"]) >= value["effect_receipt_first"],
                "fresh settled meter sample required",
            )
            sequences.append(sequence)
            tags, args = sample["tags"], sample["args"]
            need(
                tags == ("ffi" if playback else "ffffi")
                and type(args) is list
                and len(args) == len(tags)
                and type(args[-1]) is int
                and args[-1] == 0,
                "actual meter tags/arity/clip required",
            )
            values = list(map(meter, args[:-1]))
            need(max(values[::2]) <= -40, "quiet bound exceeded")
            if playback:
                need(-63 <= values[0] <= -57, "owned source must continue in every DSP stage")
            else:
                left = path == "/output/1/level"
                suppressed = (pan == 100 and left) or (pan == -100 and not left)
                if suppressed:
                    need(
                        values[0] <= -90 and values[1] <= -90,
                        "endpoint must suppress selected output peak/RMS",
                    )
                elif baseline is None:
                    need(
                        pan == 0 and -65 <= values[0] <= -55,
                        "actual own unity DSP baseline required",
                    )
                else:
                    expected = reference[0 if left else 1]
                    if pan == 50 and left:
                        expected -= 6.020599913279624
                    need(
                        expected - 2 <= values[0] <= expected + 2,
                        "output effect must match its own actual unity reference",
                    )
        need(sequences == sorted(set(sequences)), "ordered distinct DSP deliveries required")
    if pan == 50:
        left, right = (
            {sample["sequence"]: meter(sample["args"][0]) for sample in value["selected"][path]}
            for path in ("/output/1/level", "/output/2/level")
        )
        common = set(left).intersection(right)
        need(
            len(common) >= 3 and all(3 <= right[key] - left[key] <= 9 for key in common),
            "paired output meters must show half-balance attenuation",
        )
    if baseline is None:
        observed = [
            statistics.median(meter(sample["args"][0]) for sample in value["selected"][p])
            for p in ("/output/1/level", "/output/2/level")
        ]
        need(reference == observed, "baseline references must equal actual retained meter medians")
    for key in (
        "raw_sha256",
        "control_trace_sha256",
        "source_before_sha256",
        "source_after_sha256",
    ):
        sha(value[key])
    need(value["quiet_all60"]["raw_sha256"] == value["raw_sha256"], "quiet stage SHA differs")


def register_word(reg, value):
    base = reg << 16 | value
    return base | ((bin(base).count("1") % 2 ^ 1) << 31)


def coefficient_code(bits):
    value = struct.unpack("<f", bytes.fromhex(bits))[0]
    code = math.floor(value * 0x8000 + 0.5)
    return ((code >> 3) - 0x8000 if code > 0x4000 else code) & 0xFFFF


def bulk(value, bits):
    regs = (0x4021, 0x4061, 0x4020, 0x4060)
    ordered = (bits[1], bits[3], bits[0], bits[2])
    expected = [
        f"{register_word(reg, coefficient_code(bit)):08x}"
        for reg, bit in zip(regs, ordered)
    ]
    need(
        type(value["frame_bytes"]) is int
        and value["frame_bytes"] == 27
        and value["register_words_hex"] == expected
        and type(value["successful_completions"]) is int
        and value["successful_completions"] >= 1
        and value["playback_metadata_words"] == 0
        and type(value["playback_metadata_words"]) is int,
        "exact4coefficient/27B completed no-metadata bulk required",
    )


def cache(value, pan):
    need(
        value["baseline_bits"] == value["restoration_bits"] == UNITY
        and value["probe_bits"] == BITS[pan],
        "own unity/probe/restored coefficient bits required",
    )
    need(
        value["matrix_coefficients"] == 800
        and type(value["matrix_coefficients"]) is int
        and value["independent_copies"] == 2
        and type(value["independent_copies"]) is int
        and value["other_coefficients"] == 796
        and type(value["other_coefficients"]) is int
        and value["other796_preserved"] is True
        and value["all20_playback_flags_preserved"] is True
        and value["all_other_span_bytes_preserved"] is True
        and value["mix_observed_pointees_assessed"] is False,
        "whole800/two-copy strict own-cache evidence required",
    )
    need(
        value["pair_flags"]
        == dict(stereo=[1, 1], mute=[0, 0], width=[100, 100], output_stereo=[1, 1]),
        "actual linked/unmuted/width100 required",
    )
    for numbers in value["pair_flags"].values():
        need(all(type(n) is int for n in numbers), "strict cached pair flag integers required")
    sha_map(value["phase_sha256"], ("baseline", "probe", "restoration"))
    for name, expected in (("baseline", 1), ("probe", 0), ("restoration", 0)):
        flags = value["native_freshness"][name]
        need(
            set(flags) == {"inputs", "outputs"}
            and all(
                numbers == [expected] * 20 and all(type(n) is int for n in numbers)
                for numbers in flags.values()
            ),
            "exact source-proven40ephemeral native flags required",
        )


def source(value):
    need(
        all(
            type(value[key]) is int and value[key] == expected
            for key, expected in (
                ("size_bytes", 115200000),
                ("sample_rate", 48000),
                ("channels", 20),
                ("duration_seconds", 40),
                ("frequency_hz", 1000),
            )
        )
        and value["format"] == "S24_3LE"
        and value["signal_channels"] == [1, 2]
        and all(type(n) is int for n in value["signal_channels"])
        and abs(number(value["peak_dbfs"]) + 60) <= 0.01
        and value["dc_symmetric"] is True
        and value["other_channels_zero"] is True,
        "exact40s115200000B quiet owned20ch signal required",
    )
    sha(value["sha256"])


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
        "exact independent source guard summary required",
    )
    for field in (
        "static_sources_qualified_outside_METER_callbacks",
        "static_immediately_before_each_WRITE_and_REFRESH",
        "meter_receipt_age_rechecked_after_static",
    ):
        need(value[field] is True, "static checks and after-static WRITE freshness required")
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
        need(number(value[field]) == expected, "unchanged guard and submission limits required")


def case(value, case_id, pan, *, completion_scope_required=False):
    need(
        value["id"] == case_id
        and type(value["requested_pan"]) is int
        and value["requested_pan"] == pan
        and value["requested_db"] == 0.0
        and type(value["requested_db"]) is float
        and value["pair"] == [1, 2]
        and all(type(n) is int for n in value["pair"]),
        "only three sealed own PB1/2 balance cases",
    )
    source(value["signal"])
    need(set(value["stages"]) == set(STAGES), "all3DSP stages required")
    for label in STAGES:
        dsp_stage(
            value["stages"][label],
            pan if label == "probed" else 0,
            None if label == "baseline" else value["stages"]["baseline"],
        )
    owner = value["owned_source"]
    need(
        type(owner["pid"]) is int
        and owner["pid"] > 0
        and type(owner["starttime"]) is int
        and owner["starttime"] > 0
        and owner["own_process_closed"] is True
        and owner["kernel_playback_closed"] is True
        and owner["capture_closed"] is True
        and owner["command_replayed"] is False,
        "only owned bounded PCM process and quiet kernel closure",
    )
    launched, running, deadline, closed = [
        number(owner[key]) for key in ("launched_at", "running_at", "deadline", "closed_at")
    ]
    need(
        launched <= running < launched + 2
        and deadline == launched + 40
        and running <= closed < deadline,
        "own absolute40s source deadline required",
    )
    sha(owner["executable_sha256"])
    sha(owner["kernel_receipts_sha256"])
    stages = value["stages"]
    need(
        stages["baseline"]["boundary"] == running
        and stages["baseline"]["effect_receipt_first"] < stages["probed"]["boundary"]
        and stages["probed"]["effect_receipt_first"] < stages["restored"]["boundary"]
        and stages["restored"]["effect_receipt_first"] <= closed,
        "source must continue across3ordered DSP stages",
    )
    cache(value["cache"], pan)
    source_guards(value["source_guards"])
    sha(value["guard_journal_sha256"])
    writes = value["writes"]
    need(len(writes) == 2, "two own route writes required")
    for row, phase, target_pan in zip(writes, ("probe", "restore-target"), (pan, 0)):
        need(
            row["phase"] == phase
            and row["path"] == "/mix/1/playback/1"
            and row["tags"] == "fi"
            and row["args"] == [0.0, target_pan]
            and type(row["args"][0]) is float
            and type(row["args"][1]) is int
            and row["submitted"] is True
            and row["acknowledged"] is True,
            "exact submitted/ACKed own probe+pan0 restoration",
        )
        quiet(row["quiet"])
        guard = number(row["submission_receipt_check_at"])
        reserve = number(row["send_time_reserved_seconds"])
        need(
            reserve == 0.5
            and row["quiet"]["delivery_last_at"] <= guard <= number(row["ack_at"])
            and 0 <= guard + reserve - row["quiet"]["oldest_path_last_receipt_at"] <= 1.0,
            "all60 last receipts must remain fresh after static check plus socket submission",
        )
    need(
        writes[0]["quiet"]["raw_sha256"] != writes[1]["quiet"]["raw_sha256"],
        "each WRITE needs a different fresh meter window",
    )
    wire = value["usb"]
    need(len(wire["bulks"]) == 2, "exact2sealed route bulks required")
    bulk(wire["bulks"][0], BITS[pan])
    bulk(wire["bulks"][1], UNITY)
    need(
        wire["coefficient_device_readback"] is False
        and wire["playback_pan_device_readback"] is False
        and wire["full_stream_completeness_claim"] is False
        and wire["complete_messages_only_used_as_register_evidence"] is True
        and wire["capture_boundary_feedback_claim"] is False
        and wire["all_outgoing_registers_assessed"] is True,
        "honest setter/OUT scope required",
    )
    for item in wire["capture_boundaries"]:
        boundary(item)
    sha(wire["assessed_outgoing_sha256"])
    if completion_scope_required:
        assessment = wire.get("outgoing_assessment")
        need(type(assessment) is dict and set(assessment) == {
            "message_counts", "status_serial_final", "outgoing_completion_scope",
            "unconfirmed_autonomous_eof_tail",
        }, "EOF case requires explicit complete transfer scope and tail disclosure")
        counts = assessment["message_counts"]
        need(type(counts) is dict and set(counts) == {
            "autonomous_statusserial", "autonomous_meter_requests", "own_final_refresh",
            "unchanged_native_zero_corrections",
        } and all(type(count) is int and count >= 0 for count in counts.values())
             and counts["autonomous_statusserial"] > 0
             and type(assessment["status_serial_final"]) is int
             and 0 <= assessment["status_serial_final"] < 16,
             "complete autonomous message counts and final status required")
        need(assessment["outgoing_completion_scope"] is not None,
             "EOF completion scope must be explicit even with no tail")
        validate_outgoing_completion_scope(assessment)
    life = value["lifecycle"]
    cleanup_heartbeat(life["close_heartbeat"],
        restored_finished=stages["restored"]["adaptive_meter"]["finished"], closed_at=closed)
    need(
        life["begin_count"] == life["end_count"] == 1
        and type(life["begin_count"]) is int
        and type(life["end_count"]) is int
        and life["end_acknowledged"] is True
        and life["full_device_restored"] is True
        and life["full_cache_restored"] is True
        and life["final_device_paths"] == 2232
        and type(life["final_device_paths"]) is int
        and life["automatic_retry"] is False
        and closed
        <= number(life["end_sent_at"])
        <= number(life["end_ack_at"])
        <= number(life["watch_started_at"])
        <= life["end_ack_at"] + 0.1
        and number(life["watch_seconds"]) >= 24,
        "own whole restoration/soleEND/immediate nonvolatile24s watch required",
    )
    watch = life["device_watch_scope"]
    paths = {
        "/input/%d/%s/meter" % (channel, family)
        for channel in (1, 2)
        for family in ("dynamics", "autolevel")
    }
    need(
        type(watch["static_device_paths"]) is int
        and watch["static_device_paths"] == 0
        and set(watch["volatile_device_paths"]) == paths
        and all(type(n) is int and n >= 0 for n in watch["volatile_device_paths"].values())
        and type(watch["volatile_device_messages"]) is int
        and watch["volatile_device_messages"] == sum(watch["volatile_device_paths"].values())
        and type(watch["raw_control_frames"]) is int
        and watch["raw_control_frames"] >= 0
        and watch["seconds"] == life["watch_seconds"]
        and watch["inventory_qualified"] is True
        and watch["playback_pan_device_readback"] is False
        and watch["coefficient_device_readback"] is False,
        "zero nonvolatile watch with retained four qualified volatile paths",
    )
    sha_map(value["evidence_sha256"], ("raw", "review", "wrapper", "pcap", "decoded"))


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


def public_only(value):
    """The durable fixture contains OSC paths, IDs and hashes, never private paths."""
    if type(value) is dict:
        for key, part in value.items():
            need(type(key) is str, "public JSON string keys required")
            need(
                "synthetic" not in key
                and key not in ("private_plan", "private_review", "private_path"),
                "private model/review fields cannot become hardware evidence",
            )
            public_only(key)
            public_only(part)
    elif type(value) is list:
        for part in value:
            public_only(part)
    elif type(value) is str:
        need(
            not any(
                token in value
                for token in ("/home/", "/var/tmp/", "/tmp/", "build/0.9.0", "docs/plans/")  # noqa: S108 -- these are forbidden public evidence path prefixes.
            ),
            "private filesystem paths cannot become public evidence",
        )


def validate(value):
    public_only(value)
    need(
        type(value["schema"]) is int
        and value["schema"] in (1, 2)
        and value["kind"] == "playback-route-pan-representative-pcm-ucx2"
        and value["device"] == "Fireface UCX II"
        and value["serial"] == "24216011"
        and value["accepted"] is True
        and value["mode"] == "warm"
        and value["device_meter_dsp_effect_confirmed"] is True
        and value["claims"] == dict.fromkeys(NO_CLAIMS, False),
        "accepted warm representative DSP effect with bounded honest claims",
    )
    need(
        all(type(flag) is bool for flag in value["claims"].values()),
        "strict bounded claim booleans required",
    )
    need(len(value["cases"]) == 3, "all3sealed representative cases required")
    for row, name, pan in zip(value["cases"], CASES, PANS):
        case(row, name, pan, completion_scope_required=value["schema"] == 2)
    sha_map(
        value["acceptance_sha256"],
        (
            "root_acceptance",
            "root_result",
            "root_post",
            "bench_receipt",
            "batch_review",
            "template_handoff",
        ),
    )
    if value["schema"] == 2:
        sha_map(value["acceptance_sha256"], ("binding_record", "pb_projector_handoff"))
        need(
            set(value["source_sha256"])
            == {
                'adapters.py',
                'bindings.py',
                'control.py',
                'inputs.json',
                'integration-notes.md',
                'meter_review.py',
                'offline-review.py',
                'origin.json',
                'owned_pcm.py',
                'ownership-assumptions.json',
                'ownership-assumptions.py',
                'pb_review.py',
                'pb_session.py',
                'pcm_signal.py',
                'pilot.py',
                'qualify-c.py',
                'qualify.py',
                'review.py',
                'route_pan.c',
                'run.py',
                'scope.py',
                'test_autonomous_eof_tail.py',
                'test_control.py',
                'test_engine.py',
                'test_native_pan_repeat.py',
                'test_owner.py',
                'test_pan.py',
                'test_review.py',
            },
            "exact portable runtime seal excludes private binding metadata",
        )
    need(
        value["backend"]["protocol"] == "ODK1"
        and value["backend"]["upstream"] == "f2fdd5ec78338848754aad32cc07f3440de63395",
        "actual known backend/source protocol required",
    )
    sha_map(value["backend"]["source_sha256"], ("oscmix.c", "control.c", "device_ffucxii.c"))
    for name in ("binary_sha256", "bridge_sha256", "series_sha256", "source_manifest_sha256"):
        sha(value["backend"][name])
    sha_map(value["source_sha256"], ("review.py", "pilot.py", "meter_review.py", "owned_pcm.py"))
    return value


def adaptive_meter(value):
    """Rebuild every retained early/late path and the mandatory adaptive deadline."""
    record = value["adaptive_meter"]
    context = record["settling_context"]
    boundary = number(value["boundary"])
    need(type(context) is dict and set(context) == {
        "schema", "boundary", "settle_seconds", "tail_seconds", "minimum_read_seconds",
        "target_end", "read_deadline", "subscription_started", "total_limit",
        "all_early_events_retained", "all_early_and_late_quiet_peaks_validated",
        "settled_delivery_counts",
    }, "exact mandatory adaptive context required")
    start, first, last, finish, duration, total = [number(record[key]) for key in (
        "started", "read_started", "receipt_finished", "finished", "read_seconds", "total_seconds"
    )]
    expected_end = max(first + .8, boundary + 1.5 + .4)
    need(type(context["schema"]) is int and context["schema"] == 1
         and number(context["boundary"]) == boundary and context["settle_seconds"] == 1.5
         and context["tail_seconds"] == .4 and context["minimum_read_seconds"] == .8
         and context["total_limit"] == 3.8 and context["subscription_started"] == start
         and abs(number(context["target_end"]) - (boundary + 1.5 + .4)) <= 1e-9
         and abs(number(context["read_deadline"]) - expected_end) <= 1e-9
         and boundary <= start <= first <= expected_end <= last <= finish
         and last <= expected_end + .05 + 1e-9
         and abs(duration - (last - first)) <= 1e-9
         and abs(total - (finish - start)) <= 1e-9 and total <= 3.8 + 1e-9
         and record["passed"] is True and record["reader_closed"] is True
         and record["new_connection"] is True
         and context["all_early_events_retained"] is True
         and context["all_early_and_late_quiet_peaks_validated"] is True,
         "adaptive source-relative receipt timing or unchanged cap differs")
    paths = {"/%s/%d/level" % (kind, channel)
             for kind in ("input", "output", "playback") for channel in range(1, 21)}
    counts, previous = {path: set() for path in paths}, {}
    all_counts = {path: set() for path in paths}
    selected = {path: [] for path in SELECTED}
    peaks, clips = [], []
    need(type(record["events"]) is list and record["events"], "retained adaptive events required")
    for event in record["events"]:
        need(type(event) is dict and set(event) == {
            "path", "sequence", "receipt", "origin", "tags", "args"
        }, "exact portable adaptive event fields required")
        path, seq, receipt = event["path"], event["sequence"], number(event["receipt"])
        need(path in paths and type(seq) is int and seq > previous.get(path, 0)
             and event["origin"] == "METERS" and first <= receipt <= last,
             "every early/late receipt must retain actual path/order/origin")
        previous[path] = seq
        all_counts[path].add(seq)
        playback = path.startswith("/playback/")
        tags, args = event["tags"], event["args"]
        need(tags == ("ffi" if playback else "ffffi")
             and type(args) is list and len(args) == len(tags)
             and type(args[-1]) is int and args[-1] == 0,
             "every early/late meter requires exact tags/arity/zero clip")
        values = [meter(n) for n in args[:-1]]
        peak = max(values[::2])
        need(peak <= -40, "an unsafe early or late peak cannot be discarded")
        peaks.append(peak)
        clips.append(args[-1])
        if receipt >= boundary + 1.5:
            counts[path].add(seq)
            if path in selected:
                selected[path].append({key: event[key] for key in (
                    "sequence", "receipt", "tags", "args"
                )})
    actual = {path: len(sequences) for path, sequences in counts.items()}
    claimed = context["settled_delivery_counts"]
    need(type(claimed) is dict and set(claimed) == paths
         and all(type(n) is int for n in claimed.values())
         and claimed == actual and min(actual.values()) >= 3,
         "all sixty late paths require three independently counted fresh deliveries")
    need(value["selected"] == selected
         and value["minimum_distinct_deliveries"] == min(actual.values())
         and value["sequence_first"] == min(min(rows) for rows in counts.values())
         and value["sequence_last"] == max(max(rows) for rows in counts.values())
         and value["effect_receipt_first"] == min(
             row["receipt"] for row in record["events"]
             if row["receipt"] >= boundary + 1.5),
         "selected effects must contain every late delivery without early substitution")
    quiet_view = value["quiet_all60"]
    need(quiet_view["minimum_distinct_deliveries"] == min(map(len, all_counts.values()))
         and meter(quiet_view["peak_max_dbfs"]) == max(peaks)
         and quiet_view["clip_max"] == max(clips),
         "all-sixty quiet reduction must include every early and late observation")


def cleanup_heartbeat(value, *, restored_finished, closed_at):
    """A local lease witness includes send-to-next-ACK latency before and after close."""
    need(type(value) is dict and set(value) == {
        "schema", "started_at", "finished_at", "ack_at", "acknowledged", "lease",
        "send_at", "token", "ack_lease", "ack_code", "previous_send_at",
        "previous_token", "previous_ack_lease", "following_ack_at", "following_token",
        "following_ack_lease", "following_kind", "following_ack_code",
        "source_after_checked_at", "control_trace_sha256",
    }, "exact actual cleanup KEEPALIVE witness required")
    start, finish, ack, sent, prior, following, checked = [number(value[key]) for key in (
        "started_at", "finished_at", "ack_at", "send_at", "previous_send_at",
        "following_ack_at", "source_after_checked_at"
    )]
    lease = value["lease"]
    need(type(value["schema"]) is int and value["schema"] == 1
         and type(lease) is int and lease > 0 and value["acknowledged"] is True
         and all(type(value[key]) is int and value[key] == lease for key in (
             "token", "ack_lease", "previous_token", "previous_ack_lease", "following_token"
         )) and type(value["ack_code"]) is int and value["ack_code"] == 0
         and type(value["following_ack_code"]) is int and value["following_ack_code"] == 0
         and type(value["following_kind"]) is int and value["following_kind"] in (3, 4)
         and type(value["following_ack_lease"]) is int
         and value["following_ack_lease"] == lease + (value["following_kind"] == 3)
         and number(restored_finished) <= checked <= start <= sent <= ack <= finish
         <= number(closed_at)
         and finish - start <= .55 + 1e-9 and number(closed_at) - finish <= 2 + 1e-9
         and 0 <= ack - prior < 5 and 0 <= following - sent < 5,
         "same-lease cleanup ACK, close bounds or conservative local lease gap differs")
    sha(value["control_trace_sha256"])
