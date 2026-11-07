/* Compile the complete unchanged/candidate oscmix.c, including actual callbacks.
 * Only transport sinks are substituted. No real socket, MIDI or device access. */
#ifndef BACKEND_SOURCE
#define BACKEND_SOURCE "oscmix.c"
#endif
#include BACKEND_SOURCE
#include <setjmp.h>
#include <time.h>

extern const struct device ffucxii;
static struct input input_store[40], initial_inputs[40];
static struct output output_store[20];
static float matrix[20][40], saved_matrix[20][40];
static unsigned char observations[20][20], saved_observations[20][20];
static uint16_t hardware[0x8000], expected_hardware[0x8000];
static bool seen[0x8000];
static unsigned calls, wordcount, osccalls, frame_max;
static unsigned captured_reg[40], captured_val[40];
static unsigned char frames[50][207];
static size_t frame_lengths[50];
static bool check_prior_flags, simulate_failure;
static jmp_buf failure_jump;
static struct device device_copy;
static int bad_map;
static unsigned cases, malformed, physical_cases, alias_cases, helper_cases;

/* Independent decoder: no base128dec, getle32_7bit, regword or mixlevelvalue. */
static uint32_t decode_word(const unsigned char *p)
{
    uint32_t v=0;
    for(unsigned b=0;b<5;b++) {
        assert(p[b]<128);
        if(b==4) assert(p[b]<16);
        for(unsigned k=0;k<7 && b*7+k<32;k++)
            if(p[b] & (1u<<k)) v |= UINT32_C(1)<<(b*7+k);
    }
    unsigned ones=0;
    for(unsigned k=0;k<32;k++) ones += (v>>k)&1u;
    assert(ones%2==1);
    return v;
}
/* Independent reference to the device's coefficient word (TotalMix FX on
 * Windows, 2026-11-06): bits 0-14 a 15-bit two's complement, bit 15 a x8
 * range flag. 0x4000 would read as -0.5, so 0.5 itself takes the x8 form. */
static unsigned encoded_coefficient(float value)
{
    assert(isfinite(value) && value>=0 && value<=2);
    double scaled=(double)value*32768.0;
    unsigned fixed=(unsigned)floor(scaled+0.5);
    return fixed<16384 ? fixed : 0x8000u|fixed/8;
}
static unsigned expected_reg(int input, int output)
{
    /* UCX2 MIX_LEVEL: playback begins at wire-column32, physical at0. */
    unsigned col=(unsigned)input;
    if(input>=device->inputslen) col=32u+(unsigned)(input-device->inputslen);
    return 0x4000u+(unsigned)output*64u+col;
}
void writemidi(const void *ptr, size_t len)
{
    const unsigned char *bytes=ptr;
    assert(calls<50 && len<=207 && len>=7 && (len-7)%5==0);
    if(check_prior_flags)
        assert(!memcmp(input_store,initial_inputs,sizeof input_store));
    memcpy(frames[calls],bytes,len); frame_lengths[calls]=len; ++calls;
    if(len>frame_max)frame_max=(unsigned)len;
    if(simulate_failure) longjmp(failure_jump,1);
    assert(bytes[0]==0xf0 && bytes[1]==0 && bytes[2]==0x20 && bytes[3]==0x0d);
    assert(bytes[4]==0x10 && bytes[5]==0 && bytes[len-1]==0xf7);
    for(size_t offset=6;offset<len-1;offset+=5) {
        uint32_t v=decode_word(bytes+offset);
        unsigned reg=(v>>16)&0x7fff, val=v&0xffff;
        assert(wordcount<40 && !seen[reg]);
        seen[reg]=true;
        captured_reg[wordcount]=reg;captured_val[wordcount++]=val;
        hardware[reg]=(uint16_t)val;
    }
}
void writeosc(const void *ptr, size_t len)
{
    assert(ptr && len>0); ++osccalls;
}
/* This is not used by retained callbacks; satisfy the whole-source link. */
void oscsource(unsigned value) { (void)value; assert(!"unexpected OSC source call"); }
static int mapped(enum control ctl,const struct param *p)
{
    if(ctl==MIX_LEVEL && p->out==device->outputslen-1) {
        if(bad_map==1)return -1;
        if(bad_map==2)return 0x8000;
        if(bad_map==3)return expected_reg(p->in,0);
    }
    return ffucxii.ctltoreg(ctl,p);
}
/* Synthetic18-channel mapping checks buffer dimensions, not FF802 support.
 * Actual UCX2 tests always use its independently compiled ctltoreg. */
