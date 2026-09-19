"""Which interface a desk is for: the serial, the sequencer client and
the card list, from one resolution that never guesses (ADR 0024).
"""

import threading

import pytest
from support import fake_proc, free_udp_port, write_config
from two_boxes import DESK, A, B, add_clients

from oscmix_desk import profiles
from oscmix_desk.discovery import (
    Device,
    resolve_device,
    select_seq_client,
    serial_in,
)
from oscmix_desk.errors import DeviceAmbiguous
from oscmix_desk.process import port_holder


def test_the_serial_is_read_from_a_device_name():
    assert serial_in("Fireface UCX II (24216011)") == "24216011"
    assert serial_in("Fireface UCX II") is None
    assert serial_in("Fireface UCX II (123)") is None

def test_the_client_is_selected_by_serial_and_never_guessed():
    text = ('Client  24 : "Fireface UCX II (24216011)" [Kernel Legacy]\n'
            'Client  28 : "Fireface UCX II (99887766)" [Kernel Legacy]\n')
    assert select_seq_client(text, "Fireface UCX II", B[1]) == B[0]
    assert select_seq_client(text, "Fireface UCX II", "11111111") is None
    assert select_seq_client(text[:text.index("Client  28")],
                             "Fireface UCX II") == A[0]
    with pytest.raises(DeviceAmbiguous) as raised:
        select_seq_client(text, "Fireface UCX II")
    assert ("Fireface UCX II (24216011) (client 24), "
            "Fireface UCX II (99887766) (client 28)") in str(raised.value)

def test_the_card_list_decides_when_no_client_is_up(tmp_path):
    proc = fake_proc(tmp_path, boxes=[A, B])
    (proc / "asound" / "seq" / "clients").write_text("")
    with pytest.raises(DeviceAmbiguous) as raised:
        resolve_device("2a39:3fd9", "Fireface UCX II", "", proc)
    assert "24216011, 99887766" in str(raised.value)
    assert resolve_device("2a39:3fd9", "Fireface UCX II", B[1], proc) == \
        Device(usb_id="2a39:3fd9", serial=B[1], client=None)

    one = fake_proc(tmp_path / "one", boxes=[A])
    (one / "asound" / "seq" / "clients").write_text("")
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", one).serial == A[1]
    assert Device("2a39:3fd9", "", None).key == "2a39-3fd9-unknown"

def test_a_known_client_without_a_readable_client_list_has_no_serial(tmp_path):
    port = free_udp_port()
    proc = fake_proc(tmp_path, boxes=[B], bound=[(port, "oscmix", B[0])])
    (proc / "asound" / "seq" / "clients").unlink()
    holder = port_holder(port, proc)
    assert (holder.client, holder.serial) == (B[0], None)

