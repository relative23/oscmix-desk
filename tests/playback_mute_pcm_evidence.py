"""Retained representative Playback-Mute DSP-meter evidence, without live I/O.

One warm PB1/2 pair is observed at output1/2 with a continuing owned source.
The fixture never qualifies acoustic output, other pairs, cold start or atomicity.
"""

import math

from playback_mute_evidence import (
    USB_SCOPE,
    need,
    sha,
    validate_boundaries,
    validate_outgoing,
    validate_outgoing_completion_scope,
)

STAGES = ("unmuted", "muted", "restored")
PATHS = tuple(
    sorted(
        "/%s/%d/level" % (kind, channel)
        for kind in ("input", "output", "playback")
        for channel in range(1, 21)
    )
)
VOLATILE_WATCH_PATHS = tuple(
    sorted(
        "/input/%d/%s/meter" % (channel, family)
        for channel in (1, 2)
        for family in ("dynamics", "autolevel")
    )
)
SELECTED = ("/output/1/level", "/output/2/level", "/playback/1/level", "/playback/2/level")
NO_CLAIMS = (
    "acoustic_output_claim",
    "all_playback_channels_effect_claim",
    "cold_start_qualified",
    "atomic_transition_claim",
    "device_mute_readback",
    "coefficient_device_readback",
)


def finite(value):
    need(type(value) in (int, float) and math.isfinite(value), "finite number required")
    return value


def meter_number(value):
    return -math.inf if value == "-inf" else finite(value)


def sha_map(values, *, required=()):
    need(type(values) is dict and set(required) <= set(values), "required SHA commitments absent")
    need(all(type(name) is str and name for name in values), "named SHA commitments required")
    for value in values.values():
        sha(value)


def quiet(value):
    need(
        type(value) is dict
        and set(value)
        == {
            "path_count",
            "minimum_distinct_deliveries",
            "peak_max_dbfs",
            "clip_max",
            "raw_sha256",
        },
        "quiet meter schema differs",
    )
    need(
        type(value["path_count"]) is int
        and value["path_count"] == 60
        and type(value["minimum_distinct_deliveries"]) is int
        and value["minimum_distinct_deliveries"] >= 3
        and meter_number(value["peak_max_dbfs"]) <= -40
        and type(value["clip_max"]) is int
        and value["clip_max"] == 0,
        "all sixty paths must be fresh, quiet and unclipped",
    )
    sha(value["raw_sha256"])