static int synthetic18_map(enum control ctl,const struct param *p)
{
    if(ctl==UNKNOWN)return -1;
    assert(ctl==MIX_LEVEL && p->in>=0 && p->in<36 && p->out>=0 && p->out<18);
    return (int)expected_reg(p->in,p->out);
}
static void reset(int count,int matrix_kind)
{
    memset(input_store,0,sizeof input_store);
    memset(output_store,0,sizeof output_store);
    memset(observations,0x35,sizeof observations);
    memset(matrix,0,sizeof matrix);
    for(int o=0;o<20;o++)for(int i=0;i<40;i++) {
        static const float values[]={0.f,0x1p-16f,0.125f,0.5f,0x1.0002p-1f,1.f,2.f};
        matrix[o][i]=matrix_kind==0 ? 0.f : matrix_kind==1 ?
            ((o*3+i)%7==0 ? 1.f : 0.f) : values[(o*3+i)%7];
    }
    for(int i=0;i<40;i++){ input_store[i].stereo_observed=(i&1)!=0; input_store[i].width=100-i; }
    for(int o=0;o<20;o++) {
        output_store[o].mix=matrix[o];output_store[o].mix_observed=observations[o];
        output_store[o].stereo=(o&1)!=0;output_store[o].stereo_observed=(o&1)==0;
    }
    inputs=input_store;outputs=output_store;
    device_copy=ffucxii;device_copy.inputslen=count;device_copy.outputslen=count;
    if(count==18)device_copy.ctltoreg=synthetic18_map;
    device=&device_copy;bad_map=0;
    calls=wordcount=osccalls=0;frame_max=0;
    memset(seen,0,sizeof seen);
    for(unsigned r=0;r<0x8000;r++)hardware[r]=expected_hardware[r]=(uint16_t)(0x0100+(r%0x3000));
    check_prior_flags=false;simulate_failure=false;
}
static void save_cache(void)
{
    memcpy(saved_matrix,matrix,sizeof matrix);
    memcpy(saved_observations,observations,sizeof observations);
    memcpy(initial_inputs,input_store,sizeof input_store);
}
static void unchanged_cache(void)
{
    assert(!memcmp(saved_matrix,matrix,sizeof matrix));
    assert(!memcmp(saved_observations,observations,sizeof observations));
}
static const struct node *mute_node(bool physical)
{
    for(const struct node *top=roottree;top->name;top++)
        if(!strcmp(top->name,physical?"input":"playback"))
            for(const struct node *leaf=top->tree;leaf->name;leaf++)
                if(!strcmp(leaf->name,"mute")) {
                    assert(leaf->set==setinputmute && leaf->ctl==(physical?INPUT_MUTE:UNKNOWN));
                    return leaf;
                }
    assert(!"actual mute node missing");return NULL;
}
static int command(int absolute_input,int value,bool physical)
{
    unsigned char raw[4];putbe32(raw,(unsigned)value);
    struct oscmsg msg={.buf=raw,.end=raw+4,.type="i"};
    struct context ctx={.node=mute_node(physical),
        .param={.in=absolute_input,.out=-1},.exact=true,.addr="/input/1/mute"};
    ctx.node->set(&ctx,&msg);
    assert(msg.buf==msg.end && !*msg.type);
    return msg.err ? -1 : 0;
}
static void setup_flags(int count,int channel,bool linked,int own,int partner)
{
    for(int i=0;i<count;i++)input_store[count+i].stereo=linked;
    int base=channel&~1;
    input_store[count+channel].mute=own;
    input_store[count+base+(channel==base ? 1 : 0)].mute=partner;
}
static void assert_target_flags(int count,int channel,bool linked,bool desired)
{
    int first=linked ? channel&~1 : channel;
    int width=linked ? 2 : 1;
    for(int i=0;i<40;i++) {
        struct input expected=initial_inputs[i];
        if(i>=count+first && i<count+first+width)expected.mute=desired;
        assert(!memcmp(&input_store[i],&expected,sizeof expected));
    }
}
static void playback_suite(void)
{
    const int counts[]={20,18};
    for(unsigned c=0;c<2;c++) {
        int count=counts[c];
        for(int channel=0;channel<count;channel++)for(int linked=0;linked<2;linked++)
            for(int own=0;own<2;own++)for(int partner=0;partner<2;partner++)
            for(int desired=0;desired<2;desired++)for(int kind=0;kind<3;kind++) {
                reset(count,kind);setup_flags(count,channel,linked,own,partner);save_cache();
                int first=linked ? channel&~1 : channel,width=linked?2:1;
                for(int o=0;o<count;o++)for(int i=0;i<width;i++) {
                    int absolute=count+first+i;
                    expected_hardware[expected_reg(absolute,o)]=(uint16_t)
                        encoded_coefficient(desired ? 0 : matrix[o][absolute]);
                }
                check_prior_flags=true;assert(command(count+channel,desired,false)==0);
                assert(calls==1 && wordcount==(unsigned)(count*width));
                assert(frame_lengths[0]==7u+5u*count*width && osccalls==0);
                assert(!memcmp(hardware,expected_hardware,sizeof hardware));
                unsigned w=0;
                for(int o=0;o<count;o++)for(int i=0;i<width;i++,w++) {
                    assert(captured_reg[w]==expected_reg(count+first+i,o));
                    assert(captured_val[w]==encoded_coefficient(desired ? 0 : matrix[o][count+first+i]));
                }
                unchanged_cache();assert_target_flags(count,channel,linked,desired);
                ++cases;if(linked && (channel&1))++alias_cases;
            }
    }
}

