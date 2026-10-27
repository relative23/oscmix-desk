/* Actual frozen combined C backend; no hardware/transport/syscalls. */
#include "oscmix.c"
#include <fenv.h>
extern const struct device ffucxii;
static struct input input_store[40];
static struct output output_store[20];
static float coefficients[20][40];
static unsigned char observed[20][20];
static unsigned frames, reports, metadata_count;
static unsigned metadata_in[4],metadata_value[4],physical_values[4];
static char address[256];
static float last_db;
static int last_pan;
static uint32_t word(const unsigned char *p)
{
    uint32_t value=0;unsigned parity=0;
    for(unsigned i=0;i<5;i++){
        assert(p[i]<128 && (i!=4 || p[i]<16));
        for(unsigned b=0;b<7 && i*7+b<32;b++)if(p[i]&(1u<<b))value|=UINT32_C(1)<<(i*7+b);
    }
    for(unsigned i=0;i<32;i++)parity+=(value>>i)&1;
    assert(parity%2==1);return value;
}
void writemidi(const void *ptr,size_t len)
{
    const unsigned char *p=ptr;assert(len>=12 && len<=47 && (len-7)%5==0);
    assert(p[0]==0xf0 && p[1]==0 && p[2]==0x20 && p[3]==0x0d && p[4]==0x10 && p[5]==0 && p[len-1]==0xf7);
    for(size_t offset=6;offset<len-1;offset+=5){
    uint32_t value=word(p+offset);unsigned reg=(value>>16)&0x7fff,v=value&0xffff;
    if(reg==0x200c || reg==0x200d){
        assert(metadata_count<4);metadata_in[metadata_count]=reg-0x2000;metadata_value[metadata_count++]=v;
    }else{
        const unsigned own[]={0x400c,0x400d,0x404c,0x404d};int found=-1;
        for(unsigned i=0;i<4;i++)if(reg==own[i])found=(int)i;
        assert(found>=0);physical_values[found]=v;
    }
    }
    ++frames;
}
void writeosc(const void *ptr,size_t len)
{
    /* Real oscflush emits bundles; inspect all actual compoundreports. */
    const unsigned char *p=ptr;assert(len>=16 && !memcmp(p,"#bundle",8));
    size_t pos=16;
    while(pos<len){
        assert(pos+4<=len);size_t n=getbe32(p+pos);pos+=4;assert(pos+n<=len);
        struct oscmsg msg={.buf=(unsigned char *)p+pos,.end=(unsigned char *)p+pos+n,.type="ss"};
        const char *path=oscgetstr(&msg);msg.type=oscgetstr(&msg);
        assert(!strcmp(path,"/mix/1/input/13") && !strcmp(msg.type,",fi"));
        ++msg.type;last_db=oscgetfloat(&msg);last_pan=oscgetint(&msg);assert(!oscend(&msg));
        ++reports;pos+=n;
    }
}
void oscsource(unsigned value){(void)value;assert(!"unexpected source hook");}
static void initialize(void)
{
    inputs=input_store;outputs=output_store;device=&ffucxii;
    memset(input_store,0,sizeof input_store);memset(output_store,0,sizeof output_store);
    memset(coefficients,0,sizeof coefficients);memset(observed,0,sizeof observed);
    for(int o=0;o<20;o++){outputs[o].mix=coefficients[o];outputs[o].mix_observed=observed[o];}
    for(int i=0;i<40;i++)inputs[i].width=100;
    inputs[12].stereo=inputs[13].stereo=true;outputs[0].stereo=outputs[1].stereo=true;
    frames=reports=metadata_count=0;memset(physical_values,0,sizeof physical_values);feclearexcept(FE_ALL_EXCEPT);
}
static void submit(float db,int pan,bool even_alias)
{
    unsigned char raw[256];struct oscmsg msg={.buf=raw,.end=raw+sizeof raw,.type=NULL};
    oscputstr(&msg,even_alias?"/mix/2/input/14":"/mix/1/input/13");
    oscputstr(&msg,",fi");oscputfloat(&msg,db);oscputint(&msg,pan);
    assert(!msg.err && handleosc(raw,msg.buf-raw)==0 && metadata_count==4);
    assert(frames==1);
}
static void consume(const int order[4])
{
    /* setmix intentionally computeslog10(0) for a mute command; isolate
     * actual newmix finite reconstruction from that established floor. */
    feclearexcept(FE_ALL_EXCEPT);
    unsigned values[4],channels[4];memcpy(values,metadata_value,sizeof values);memcpy(channels,metadata_in,sizeof channels);
    inputs[12].stereo_observed=inputs[13].stereo_observed=outputs[0].stereo_observed=true;
    for(int i=0;i<4;i++){
        int index=order[i];struct context ctx={.param={.in=(int)channels[index],.out=0},.addr=address,.addrend=address+sizeof address};
        newmix(&ctx,(int)values[index]);
    }
    oscflush();assert(reports==1);
    assert(!(fetestexcept(FE_INVALID|FE_DIVBYZERO)));
    assert(isfinite(last_db) || isinf(last_db)&&last_db<0);
    assert(last_pan>=-100 && last_pan<=100 && inputs[12].width==100);
    for(int o=0;o<20;o++)for(int i=0;i<40;i++)
        assert(isfinite(coefficients[o][i]) && coefficients[o][i]>=0 && coefficients[o][i]<=2);
}
static void silence_restore(float db,bool expected_pan_preserved)
{
    initialize();submit(db,0,false);
    const unsigned baseline_db=(unsigned)(-650)&0x7fff;
    assert(metadata_in[0]==12 && metadata_in[1]==12 && metadata_in[2]==13 && metadata_in[3]==13);
    assert(metadata_value[0]==baseline_db && metadata_value[2]==baseline_db);
    if(expected_pan_preserved){
        assert(metadata_value[1]==((unsigned)(-100)&0x7fff|0x8000));
        assert(metadata_value[3]==(100u|0x8000));
    }else{
        assert(metadata_value[1]==0x8000 && metadata_value[3]==0x8000);
    }
    const int order[]={0,1,2,3};consume(order);
    assert(isinf(last_db)&&last_db<0 && last_pan==0);
    for(int o=0;o<20;o++)for(int i=0;i<40;i++)assert(coefficients[o][i]==0);
    for(int i=0;i<4;i++)assert(physical_values[i]==0);
}
int main(void)
{
    unsigned cases=0;silence_restore(-64.99f,true);++cases;
    silence_restore(-INFINITY,false);++cases;
    const float dbs[]={-60,-60,-60,-60,-20};const int pans[]={-1,1,-100,100,99};
    for(int c=0;c<5;c++)for(int alias=0;alias<2;alias++)
        for(int a=0;a<4;a++)for(int b=0;b<4;b++)for(int x=0;x<4;x++)for(int d=0;d<4;d++){
            if(a==b||a==x||a==d||b==x||b==d||x==d)continue;
            initialize();submit(dbs[c],pans[c],alias);
            const int order[]={a,b,x,d};consume(order);
            assert(isfinite(last_db) && fabsf(last_db-dbs[c])<=0.1f && abs(last_pan-pans[c])<=1);
            ++cases;
        }
    printf("PASS actual_combined_callback_cases=%u tiny_restore_db=-650,-650 tiny_restore_raw_pan=-100,100 all4physicalcoeff_zero=true alias14out2_equivalent=true no_hardware=true\n",cases);
    return 0;
}
