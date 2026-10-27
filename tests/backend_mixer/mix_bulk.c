/* Full actual old/new backend callbacks + codecs; only sinks are substituted. */
#ifndef BACKEND_SOURCE
#define BACKEND_SOURCE "oscmix.c"
#endif
#include BACKEND_SOURCE
#ifdef FIXTURE_PARTIAL
#include <setjmp.h>
static jmp_buf partial_jump;
static unsigned fail_after_words;
#endif
extern const struct device ffucxii;
static struct input input_store[40];
static struct output output_store[20];
static float matrix[20][40];
static unsigned char observed[20][20];
static unsigned calls, words, record[8], frames_max;
static unsigned cases;
static uint64_t digest=UINT64_C(14695981039346656037);
static uint32_t decode(const unsigned char *p)
{
    uint32_t value=0;unsigned parity=0;
    for(unsigned i=0;i<5;i++){
        assert(p[i]<128 && (i!=4 || p[i]<16));
        for(unsigned b=0;b<7 && i*7+b<32;b++)if(p[i]&(1u<<b))value|=UINT32_C(1)<<(i*7+b);
    }
    for(unsigned b=0;b<32;b++)parity+=(value>>b)&1u;
    assert(parity%2==1);return value;
}
void writemidi(const void *ptr,size_t len)
{
    const unsigned char *p=ptr;assert(len>=12 && len<=47 && (len-7)%5==0);
    assert(p[0]==0xf0 && p[1]==0 && p[2]==0x20 && p[3]==0x0d && p[4]==0x10 && p[5]==0 && p[len-1]==0xf7);
    ++calls;if(len>frames_max)frames_max=(unsigned)len;
#ifdef FIXTURE_PARTIAL
    if(!fail_after_words)longjmp(partial_jump,1);
#endif
    for(size_t offset=6;offset<len-1;offset+=5){
        assert(words<8);record[words++]=decode(p+offset);
#ifdef FIXTURE_PARTIAL
        if(words==fail_after_words)longjmp(partial_jump,1);
#endif
    }
}
void writeosc(const void *ptr,size_t len){(void)ptr;(void)len;assert(!"unexpected OSC delivery");}
void oscsource(unsigned value){(void)value;assert(!"unexpected source hook");}
static void hashword(uint32_t value)
{
    for(unsigned b=0;b<4;b++){digest^=(value>>(b*8))&255u;digest*=UINT64_C(1099511628211);}
}
static void initialize(int source,int out,int instereo,int outstereo,int muted,int width)
{
    inputs=input_store;outputs=output_store;device=&ffucxii;
    memset(input_store,0,sizeof input_store);memset(output_store,0,sizeof output_store);
    memset(matrix,0,sizeof matrix);memset(observed,0,sizeof observed);
    for(int o=0;o<20;o++){outputs[o].mix=matrix[o];outputs[o].mix_observed=observed[o];}
    for(int i=0;i<40;i++)inputs[i].width=width;
    inputs[source].stereo=instereo;
    if(instereo)inputs[source&~1].stereo=inputs[(source&~1)+1].stereo=true;
    inputs[source].mute=muted;
    if(instereo)inputs[source&~1].mute=inputs[(source&~1)+1].mute=muted;
    outputs[out].stereo=outstereo;
    if(outstereo)outputs[out&~1].stereo=outputs[(out&~1)+1].stereo=true;
    calls=words=frames_max=0;
}
static void submit(int family,int source,int out,float db,int pan)
{
    unsigned char raw[256];char path[80];
    snprintf(path,sizeof path,"/mix/%d/%s/%d",out+1,family?"playback":"input",source+1);
    struct oscmsg msg={.buf=raw,.end=raw+sizeof raw,.type=NULL};
    oscputstr(&msg,path);oscputstr(&msg,",fi");oscputfloat(&msg,db);oscputint(&msg,pan);
    assert(!msg.err && handleosc(raw,msg.buf-raw)==0);
}
int main(void)
{
    const int sources[]={0,12,13,18,19},outs[]={0,1,18,19},widths[]={-100,-1,0,1,100};
    const float dbs[]={-INFINITY,-64.99f,-60,-20,0,6};
    const int pans[]={-100,-99,-1,0,1,99,100};
    for(int family=0;family<2;family++)for(unsigned s=0;s<5;s++)for(unsigned o=0;o<4;o++)
        for(int is=0;is<2;is++)for(int os=0;os<2;os++)for(int mute=0;mute<2;mute++)
        for(unsigned w=0;w<5;w++)for(unsigned db=0;db<6;db++)for(unsigned pan=0;pan<7;pan++){
            int source=sources[s]+family*20,out=outs[o];
            initialize(source,out,is,os,mute,widths[w]);submit(family,sources[s],out,dbs[db],pans[pan]);
#ifdef CANDIDATE
            assert(calls==(words?1u:0u));
#else
            assert(calls==words);
#endif
            unsigned expected_coeff=mute?0u:(is?2u:1u)*(os?2u:1u);
            unsigned expected_meta=family?0u:(is?4u:2u);
            assert(words==expected_coeff+expected_meta && words<=8);
            hashword((uint32_t)cases);hashword(words);
            for(unsigned i=0;i<words;i++)hashword(record[i]);
            /* Complete cache + flags, including every undeclared cell. */
            for(int row=0;row<20;row++)for(int col=0;col<40;col++){
                uint32_t bits;memcpy(&bits,&matrix[row][col],4);hashword(bits);
                assert(isfinite(matrix[row][col]) && matrix[row][col]>=0 && matrix[row][col]<=2);
            }
            for(int i=0;i<40;i++){
                hashword(inputs[i].stereo);hashword(inputs[i].stereo_observed);
                hashword(inputs[i].mute);hashword((uint32_t)inputs[i].width);
            }
            ++cases;
        }
    printf("PASS actual_callback_cases=%u complete_cache_word_sequence_digest=%016llx\n",cases,(unsigned long long)digest);
    return 0;
}