#ifndef ORIGINAL_WITNESS
static void reject_suite(void)
{
    const float bad[]={NAN,INFINITY,-INFINITY,-0.00001f,2.00001f};
    for(int channel=0;channel<20;channel++)for(int linked=0;linked<2;linked++)
        for(int desired=0;desired<2;desired++)for(unsigned b=0;b<5;b++) {
            reset(20,2);setup_flags(20,channel,linked,!desired,desired);
            int first=linked ? channel&~1 : channel;
            int width=linked ? 2 : 1;
            matrix[19][20+first+width-1]=bad[b];save_cache();
            assert(command(20+channel,desired,false)==-1);
            assert(calls==0 && wordcount==0 && osccalls==0);
            unchanged_cache();assert(!memcmp(input_store,initial_inputs,sizeof input_store));++malformed;
        }
    for(int channel=0;channel<20;channel++)for(int desired=0;desired<2;desired++)
        for(int failure=1;failure<4;failure++) {
            reset(20,1);setup_flags(20,channel,true,!desired,desired);
            device_copy.ctltoreg=mapped;bad_map=failure;save_cache();assert(command(20+channel,desired,false)==-1);
            assert(calls==0 && !memcmp(input_store,initial_inputs,sizeof input_store));unchanged_cache();++malformed;
        }
    for(int mode=0;mode<8;mode++) {
        reset(20,1);setup_flags(20,0,true,false,false);
        if(mode==0)device_copy.outputslen=21;
        if(mode==1)device_copy.outputslen=0;
        if(mode==2)device_copy.inputslen=0;
        if(mode==3)device_copy.inputslen=21;
        if(mode==4)output_store[19].mix=NULL;
        if(mode==5)input_store[20].stereo=false; /* linked-even alias has invalid odd */
        if(mode==6)input_store[21].stereo=false; /* linked-odd has invalid even */
        if(mode==7)device_copy.outputslen=19; /* linked last odd has no partner */
        save_cache();
        if(mode==7)muteinput(&input_store[38],true);
        else if(mode==5)muteinput(&input_store[21],true);
        else if(mode==2 || mode==3)muteplayback(&input_store[20],true);
        else muteinput(&input_store[20],true);
        assert(calls==0 && !memcmp(input_store,initial_inputs,sizeof input_store));unchanged_cache();++malformed;
    }
    reset(20,1);device_copy.ctltoreg=NULL;save_cache();muteinput(&input_store[20],true);
    assert(calls==0 && !memcmp(input_store,initial_inputs,sizeof input_store));unchanged_cache();++malformed;
    for(int desired=0;desired<2;desired++) {
        reset(20,2);setup_flags(20,1,true,!desired,!desired);save_cache();
        simulate_failure=true;check_prior_flags=true;
        if(!setjmp(failure_jump)) { command(21,desired,false);assert(!"transport failure returned"); }
        assert(calls==1 && !memcmp(input_store,initial_inputs,sizeof input_store));unchanged_cache();++malformed;
    }
    /* Actual callback malformed OSC must not send. */
    for(int kind=0;kind<3;kind++) {
        reset(20,2);save_cache();unsigned char raw[8]={0};
        struct oscmsg msg={.buf=raw,.end=raw+(kind==0?3:kind==1?4:8),.type=kind==1?"f":"i"};
        struct context ctx={.node=mute_node(false),.param={.in=20,.out=-1}};
        setinputmute(&ctx,&msg);assert(msg.err && calls==0);unchanged_cache();++malformed;
    }
}
#endif

