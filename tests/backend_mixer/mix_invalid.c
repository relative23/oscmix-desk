#define main unused_value_main
#include "mix_bulk.c"
#undef main
#include <setjmp.h>
static int bad_kind,fail_out;static struct device bad_device;
static int mapping(enum control ctl,const struct param *p)
{
    if(ctl==MIX && bad_kind==1)return -1;
    if(ctl==MIX && bad_kind==2)return 0x8000;
    if(ctl==MIX_LEVEL && p->out==fail_out){
        if(bad_kind==3)return -1;
        if(bad_kind==4)return 0x8000;
        if(bad_kind==5){struct param copied=*p;copied.out=0;return ffucxii.ctltoreg(ctl,&copied);}
    }
    return ffucxii.ctltoreg(ctl,p);
}
static int attempt(float db,int pan)
{
    unsigned char raw[256];struct oscmsg msg={.buf=raw,.end=raw+sizeof raw,.type=NULL};
    oscputstr(&msg,"/mix/1/input/13");oscputstr(&msg,",fi");oscputfloat(&msg,db);oscputint(&msg,pan);
    assert(!msg.err);return handleosc(raw,msg.buf-raw);
}
static void same_cache(const float before[20][40])
{
    assert(!memcmp(before,matrix,sizeof matrix));
    assert(inputs[12].width==100 && inputs[13].width==100);
}
int main(void)
{
    unsigned count=0;float before[20][40];
    for(int kind=1;kind<=5;kind++){
        initialize(12,0,true,true,false,100);matrix[0][12]=0.125f;memcpy(before,matrix,sizeof matrix);
        bad_device=ffucxii;bad_device.ctltoreg=mapping;device=&bad_device;bad_kind=kind;fail_out=1;
        assert(attempt(-60,1)==-1 && calls==0 && words==0);same_cache(before);++count;
    }
    const float bad[]={NAN,INFINITY,100.f};
    for(unsigned i=0;i<3;i++){
        initialize(12,0,true,true,false,100);memcpy(before,matrix,sizeof matrix);
        assert(attempt(bad[i],1)==-1 && calls==0 && words==0);same_cache(before);++count;
    }
    initialize(12,0,true,true,false,101);memcpy(before,matrix,sizeof matrix);
    assert(attempt(-60,1)==-1 && calls==0 && words==0);assert(!memcmp(before,matrix,sizeof matrix));++count;
    /* Caller-private full capacity can never overflow, mutate or send. */
    initialize(12,0,true,true,false,100);memcpy(before,matrix,sizeof matrix);
    struct mixwrites full={.count=8};struct mixwrites original=full;
    const struct level level={.vol=1,.pan=0,.width=100};
    assert(planlevel(outputs,inputs+12,true,&level,&full)==-1);
    assert(!memcmp(&full,&original,sizeof full) && calls==0);same_cache(before);++count;
    for(int n=-1;n<=9;n+=10){
        full.count=n;original=full;assert(planlevel(outputs,inputs+12,true,&level,&full)==-1);
        assert(!memcmp(&full,&original,sizeof full) && calls==0);same_cache(before);++count;
    }
    printf("PASS actual_mapping_value_capacity_reject_cases=%u zero_frames_zero_cache_mutation=true\n",count);
    return 0;
}