def test_a_fireface_of_another_model_is_not_a_second_candidate(tmp_path):
    """Counting every Fireface line made a UCX II beside an 802 ambiguous."""
    proc = fake_proc(tmp_path, boxes=[A])
    cards = proc / "asound" / "cards"
    cards.write_text(cards.read_text()
                     + " 5 [Fireface802 ]: USB-Audio - Fireface 802 (23456789)\n")
    add_clients(proc, b'Client  32 : "Fireface 802 (23456789)" [Kernel Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=A[1], client=A[0])

def test_a_second_box_still_enumerating_is_not_ignored(tmp_path):
    """Its card is listed, its client is not up yet: not a choice either."""
    proc = fake_proc(tmp_path, boxes=[A, B])
    (proc / "asound" / "seq" / "clients").write_text(
        'Client  24 : "Fireface UCX II (24216011)" [Kernel Legacy]\n')
    with pytest.raises(DeviceAmbiguous):
        resolve_device("2a39:3fd9", "Fireface UCX II", "", proc)

def test_a_user_space_client_cannot_pose_as_the_interface(tmp_path):
    proc = fake_proc(tmp_path, boxes=[A])
    add_clients(proc, b'Client 129 : "Fireface UCX II (99887766)" [User Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=A[1], client=A[0])
    assert resolve_device("2a39:3fd9", "Fireface UCX II", B[1], proc).client \
        is None

def test_a_serial_with_letters_is_a_config_error(tmp_path):
    """It passed validation and could never be matched by the selection."""
    from oscmix_desk.errors import ConfigError

    path = write_config(tmp_path / "routing.conf",
                        "[device]\nserial = ABC123\n" + DESK)
    with pytest.raises(ConfigError, match="digits only"):
        profiles.load_config(path)

KERNEL_A = 'Client  24 : "Fireface UCX II (24216011)" [Kernel Legacy]\n'

KERNEL_B = 'Client  28 : "Fireface UCX II (99887766)" [Kernel Legacy]\n'

def test_a_quote_in_a_client_name_does_not_make_it_a_kernel_client():
    spoof = 'Client 130 : "Fireface UCX II (99887766)" [Kernel" [User Legacy]\n'
    assert select_seq_client(KERNEL_A + spoof, "Fireface UCX II", B[1]) is None
    assert select_seq_client(KERNEL_A + spoof, "Fireface UCX II") == A[0]

def test_a_forged_line_that_repeats_a_client_number_is_refused_as_such():
    """A name with a newline can forge a whole line for an existing client.

    Nothing says which of the two lines is real. Dropping both made the
    real interface vanish and the start loop; a refusal names the cause.
    """
    forged = ('Client 130 : "x\nClient  28 : "Fireface UCX II (24216011)" '
              '[Kernel Legacy]\ny" [User Legacy]\n')
    with pytest.raises(DeviceAmbiguous, match="listed more than once"):
        select_seq_client(KERNEL_B + forged, "Fireface UCX II")

def test_a_forged_client_for_a_box_no_card_shows_is_not_an_interface(tmp_path):
    proc = fake_proc(tmp_path, boxes=[A])
    add_clients(proc, b'Client 140 : "Fireface UCX II (99887766)" '
                       b'[Kernel Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", B[1], proc).client \
        is None
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=A[1], client=A[0])

def test_a_model_whose_name_starts_another_is_matched_exactly(tmp_path):
    clients = ('Client  24 : "Fireface 802 (11112222)" [Kernel Legacy]\n'
               'Client  28 : "Fireface 802 FS (33334444)" [Kernel Legacy]\n')
    assert select_seq_client(clients, "Fireface 802") == 24
    assert select_seq_client(clients, "Fireface 802 FS") == 28
    # A name with its serial is still a name, and a part of one still
    # matches when nothing matches exactly.
    assert select_seq_client(clients, "Fireface 802 FS (33334444)") == 28
    assert select_seq_client(KERNEL_A, "UCX II") == A[0]
    cards = tmp_path / "cards"
    cards.write_text(" 2 [F802 ]: USB-Audio - Fireface 802 (11112222)\n"
                     " 3 [F802FS ]: USB-Audio - Fireface 802 FS (33334444)\n")
    from oscmix_desk.discovery import device_serials
    assert device_serials(cards, "Fireface 802") == ["11112222"]
    assert device_serials(cards) == ["11112222", "33334444"]

def test_a_forged_kernel_line_without_a_serial_or_under_a_bare_name_is_ignored(
        tmp_path):
    """Its name is not a card's product name, so it is not an interface."""
    proc = fake_proc(tmp_path, boxes=[A])
    add_clients(proc, b'Client 140 : "Fireface UCX II" [Kernel Legacy]\n'
                       b'Client 141 : "UCX II" [Kernel Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=A[1], client=A[0])
    assert resolve_device("2a39:3fd9", "UCX II", "", proc).client == A[0]

    unplugged = fake_proc(tmp_path / "unplugged")
    add_clients(unplugged, b'Client 140 : "Fireface UCX II" [Kernel Legacy]\n')
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "",
                          unplugged).client is None

def test_a_client_listed_before_its_card_waits_for_the_card(tmp_path):
    """The kernel may show the client a moment before the card line."""
    proc = fake_proc(tmp_path, boxes=[A])
    cards = proc / "asound" / "cards"
    listed = cards.read_text()
    cards.write_text("")
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc).client \
        is None
    cards.write_text(listed)
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc).client \
        == A[0]

def test_the_serial_comes_from_the_client_when_the_card_list_is_unreadable(
        tmp_path):
    proc = fake_proc(tmp_path, boxes=[B])
    (proc / "asound" / "cards").unlink()
    assert resolve_device("2a39:3fd9", "Fireface UCX II", "", proc) == \
        Device(usb_id="2a39:3fd9", serial=B[1], client=B[0])

def test_waiting_returns_the_device_once_its_client_comes_up(tmp_path):
    from oscmix_desk.discovery import wait_for_device

    proc = fake_proc(tmp_path, boxes=[B])
    clients = proc / "asound" / "seq" / "clients"
    listed = clients.read_text()
    clients.write_text("")
    threading.Timer(0.3, lambda: clients.write_text(listed)).start()
    assert wait_for_device("2a39:3fd9", "Fireface UCX II", "", 5.0, proc) == \
        Device(usb_id="2a39:3fd9", serial=B[1], client=B[0])

def test_a_card_list_that_is_not_utf8_is_still_read(tmp_path):
    from oscmix_desk.discovery import card_products

    cards = tmp_path / "cards"
    cards.write_bytes(b" 0 [X ]: HDA-Intel - HDA \xff\xfe\n"
                      b" 2 [II24216011 ]: USB-Audio - Fireface UCX II (24216011)\n")
    assert card_products(cards)[1] == "Fireface UCX II (24216011)"