def stage(value, *, muted):
    adaptive_meter(value)
    need(
        type(value) is dict
        and set(value)
        == {
            "muted",
            "adaptive_meter",
            "boundary",
            "settle_seconds",
            "effect_receipt_first",
            "minimum_distinct_deliveries",
            "sequence_first",
            "sequence_last",
            "quiet_all60",
            "selected",
            "raw_sha256",
            "control_trace_sha256",
            "source_before_sha256",
            "source_after_sha256",
        },
        "DSP stage schema differs",
    )
    need(
        value["muted"] is muted
        and value["settle_seconds"] == 1.5
        and finite(value["effect_receipt_first"]) >= finite(value["boundary"]) + 1.5,
        "settled RUNNING/WRITE-ACK boundary required",
    )
    first, last = value["sequence_first"], value["sequence_last"]
    need(
        type(first) is int
        and type(last) is int
        and 0 < first <= last
        and type(value["minimum_distinct_deliveries"]) is int
        and value["minimum_distinct_deliveries"] >= 3,
        "fresh settled meter sequences required",
    )
    quiet(value["quiet_all60"])
    need(set(value["selected"]) == set(SELECTED), "representative four DSP paths required")
    for path, samples in value["selected"].items():
        playback = path.startswith("/playback/")
        need(
            type(samples) is list and len(samples) >= value["minimum_distinct_deliveries"],
            "actual deliveries must cover the reported settled minimum",
        )
        sequences = []
        for sample in samples:
            need(
                type(sample) is dict and set(sample) == {"sequence", "receipt", "tags", "args"},
                "retained sample schema differs",
            )
            sequence = sample["sequence"]
            need(
                type(sequence) is int
                and first <= sequence <= last
                and finite(sample["receipt"]) >= value["effect_receipt_first"],
                "settled sample receipt/order differs",
            )
            sequences.append(sequence)
            tags, args = sample["tags"], sample["args"]
            need(
                tags == ("ffi" if playback else "ffffi")
                and type(args) is list
                and len(args) == len(tags)
                and type(args[-1]) is int
                and args[-1] == 0,
                "DSP meter arity/types/clip differ",
            )
            numbers = [meter_number(number) for number in args[:-1]]
            need(max(numbers[::2]) <= -40, "representative output peak exceeds quiet bound")
            if playback:
                need(-63 <= numbers[0] <= -57, "continuing source must remain visible")
            elif muted:
                need(
                    numbers[0] <= -90 and numbers[1] <= -90,
                    "muted output peak/RMS must be suppressed while source continues",
                )
            else:
                need(-65 <= numbers[0] <= -55, "own unmuted output must carry source")
        need(sequences == sorted(set(sequences)), "distinct ordered meter deliveries required")
    for name in (
        "raw_sha256",
        "control_trace_sha256",
        "source_before_sha256",
        "source_after_sha256",
    ):
        sha(value[name])
    need(
        value["quiet_all60"]["raw_sha256"] == value["raw_sha256"],
        "stage quiet record commitment differs",
    )


