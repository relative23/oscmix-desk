/* Exercise the actual bounded coordinator queue without scheduler races. */
#define _GNU_SOURCE
#include <stdlib.h>

static unsigned allocations;
static int fail_allocation;

static void *
packet_alloc(size_t size)
{
	void *result = fail_allocation ? NULL : malloc(size);
	if (result)
		++allocations;
	return result;
}

static void
packet_free(void *packet)
{
	if (packet)
		--allocations;
	free(packet);
}

#define malloc packet_alloc
#define free packet_free
#include "control.c"
#undef malloc
#undef free

int
control_command(const unsigned char *data, size_t len)
{
	(void)data;
	(void)len;
	return 0;
}

static int
peer(void)
{
	int pair[2], buffer = 4096;
	if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK, 0, pair) ||
	    setsockopt(pair[0], SOL_SOCKET, SO_SNDBUF, &buffer, sizeof buffer))
		exit(2);
	clients[0].fd = pair[0];
	return pair[1];
}

int
main(void)
{
	/* A recorded UCX refresh delivered 814 MIDI frames in one second.
	 * Stall the peer for the whole burst, then require every delivery. */
	static const unsigned char value[] = "/output/5/volume\0\0\0\0,f\0\0\0\0\0\0";
	unsigned char packet[CONTROL_PACKET], large[CONTROL_PAYLOAD] = {0};
	unsigned received = 0, sent, high_count = 0;
	size_t high_bytes = 0;
	ssize_t count;
	int fd, large_buffer = 32768;

	for (int index = 0; index < CONTROL_CLIENTS; ++index)
		clients[index].fd = -1;
	fd = peer();
	for (sent = 1; sent <= 814; ++sent)
		enqueue(0, CONTROL_OBSERVATION, 0, CONTROL_DEVICE, sent, value, sizeof value - 1);
	if (clients[0].fd < 0) {
		fputs("finite UCX-sized burst disconnected the paused consumer\n", stderr);
		return 1;
	}
	for (unsigned attempt = 0; received < 814 && attempt < 2048; ++attempt) {
		while ((count = recv(fd, packet, sizeof packet, MSG_DONTWAIT)) > 0) {
			if (count != CONTROL_HEADER + sizeof value - 1 ||
			    get64(packet + 16) != ++received ||
			    memcmp(packet + CONTROL_HEADER, value, sizeof value - 1))
				return 3;
		}
		flush(0);
	}
	if (received != 814 || clients[0].count || clients[0].bytes || allocations)
		return 4;
	/* A stopped reader still has a hard packet limit. */
	for (sent = 0; clients[0].fd >= 0 && sent < 2048; ++sent) {
		enqueue(0, CONTROL_EVENT, 0, CONTROL_OK, sent, NULL, 0);
		if (clients[0].count > high_count)
			high_count = clients[0].count;
	}
	if (clients[0].fd >= 0 || clients[0].count || clients[0].bytes || allocations ||
	    high_count != 1024)
		return 5;
	close(fd);
	/* Large packets hit the unchanged byte limit before the packet limit. */
	fd = peer();
	if (setsockopt(clients[0].fd, SOL_SOCKET, SO_SNDBUF, &large_buffer, sizeof large_buffer))
		return 2;
	for (sent = 0; clients[0].fd >= 0 && sent < 64; ++sent) {
		enqueue(0, CONTROL_OBSERVATION, 0, CONTROL_DEVICE, sent, large, sizeof large);
		if (clients[0].bytes > high_bytes)
			high_bytes = clients[0].bytes;
	}
	if (clients[0].fd >= 0 || clients[0].count || clients[0].bytes || allocations ||
	    high_bytes <= 240 * 1024 || high_bytes > 256 * 1024)
		return 6;
	close(fd);
	/* Allocation loss must visibly end the client, freeing its backlog. */
	fd = peer();
	for (sent = 0; sent < 100; ++sent)
		enqueue(0, CONTROL_EVENT, 0, CONTROL_OK, sent, NULL, 0);
	if (clients[0].fd < 0 || !allocations)
		return 7;
	fail_allocation = 1;
	enqueue(0, CONTROL_EVENT, 0, CONTROL_OK, sent, NULL, 0);
	if (clients[0].fd >= 0 || clients[0].count || clients[0].bytes || allocations)
		return 8;
	close(fd);
	puts("{\"burst_packets\":814,\"packet_bound\":true,\"byte_bound\":true,\"allocation_failure_closed\":true,\"remaining_allocations\":0}");
	return 0;
}
