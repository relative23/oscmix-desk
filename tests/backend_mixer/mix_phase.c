/* Phase as the sign of the mix coefficients, through the actual callbacks.
 * TotalMix FX on the UCX II sets a phase register and negates the cells it
 * concerns; the register alone changes nothing in the signal (measured through
 * output loopback, 2026-11-07). Only the MIDI and OSC sinks are substituted. */
#ifndef BACKEND_SOURCE
#define BACKEND_SOURCE "oscmix.c"
#endif
#include BACKEND_SOURCE
extern const struct device ffucxii;
static struct input input_store[40];
static struct output output_store[20];
static float matrix[20][40];
static unsigned char observed[20][20];
static unsigned regs[256], vals[256], words, cases;
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
    const unsigned char *p=ptr;assert(len>=12 && (len-7)%5==0);
    assert(p[0]==0xf0 && p[len-1]==0xf7);
    for(size_t offset=6;offset<len-1;offset+=5){
        uint32_t v=decode(p+offset);assert(words<256);
        regs[words]=(v>>16)&0x7fff;vals[words++]=v&0xffff;
    }
}
void writeosc(const void *ptr,size_t len){(void)ptr;(void)len;}
void oscsource(unsigned value){(void)value;}
static void initialize(void)
{
    static bool mapped;
    if(!mapped){memset(nodeindex,0xFF,sizeof nodeindex);maptree(roottree,0);mapped=true;}
    inputs=input_store;outputs=output_store;device=&ffucxii;
    memset(input_store,0,sizeof input_store);memset(output_store,0,sizeof output_store);
    memset(matrix,0,sizeof matrix);memset(observed,0,sizeof observed);
    for(int o=0;o<20;o++){outputs[o].mix=matrix[o];outputs[o].mix_observed=observed[o];}
    for(int i=0;i<40;i++)inputs[i].width=100;
    words=0;
}
static unsigned cell(int in,int out)
{
    struct param p={.in=in,.out=out};
    return (unsigned)ffucxii.ctltoreg(MIX_LEVEL,&p);
}
static unsigned phasereg(int input,int channel)
{
    struct param p={.in=input?channel:-1,.out=input?-1:channel};
    return (unsigned)ffucxii.ctltoreg(input?INPUT_PHASE:OUTPUT_PHASE,&p);
}
/* The value last written to a register, or ~0u when none was. */
static unsigned written(unsigned reg)
{
    unsigned value=~0u;
    for(unsigned i=0;i<words;i++)if(regs[i]==reg)value=vals[i];
    return value;
}
static void osc(const char *path,const char *type,float f,int i)
{
    unsigned char raw[256];
    struct oscmsg msg={.buf=raw,.end=raw+sizeof raw,.type=NULL};
    oscputstr(&msg,path);oscputstr(&msg,type);
    if(type[1]=='f'){oscputfloat(&msg,f);oscputint(&msg,i);}else oscputint(&msg,i);
    assert(!msg.err && handleosc(raw,msg.buf-raw)==0);
}
static void report(unsigned reg,unsigned value)
{
    uint_least32_t word=(uint_least32_t)reg<<16|value;
    handleregs(&word,1);
}
static void encoding(void)
{
    /* 0x9000 is +1 and 0xF000 -1 as TotalMix FX writes them; 0.5 is 0x8800. */
    const float levels[]={0.f,0.25f,0.5f,1.f,2.f};
    const unsigned positive[]={0,0x2000,0x8800,0x9000,0xA000};
    const unsigned negative[]={0,0x6000,0xF800,0xF000,0xE000};
    for(unsigned i=0;i<5;i++){
        assert(mixlevelvalue(levels[i],false)==positive[i]);
        assert(mixlevelvalue(levels[i],true)==negative[i]);
        ++cases;
    }
    /* Just below 0.5 stays in the fine form, as positive and negative. */
    assert(mixlevelvalue(0x3fffp-15f,false)==0x3fff && mixlevelvalue(0x3fffp-15f,true)==0x4001);
    ++cases;
}
static void input_phase(void)
{
    initialize();
    osc("/mix/3/input/2",",fi",0.f,0);          /* input 2 -> output 3 at 0 dB */
    osc("/mix/4/input/2",",fi",-6.0206f,0);     /* and -> output 4 at 0.5 */
    osc("/mix/5/playback/1",",fi",0.f,0);       /* a playback cell nearby */
    assert(written(cell(1,2))==0x9000 && written(cell(1,3))==0x8800);
    words=0;
    osc("/input/2/phase",",i",0,1);
    assert(written(phasereg(1,1))==1);
    assert(written(cell(1,2))==0xF000 && written(cell(1,3))==0xF800);
    assert(written(cell(20,4))==~0u);            /* playback is not an input */
    assert(written(cell(0,2))==~0u);             /* a zero cell is left alone */
    ++cases;
    words=0;
    osc("/output/3/phase",",i",0,1);             /* both on: positive again */
    assert(written(phasereg(0,2))==1 && written(cell(1,2))==0x9000);
    assert(written(cell(1,3))==~0u);
    ++cases;
    words=0;
    osc("/mix/3/input/2",",fi",-20.f,0);         /* a new level keeps the sign rule */
    assert(written(cell(1,2))==mixlevelvalue(powf(10.f,-1.f),false));
    words=0;
    osc("/input/2/phase",",i",0,1);              /* unchanged: nothing rewritten */
    assert(written(cell(1,2))==~0u && written(cell(1,3))==~0u);
    ++cases;
}
static void output_phase(void)
{
    initialize();
    osc("/mix/6/input/1",",fi",0.f,0);
    osc("/mix/6/playback/3",",fi",-12.f,0);
    words=0;
    osc("/output/6/phase",",i",0,1);
    assert(written(cell(0,5))==0xF000);
    assert(written(cell(22,5))==mixlevelvalue(powf(10.f,-0.6f),true));
    assert(written(cell(1,5))==~0u);
    ++cases;
    /* A muted input is not rewritten; unmuting writes it with its sign. */
    osc("/input/1/mute",",i",0,1);
    words=0;
    osc("/output/6/phase",",i",0,0);
    assert(written(cell(0,5))==~0u && written(cell(22,5))==mixlevelvalue(powf(10.f,-0.6f),false));
    osc("/output/6/phase",",i",0,1);
    words=0;
    osc("/input/1/mute",",i",0,0);
    assert(written(cell(0,5))==0xF000);
    ++cases;
}
static void reported_phase(void)
{
    /* A phase the device reports, set by another client, applies as well. */
    initialize();
    osc("/mix/2/input/3",",fi",0.f,0);
    words=0;
    report(phasereg(1,2),1);
    assert(inputs[2].phase && written(cell(2,1))==0xF000);
    words=0;
    report(phasereg(1,2),1);                      /* repeated report: no writes */
    assert(words==0);
    report(phasereg(0,1),1);                      /* the output as well: positive */
    assert(outputs[1].phase && written(cell(2,1))==0x9000);
    ++cases;
}
int main(void)
{
    encoding();input_phase();output_phase();reported_phase();
    printf("PASS actual_phase_sign_cases=%u\n",cases);
    return 0;
}