def validate(value):
    need(
        type(value) is dict
        and set(value)
        == {
            "schema",
            "kind",
            "device",
            "serial",
            "accepted",
            "scope",
            "backend",
            "signal",
            "owned_source",
            "stages",
            "quiet_writes",
            "lifecycle",
            "usb",
            "source_sha256",
            "dependency_source_sha256",
            "evidence_sha256",
            "claims",
        },
        "representative PCM fixture schema differs",
    )
    need(
        type(value["schema"]) is int
        and value["schema"] in (1, 2)
        and value["kind"] == "playback-mute-pcm-effect-ucx2"
        and value["device"] == "Fireface UCX II"
        and value["serial"] == "24216011"
        and value["accepted"] is True,
        "actual accepted UCX II PCM effect required",
    )
    need(
        all(
            type(n) is int
            for key in ("playback_channels", "output_channels")
            for n in value["scope"][key]
        )
        and value["scope"]
        == dict(
            mode="warm",
            evidence="device-dsp-meters",
            playback_channels=[1, 2],
            output_channels=[1, 2],
            representative_only=True,
        ),
        "only one representative warm DSP pair is qualified",
    )
    need(value["claims"] == dict.fromkeys(NO_CLAIMS, False), "unsupported PCM safety/scope claim")
    backend = value["backend"]
    need(
        backend["protocol"] == "ODK1" and len(backend["upstream"]) == 40,
        "backend identity differs",
    )
    for name in ("binary_sha256", "bridge_sha256", "series_sha256", "source_manifest_sha256"):
        sha(backend[name])
    sha_map(backend["source_sha256"], required=("oscmix.c", "control.c", "device_ffucxii.c"))
    signal = value["signal"]
    need(
        set(signal)
        == {
            "sha256",
            "size_bytes",
            "sample_rate",
            "format",
            "channels",
            "signal_channels",
            "frequency_hz",
            "peak_dbfs",
            "duration_seconds",
            "dc_symmetric",
            "other_channels_zero",
        },
        "sealed stimulus schema differs",
    )
    need(
        all(
            type(signal[key]) is int and signal[key] == expected
            for key, expected in (
                ("size_bytes", 86400000),
                ("sample_rate", 48000),
                ("channels", 20),
                ("frequency_hz", 1000),
                ("duration_seconds", 30),
            )
        )
        and signal["format"] == "S24_3LE"
        and type(signal["signal_channels"]) is list
        and all(type(channel) is int for channel in signal["signal_channels"])
        and signal["signal_channels"] == [1, 2]
        and abs(finite(signal["peak_dbfs"]) + 60) <= 0.01
        and signal["dc_symmetric"] is True
        and signal["other_channels_zero"] is True,
        "sealed low-level 30-second PB1/2-only stimulus required",
    )
    sha(signal["sha256"])
    owner = value["owned_source"]
    need(
        set(owner)
        == {
            "pid",
            "starttime",
            "executable_sha256",
            "launched_at",
            "running_at",
            "deadline",
            "closed_at",
            "own_process_closed",
            "kernel_playback_closed",
            "capture_closed",
            "guard_claim_count",
            "kernel_receipts_sha256",
            "returncode",
            "command_replayed",
        },
        "owned source schema differs",
    )
    need(
        all(type(owner[key]) is int and owner[key] > 0 for key in ("pid", "starttime"))
        and owner["own_process_closed"] is True
        and owner["kernel_playback_closed"] is True
        and owner["capture_closed"] is True
        and type(owner["guard_claim_count"]) is int
        and owner["guard_claim_count"] >= 8
        and type(owner["returncode"]) is int
        and owner["command_replayed"] is False,
        "bounded own process/kernel guards required",
    )
    launched, running, deadline, closed = [
        finite(owner[key]) for key in ("launched_at", "running_at", "deadline", "closed_at")
    ]
    need(
        launched <= running < launched + 2
        and deadline == launched + 30
        and running <= closed < deadline,
        "owned source absolute startup/stop deadlines differ",
    )
    sha(owner["executable_sha256"])
    sha(owner["kernel_receipts_sha256"])
    need(set(value["stages"]) == set(STAGES), "all three actual DSP stages required")
    for name in STAGES:
        stage(value["stages"][name], muted=name == "muted")
    stages = value["stages"]
    need(
        stages["unmuted"]["boundary"] == running
        and stages["unmuted"]["effect_receipt_first"] < stages["muted"]["boundary"]
        and stages["muted"]["effect_receipt_first"] < stages["restored"]["boundary"]
        and stages["restored"]["effect_receipt_first"] <= closed,
        "three ordered stages must precede own PCM close",
    )
    need(
        type(value["quiet_writes"]) is list and len(value["quiet_writes"]) == 3,
        "all three writes need fresh all60 quiet evidence",
    )
    for item in value["quiet_writes"]:
        quiet(item)
    need(
        len({item["raw_sha256"] for item in value["quiet_writes"]}) == 3
        and value["quiet_writes"][0]["raw_sha256"] == stages["unmuted"]["raw_sha256"]
        and value["quiet_writes"][1]["raw_sha256"] == stages["muted"]["raw_sha256"],
        "each write needs its own fresh quiet evidence and the first two actual DSP stages",
    )
    life = value["lifecycle"]
    cleanup_heartbeat(life["close_heartbeat"],
        restored_finished=stages["restored"]["adaptive_meter"]["finished"], closed_at=closed)
    need(
        set(life)
        == {
            "root_restored",
            "close_heartbeat",
            "post_passed",
            "root_accepted",
            "independent_raw_reviewed",
            "bench_receipt_sha256",
            "writes",
            "begin_count",
            "end_count",
            "end_acknowledged",
            "end_sent_at",
            "third_write_sent_at",
            "watch_started_at",
            "end_ack_at",
            "watch_seconds",
            "final_device_paths",
            "full_cache_coefficients",
            "own_flags_restored",
            "writer_seconds",
            "writer_limit_seconds",
            "capture_seconds",
            "automatic_retry",
            "source_guarded_other_streams",
            "device_watch_scope",
            "peer_association_scope",
            "global_socket_owner_exclusivity_claim",
            "owned_unit_no_subgroups",
        },
        "lifecycle schema differs",
    )
    need(
        all(
            life[key] is True
            for key in (
                "root_restored",
                "post_passed",
                "root_accepted",
                "independent_raw_reviewed",
                "end_acknowledged",
                "own_flags_restored",
            )
        )
        and type(life["begin_count"]) is int
        and life["begin_count"] == 1
        and type(life["end_count"]) is int
        and life["end_count"] == 1
        and life["final_device_paths"] == 2232
        and life["full_cache_coefficients"] == 800
        and life["source_guarded_other_streams"] is True
        and life["automatic_retry"] is False
        and life["writer_limit_seconds"] == 87
        and 0 < finite(life["writer_seconds"]) < 87
        and life["capture_seconds"] == 60,
        "complete restoration and sole BEGIN/END evidence required",
    )
    need(
        life["peer_association_scope"] == "owned-unit-and-current-kernel-backend-family"
        and life["global_socket_owner_exclusivity_claim"] is False
        and life["owned_unit_no_subgroups"] is True,
        "scoped peer ownership claim differs",
    )
    need(
        life["writes"]
        == [
            ["/playback/1/mute", "i", [1]],
            ["/playback/1/mute", "i", [0]],
            ["/playback/2/mute", "i", [0]],
        ],
        "sealed three-write sequence differs",
    )
    need(
        closed
        <= finite(life["third_write_sent_at"])
        < finite(life["end_sent_at"])
        <= finite(life["end_ack_at"])
        <= finite(life["watch_started_at"])
        <= life["end_ack_at"] + 0.1
        and finite(life["watch_seconds"]) >= 24,
        "close/sole-END/immediate24secondwatch differs",
    )
    sha(life["bench_receipt_sha256"])
    watch = life["device_watch_scope"]
    need(
        set(watch)
        == {
            "static_device_paths",
            "volatile_device_paths",
            "volatile_device_messages",
            "raw_control_frames",
            "seconds",
            "inventory_qualified",
            "device_mute_readback",
            "coefficient_device_readback",
        }
        and type(watch["static_device_paths"]) is int
        and watch["static_device_paths"] == 0
        and set(watch["volatile_device_paths"]) == set(VOLATILE_WATCH_PATHS)
        and all(
            type(count) is int and count >= 0 for count in watch["volatile_device_paths"].values()
        )
        and type(watch["volatile_device_messages"]) is int
        and watch["volatile_device_messages"] == sum(watch["volatile_device_paths"].values())
        and type(watch["raw_control_frames"]) is int
        and watch["raw_control_frames"] >= 0
        and finite(watch["seconds"]) == life["watch_seconds"]
        and watch["seconds"] >= 24
        and watch["inventory_qualified"] is True
        and watch["device_mute_readback"] is False
        and watch["coefficient_device_readback"] is False,
        "24second nonvolatile DEVICE watch scope and actual four-path counts required",
    )
    usb = value["usb"]
    need(
        set(usb)
        == {
            "file_sha256",
            "capture_boundaries",
            "capture_packet_count",
            "scope",
            "outgoing_assessment",
        }
        and usb["scope"] == USB_SCOPE,
        "qualified USB scope differs",
    )
    sha_map(
        usb["file_sha256"],
        required=("capture.pcap", "decoded.json", "manifest.json", "binding.json"),
    )
    validate_boundaries(usb["capture_boundaries"], usb["capture_packet_count"])
    validate_outgoing(usb["outgoing_assessment"])
    if value["schema"] == 2:
        need(
            "outgoing_completion_scope" in usb["outgoing_assessment"]
            and "unconfirmed_autonomous_eof_tail" in usb["outgoing_assessment"],
            "actual EOF fixture requires explicit completion scope and tail disclosure",
        )
        validate_outgoing_completion_scope(usb["outgoing_assessment"])
        sha(value["evidence_sha256"]["binding_record"])
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
                'owned_pcm.py',
                'ownership-assumptions.json',
                'ownership-assumptions.py',
                'pb_review.py',
                'pb_session.py',
                'pcm_signal.py',
                'pilot.py',
                'qualify.py',
                'review.py',
                'run.py',
                'scope.py',
                'test_autonomous_eof_tail.py',
                'test_cache_adapter.py',
                'test_native_pan_repeat.py',
                'test_pcm.py',
                'test_pcm_integration.py',
                'test_pilot.py',
            },
            "exact portable runtime seal excludes private binding metadata",
        )
    sha_map(
        value["source_sha256"],
        required=(
            "review.py",
            "pb_review.py",
            "pcm_signal.py",
            "owned_pcm.py",
            "meter_review.py",
            "pilot.py",
            "adapters.py",
            "bindings.py",
            "inputs.json",
        ),
    )
    need(
        set(value["dependency_source_sha256"]) == {"usb", "cache", "readonly_helper", "projector"},
        "all independent source commitments required",
    )
    need(
        set(value["dependency_source_sha256"]["cache"])
        == {"inspect-cache.py", "review.py", "layout-proof.json"},
        "qualified cache source inventory differs",
    )
    for sources in value["dependency_source_sha256"].values():
        sha_map(sources)
    sha_map(
        value["evidence_sha256"],
        required=(
            "root_result",
            "root_post",
            "root_acceptance",
            "batch_result",
            "batch_review",
            "bench_receipt",
            "raw.json",
            "review.json",
            "result.json",
            "inputs",
            "wave",
            "pcm_executable",
            "pcm_template_handoff",
            "readonly_helper_handoff",
            "watch_inventory",
        ),
    )
    need(
        value["evidence_sha256"]["bench_receipt"] == life["bench_receipt_sha256"]
        and value["evidence_sha256"]["wave"] == signal["sha256"]
        and value["evidence_sha256"]["pcm_executable"] == owner["executable_sha256"],
        "own receipt/wave/player associations differ",
    )
    return value


