/* Record the input pool the prepared bridge asks the sequencer for, without
 * opening ALSA devices. Only the ALSA calls before the first port exist here.
 */
#define main prepared_bridge_main
#include "alsaseqio.c"
#undef main

static long pool = -1;

int
snd_seq_open(snd_seq_t **handle, const char *name, int streams, int mode)
{
	(void)name;
	(void)streams;
	(void)mode;
	*handle = (snd_seq_t *)&pool;
	return 0;
}

int
snd_seq_set_client_pool_input(snd_seq_t *handle, size_t size)
{
	(void)handle;
	pool = (long)size;
	return 0;
}

int
snd_seq_set_client_name(snd_seq_t *handle, const char *name)
{
	(void)handle;
	(void)name;
	printf("input-pool=%ld\n", pool);
	fflush(stdout);
	return -ENODEV;
}

int
main(void)
{
	char *argv[] = {"alsaseqio", "24:1", "true", NULL};
	return prepared_bridge_main(3, argv);
}
