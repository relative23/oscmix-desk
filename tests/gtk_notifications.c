/* Observe actual GTK mixer values and global connection notifications. */
#include <stdio.h>
#include <stdlib.h>
#include <gtk/gtk.h>
#include "gtk/mixer.h"

static GMainLoop *loop;
static unsigned notifications, reports;
static gboolean armed;

static void
connection_changed(GObject *obj, GParamSpec *spec, gpointer unused)
{
	++notifications;
}

static gboolean
arm(gpointer unused)
{
	notifications = reports = 0;
	armed = TRUE;
	puts("READY");
	fflush(stdout);
	return G_SOURCE_REMOVE;
}

static gboolean
finish(gpointer unused)
{
	printf("{\"reports\":%u,\"connection_notifications\":%u}\n", reports, notifications);
	fflush(stdout);
	g_main_loop_quit(loop);
	return G_SOURCE_REMOVE;
}

static void
observed(GValue *args, guint len, gpointer unused)
{
	if (len != 1 || !G_VALUE_HOLDS_FLOAT(args))
		exit(3);
	++reports;
	/* Idle callbacks count notifications from the complete delivery too. */
	if (!armed)
		g_idle_add(arm, NULL);
	else if (g_value_get_float(args) == -44.0f)
		g_idle_add(finish, NULL);
}

static gboolean
expired(gpointer unused)
{
	fputs("GTK observation probe timed out\n", stderr);
	exit(2);
}

int
main(int argc, char **argv)
{
	Mixer *mixer;
	if (argc != 3)
		return 2;
	loop = g_main_loop_new(NULL, FALSE);
	mixer = mixer_new();
	g_signal_connect(mixer, "notify::writable", G_CALLBACK(connection_changed), NULL);
	g_signal_connect(mixer, "notify::status", G_CALLBACK(connection_changed), NULL);
	mixer_connect(mixer, "/output/5/volume", observed, NULL);
	mixer_start(mixer, argv[1], strtol(argv[2], NULL, 10), "00000000");
	g_timeout_add_seconds(5, expired, NULL);
	g_main_loop_run(loop);
	g_object_unref(mixer);
	g_main_loop_unref(loop);
	return 0;
}