def adaptive_meter(value):
    """Rebuild every retained early/late path and the mandatory adaptive deadline."""
    record = value["adaptive_meter"]
    context = record["settling_context"]
    boundary = finite(value["boundary"])
    need(type(context) is dict and set(context) == {
        "schema", "boundary", "settle_seconds", "tail_seconds", "minimum_read_seconds",
        "target_end", "read_deadline", "subscription_started", "total_limit",
        "all_early_events_retained", "all_early_and_late_quiet_peaks_validated",
        "settled_delivery_counts",
    }, "exact mandatory adaptive context required")
    start, first, last, finish, duration, total = [finite(record[key]) for key in (
        "started", "read_started", "receipt_finished", "finished", "read_seconds", "total_seconds"
    )]
    expected_end = max(first + .8, boundary + 1.5 + .4)
    need(type(context["schema"]) is int and context["schema"] == 1
         and finite(context["boundary"]) == boundary and context["settle_seconds"] == 1.5
         and context["tail_seconds"] == .4 and context["minimum_read_seconds"] == .8
         and context["total_limit"] == 3.8 and context["subscription_started"] == start
         and abs(finite(context["target_end"]) - (boundary + 1.5 + .4)) <= 1e-9
         and abs(finite(context["read_deadline"]) - expected_end) <= 1e-9
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
        path, seq, receipt = event["path"], event["sequence"], finite(event["receipt"])
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
        values = [meter_number(n) for n in args[:-1]]
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
         and meter_number(quiet_view["peak_max_dbfs"]) == max(peaks)
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
    start, finish, ack, sent, prior, following, checked = [finite(value[key]) for key in (
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
         and finite(restored_finished) <= checked <= start <= sent <= ack <= finish
         <= finite(closed_at)
         and finish - start <= .55 + 1e-9 and finite(closed_at) - finish <= 2 + 1e-9
         and 0 <= ack - prior < 5 and 0 <= following - sent < 5,
         "same-lease cleanup ACK, close bounds or conservative local lease gap differs")
    sha(value["control_trace_sha256"])
