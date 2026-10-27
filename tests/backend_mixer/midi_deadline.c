/* Actual complete main.c. Deterministic syscall hooks, no fd/socket access. */
#define main unused_backend_main
#define write fixture_write
#define poll fixture_poll
#define clock_gettime fixture_clock
#define fatal fixture_fatal
#ifndef MAIN_SOURCE
#define MAIN_SOURCE "main.c"
#endif
#include MAIN_SOURCE
#undef main
#undef write
#undef poll
#undef clock_gettime
#undef fatal
#include <setjmp.h>

static jmp_buf stop_jump;
static int mode, write_calls, poll_calls, clock_calls, fail_clock_at, fatal_count;
static int last_poll_timeout, fatal_errno;
static int64_t now_ns, step_ns;
static size_t sent;
static unsigned char sink[207], input[207];
static const char *fatal_text;

void fixture_fatal(const char *msg,...)
{
    fatal_text=msg;fatal_errno=errno;++fatal_count;longjmp(stop_jump,1);
}
int fixture_clock(clockid_t clock_id,struct timespec *p)
{
    assert(clock_id==CLOCK_MONOTONIC);++clock_calls;
    if(clock_calls==fail_clock_at){errno=EIO;return -1;}
    p->tv_sec=(time_t)(now_ns/1000000000);
    p->tv_nsec=(long)(now_ns%1000000000);errno=E2BIG;return 0;
}
ssize_t fixture_write(int fd,const void *p,size_t len)
{
    assert(fd==7 && p==input+sent && len==sizeof input-sent);
    ++write_calls;now_ns+=step_ns;
#ifdef ORIGINAL_TRANSPORT_WITNESS
    if(mode==1 && write_calls>=6)fixture_fatal("fixture safety stop after unbounded EINTR");
#endif
    if(mode==1){errno=EINTR;return -1;}
    if(mode==3 || mode==9 || mode==10 || (mode==4 && write_calls==1)){errno=EAGAIN;return -1;}
    if(mode==6){errno=EIO;return -1;}
    if(mode==7)return 0;
    if(mode==8)stopping=1;
    size_t n=(mode==2 || mode==8) ? 1 : mode==4 ? (len>11?11:len) : len;
    memcpy(sink+sent,p,n);sent+=n;return (ssize_t)n;
}
int fixture_poll(struct pollfd *fds,nfds_t n,int wait)
{
    assert(n==1 && fds[0].fd==7 && fds[0].events==POLLOUT);
    assert(wait>0 && wait<=2000);last_poll_timeout=wait;++poll_calls;
    if(mode==3){now_ns+=(int64_t)wait*1000000;return 0;}
    if(mode==9){now_ns+=(int64_t)wait*1000000;errno=EINTR;return -1;}
    if(mode==10){errno=EIO;return -1;}
    return 1;
}
static bool run(int selected,int64_t per_write,bool coordinated_mode)
{
    mode=selected;step_ns=per_write;now_ns=900000000;
    write_calls=poll_calls=clock_calls=fatal_count=last_poll_timeout=0;sent=0;
    fatal_text=NULL;stopping=selected==12;coordinated=coordinated_mode;
    memset(sink,0,sizeof sink);
    for(size_t i=0;i<sizeof input;i++)input[i]=(unsigned char)i;
    if(setjmp(stop_jump))return false;
    writemidi(input,sizeof input);return true;
}
int main(void)
{
    unsigned cases=0;
#ifdef ORIGINAL_TRANSPORT_WITNESS
    assert(run(2,500000000,true) && sent==207 && write_calls==207 && now_ns==104400000000);
    printf("original coordinated partial progress: returned success after simulated103.5seconds (%d writes)\n",write_calls);++cases;
    assert(!run(1,500000000,true) && write_calls==6 && !strcmp(fatal_text,"fixture safety stop after unbounded EINTR"));
    printf("original coordinated EINTR: no deadline check, external fixture stopped after simulated3seconds\n");++cases;
    assert(run(0,3000000000,true) && sent==207);
    printf("original coordinated final full write: returned success after simulated3seconds\n");++cases;
#else
    assert(run(0,0,true) && write_calls==1 && sent==207 && clock_calls==3);++cases;
    assert(run(2,1000000,true) && write_calls==207 && sent==207 && now_ns==1107000000);
    assert(!memcmp(sink,input,sizeof input));++cases;
    assert(!run(1,500000000,true) && write_calls==4 && sent==0 && !strcmp(fatal_text,"MIDI bridge write timed out"));++cases;
    assert(!run(2,500000000,true) && write_calls==4 && sent==4 && !strcmp(fatal_text,"MIDI bridge write timed out"));++cases;
    assert(!run(0,2000000000,true) && sent==207 && write_calls==1 && !strcmp(fatal_text,"MIDI bridge write timed out"));++cases;
    assert(run(0,1999999999,true) && sent==207);++cases;
    assert(!run(3,0,true) && write_calls==1 && poll_calls==1 && last_poll_timeout==2000 && !strcmp(fatal_text,"MIDI bridge write timed out"));++cases;
    assert(!run(3,1,true) && write_calls==1 && poll_calls==1 && last_poll_timeout==2000);++cases;
    assert(run(4,10000000,true) && poll_calls==1 && write_calls==20 && sent==207 && last_poll_timeout==1990);
    assert(!memcmp(sink,input,sizeof input));++cases;
    assert(!run(6,1,true) && sent==0 && fatal_errno==EIO && !strcmp(fatal_text,"write 7:"));++cases;
    assert(!run(7,1,true) && !strcmp(fatal_text,"MIDI bridge write made no progress"));++cases;
    assert(!run(8,1,true) && sent==1 && write_calls==1 && !strcmp(fatal_text,"MIDI write cancelled"));++cases;
    assert(!run(9,0,true) && write_calls==1 && poll_calls==1 && !strcmp(fatal_text,"MIDI bridge write timed out"));++cases;
    assert(!run(10,0,true) && write_calls==1 && poll_calls==1 && fatal_errno==EIO && !strcmp(fatal_text,"poll MIDI bridge:"));++cases;
    assert(!run(12,0,true) && write_calls==0 && !strcmp(fatal_text,"MIDI write cancelled"));++cases;
    assert(!run(0,-1,true) && !strcmp(fatal_text,"MIDI monotonic clock moved backwards"));++cases;
    fail_clock_at=1;assert(!run(0,0,true) && write_calls==0 && !strcmp(fatal_text,"clock_gettime MIDI write:"));++cases;
    fail_clock_at=2;assert(!run(0,0,true) && write_calls==0 && !strcmp(fatal_text,"clock_gettime MIDI write:"));++cases;
    fail_clock_at=3;assert(!run(0,0,true) && write_calls==1 && !strcmp(fatal_text,"clock_gettime MIDI write:"));++cases;
    fail_clock_at=0;
    assert(run(0,3000000000,false) && clock_calls==1 && sent==207);++cases;
#endif
    printf("PASS actual_writemidi_deadline_cases=%u no_real_syscalls=true absolute_ns_budget=2000000000\n",cases);
    return 0;
}
