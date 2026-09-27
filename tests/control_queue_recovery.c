/* Actual coordinator queue with a temporarily stalled, then writable peer. */
#include "control.c"

int
control_command(const unsigned char *data, size_t len)
{
	(void)data;
	(void)len;
	return 0;
}

static unsigned received;

static void
drain(int fd)
{
	unsigned char packet[CONTROL_PACKET];
	ssize_t count;
	while ((count = recv(fd, packet, sizeof packet, MSG_DONTWAIT)) > 0) {
		if (count != CONTROL_HEADER || get64(packet + 16) != ++received) {
			fputs("lost or reordered queued reply\n", stderr);
			exit(3);
		}
	}
	if (count < 0 && errno != EAGAIN && errno != EWOULDBLOCK)
		exit(4);
}

int
main(void)
{
	int pair[2], buffer = 4096;
	unsigned sent = 0, next;
	struct pollfd ready;

	for (int index = 0; index < CONTROL_CLIENTS; ++index)
		clients[index].fd = -1;
	if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK, 0, pair) ||
	    setsockopt(pair[0], SOL_SOCKET, SO_SNDBUF, &buffer, sizeof buffer))
		return 2;
	clients[0].fd = pair[0];
	while (clients[0].count < QUEUE_PACKETS && sent < 1000)
		enqueue(0, CONTROL_EVENT, 0, CONTROL_OK, ++sent, NULL, 0);
	if (clients[0].fd < 0 || clients[0].count != QUEUE_PACKETS)
		return 5;
	/* The peer has now resumed; capacity exists before the next delivery. */
	drain(pair[1]);
	ready = (struct pollfd){.fd = pair[0], .events = POLLOUT};
	if (poll(&ready, 1, 0) != 1 || !(ready.revents & POLLOUT))
		return 6;
	enqueue(0, CONTROL_EVENT, 0, CONTROL_OK, ++sent, NULL, 0);
	if (clients[0].fd < 0) {
		fputs("writable peer disconnected despite available send capacity\n", stderr);
		return 1;
	}
	for (unsigned index = 0; received < sent && index < 1000; ++index) {
		drain(pair[1]);
		flush(0);
	}
	if (received != sent || clients[0].count)
		return 7;
	/* A peer which stays stalled must still be disconnected at the bound. */
	for (next = sent; clients[0].fd >= 0 && next < sent + 1000; ++next)
		enqueue(0, CONTROL_EVENT, 0, CONTROL_OK, next + 1, NULL, 0);
	if (clients[0].fd >= 0)
		return 8;
	close(pair[1]);
	printf("{\"recovered_packets\":%u,\"slow_peer_disconnected\":true}\n", received);
	return 0;
}