static void physical_suite(void)
{
    /* Expected legacy behavior includes early equality, canonicalization after
     * equality, only strictly positive cache cells, and one write per cell. */
    for(int channel=0;channel<20;channel++)for(int linked=0;linked<2;linked++)
        for(int own=0;own<2;own++)for(int partner=0;partner<2;partner++)
        for(int desired=0;desired<2;desired++)for(int kind=0;kind<3;kind++) {
            reset(20,kind);
            for(int i=0;i<20;i++)input_store[i].stereo=linked;
            int first=linked ? channel&~1 : channel,width=linked?2:1;
            input_store[channel].mute=own;
            input_store[(channel&~1)+(channel%2?0:1)].mute=partner;
            save_cache();check_prior_flags=false;
            unsigned needed=0;
            if(own!=desired)for(int o=0;o<20;o++)for(int i=0;i<width;i++)
                if(matrix[o][first+i]>0) {
                    expected_hardware[expected_reg(first+i,o)]=(uint16_t)
                        encoded_coefficient(desired?0:matrix[o][first+i]);++needed;
                }
            muteinput(&input_store[channel],desired);
            assert(calls==needed && wordcount==needed && !memcmp(hardware,expected_hardware,sizeof hardware));
            unchanged_cache();
            for(int i=0;i<40;i++) {
                struct input expected=initial_inputs[i];
                if(own!=desired && i>=first && i<first+width)expected.mute=desired;
                assert(!memcmp(&input_store[i],&expected,sizeof expected));
            }
            /* Actual newinputmute uses only the unchanged physical branch. */
            calls=wordcount=osccalls=0;memset(seen,0,sizeof seen);
            struct context ctx={.param={.in=channel,.out=-1},.exact=false};
            newinputmute(&ctx,desired);assert(calls==0 && osccalls==0);
            ++physical_cases;
        }
    /* Actual physical callback keeps its independent INPUT_MUTE register first. */
    reset(20,0);save_cache();check_prior_flags=true;
    command(3,1,true);assert(calls==1 && wordcount==1);
    assert(captured_reg[0]==(3u<<6) && captured_val[0]==1 && input_store[3].mute);
    unchanged_cache();++physical_cases;
}
#ifndef ORIGINAL_WITNESS
static size_t packet(unsigned char *raw,const char *path,const char *type,int value)
{
    struct oscmsg msg={.buf=raw,.end=raw+256,.type=NULL};
    oscputstr(&msg,path);oscputstr(&msg,type);oscputint(&msg,value);
    assert(!msg.err);return (size_t)(msg.buf-raw);
}
static void whole_packet_suite(void)
{
    for(int channel=0;channel<20;channel++)for(int linked=0;linked<2;linked++)
        for(int desired=0;desired<2;desired++)for(int invalid=0;invalid<2;invalid++) {
            reset(20,2);setup_flags(20,channel,linked,!desired,desired);
            if(invalid)matrix[19][20+(linked ? channel&~1 : channel)]=NAN;
            unsigned char raw[256];char path[80];
            snprintf(path,sizeof path,"/playback/%d/mute",channel+1);
            size_t len=packet(raw,path,",i",desired);
            int result=handleosc(raw,len);
            assert(result==(invalid?-1:0));
            assert(calls==(invalid?0u:1u));
            assert(input_store[20+channel].mute==(invalid ? !desired : desired));
            ++helper_cases;
        }
    reset(20,0);unsigned char raw[256];
    size_t len=packet(raw,"/playback/1/mute",",f",0);
    assert(handleosc(raw,len)==-1 && calls==0);++helper_cases;
    /* A non-playback setter error is now reflected in the shared return value. */
    len=packet(raw,"/input/1/mute",",f",0);
    assert(handleosc(raw,len)==-1 && calls==0);++helper_cases;
}
#endif
#ifndef ORIGINAL_WITNESS
static void staged_roundtrip_suite(void)
{
    for(int channel=0;channel<20;channel++)for(int linked=0;linked<2;linked++) {
        reset(20,0);setup_flags(20,channel,linked,0,0);save_cache();
        assert(command(20+channel,1,false)==0 && calls==1);
        int first=linked ? channel&~1 : channel, width=linked?2:1;
        for(int o=0;o<20;o++)for(int i=0;i<width;i++)
            assert(hardware[expected_reg(20+first+i,o)]==0);
        /* Actual cached mix planner stages both positive and exactzero rows
         * while ON; no physical matrix write occurs in setlevel. */
        calls=wordcount=0;memset(seen,0,sizeof seen);
        for(int o=0;o<20;o++) {
            output_store[o].stereo=false;
            struct level level={.vol=o%3?0.125f:0.f,.pan=0,.width=100};
            setlevel(&output_store[o],&input_store[20+first],linked,&level);
        }
        assert(calls==0 && input_store[20+first].mute);
        /* A signedzero bit pattern is retained, while the emitted coefficient
         * is the same physicalzero as positivezero. */
        matrix[19][20+first]=-0.f;save_cache();
        assert(command(20+channel,0,false)==0 && calls==1);
        for(int o=0;o<20;o++)for(int i=0;i<width;i++)
            assert(hardware[expected_reg(20+first+i,o)]==encoded_coefficient(matrix[o][20+first+i]));
        unchanged_cache();assert_target_flags(20,channel,linked,false);++helper_cases;
    }
}
#endif
static void scalar_encoding_suite(void)
{
    reset(20,0);
    /* All 65537 uncompressed fixed values exercise shared compression,
     * parity and bit framing through actual setmixlevel + setreg. */
    for(unsigned q=0;q<=65536;q++) {
        calls=wordcount=0;memset(seen,0,sizeof seen);
        float value=(float)q/32768.f;
        setmixlevel(&input_store[20],&output_store[0],value);
        assert(calls==1 && wordcount==1 && frame_lengths[0]==12);
        assert(captured_reg[0]==0x4020 && captured_val[0]==encoded_coefficient(value));++helper_cases;
    }
    /* Generic 0-byte SysEx is byte-identical to the original timer request. */
    unsigned char frame[7];calls=0;simulate_failure=true;
    if(!setjmp(failure_jump)){writesysex(2,NULL,0,frame);assert(!"sink did not jump");}
    const unsigned char expected[]={0xf0,0,0x20,0x0d,0x10,2,0xf7};
    assert(frame_lengths[0]==7 && !memcmp(frames[0],expected,7));++helper_cases;
    simulate_failure=false;
}
#ifdef ORIGINAL_WITNESS
static void original_witness(void)
{
    reset(20,0);setup_flags(20,0,true,0,0);save_cache();
    command(20,1,false);
    assert(calls==0 && hardware[0x4020]>0 && input_store[20].mute);
    printf("original hidden-positive zero-cache ON: midi_calls=%u physical_4020=%u flag=%d\n",calls,hardware[0x4020],input_store[20].mute);
    calls=wordcount=0;memset(seen,0,sizeof seen);command(20,1,false);
    assert(calls==0 && hardware[0x4020]>0);
    printf("original repeated ON: midi_calls=%u hidden physical coefficient unchanged\n",calls);
    calls=wordcount=0;memset(seen,0,sizeof seen);command(20,0,false);
    assert(calls==0 && hardware[0x4020]>0);
    printf("original zero-cache OFF: midi_calls=%u hidden physical coefficient unchanged\n",calls);
    reset(20,2);setup_flags(20,1,true,0,0);save_cache();command(21,1,false);
    unsigned positives=0;for(int o=0;o<20;o++)for(int i=0;i<2;i++)positives+=matrix[o][20+i]>0;
    assert(calls==positives && calls>1);
    printf("original linked ON: midi_calls=%u, independently timed writemidi calls per positive cell\n",calls);
}
#endif
int main(void)
{
#ifdef ORIGINAL_WITNESS
    original_witness();physical_suite();scalar_encoding_suite();
    printf("PASS original physical=%u scalar_encoding=%u total=%u\n",physical_cases,helper_cases,physical_cases+helper_cases);
#else
    playback_suite();reject_suite();physical_suite();scalar_encoding_suite();whole_packet_suite();staged_roundtrip_suite();
    printf("PASS playback=%u even_alias=%u malformed_or_failed=%u physical=%u scalar_encoding=%u max_bulk_bytes=207 total=%u\n",cases,alias_cases,malformed,physical_cases,helper_cases,cases+malformed+physical_cases+helper_cases);
#endif
    return 0;
}
