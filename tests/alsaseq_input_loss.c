/* Exercise the prepared bridge's actual reader without opening ALSA devices.
 * Only the ALSA input result is substituted; the reader/error path is intact.
 */
#define main prepared_bridge_main
#include "alsaseqio.c"
#undef main

int
snd_seq_event_input(snd_seq_t *handle, snd_seq_event_t **event)
{
	static int calls;
	(void)handle;
	(void)event;
	if (!calls++)
		return -ENOSPC;
	fputs("continued-after-known-input-loss\n", stdout);
	return -EIO;
}

int
main(void)
{
	int fd = STDOUT_FILENO;
	midiread(&fd);
	return 2;
}
