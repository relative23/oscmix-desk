/* Exact combined backend source, actual handleosc/setter; only transport sinks
 * are replaced. No process, socket, MIDI, device or service access. */
#include "oscmix.c"
extern const struct device ffucxii;
static struct input input_store[40], original_inputs[40];
static struct output output_store[20], original_outputs[20];
static float matrix[20][40], original_matrix[20][40];
static unsigned char observations[20][20];
static unsigned frames, cases;

void writemidi(const void *ptr, size_t len)
{
    const unsigned char *p=ptr;
    assert(len==207 && p[0]==0xf0 && p[206]==0xf7);
    ++frames;
}
void writeosc(const void *ptr, size_t len) { assert(ptr && len>0); }
void oscsource(unsigned value) { (void)value; assert(!"unexpected source hook"); }

static void initialize(int pattern)
{
    memset(input_store,0xa5,sizeof input_store);
    memset(output_store,0x5a,sizeof output_store);
    memset(observations,0x35,sizeof observations);
    for(int i=0;i<40;i++) {
        input_store[i].stereo=pattern ? (i%2)==0 : true;
        if(i>=20)input_store[i].stereo=true;
        input_store[i].stereo_observed=true;
        input_store[i].mute=i<20 && (i%2);
        input_store[i].width=i<20 ? i-10 : 100;
    }
    for(int o=0;o<20;o++) {
        output_store[o].stereo=(o%2)==0;
        output_store[o].stereo_observed=true;
        output_store[o].mix=matrix[o];output_store[o].mix_observed=observations[o];
        for(int i=0;i<40;i++)matrix[o][i]=(o+i)%3 ? 0.f : .125f;
    }
    memcpy(original_inputs,input_store,sizeof input_store);
    memcpy(original_outputs,output_store,sizeof output_store);
    memcpy(original_matrix,matrix,sizeof matrix);
    inputs=input_store;outputs=output_store;device=&ffucxii;
}

static void command(int left,int channel,int value)
{
    unsigned char raw[128];char path[64];
    snprintf(path,sizeof path,"/playback/%d/mute",channel);
    struct oscmsg msg={.buf=raw,.end=raw+sizeof raw};
    oscputstr(&msg,path);oscputstr(&msg,",i");oscputint(&msg,value);
    unsigned prior=frames;
    assert(handleosc(raw,(size_t)(msg.buf-raw))==0);
    assert(frames==prior+1 && !memcmp(matrix,original_matrix,sizeof matrix));
    struct input wanted_inputs[40];struct output wanted_outputs[20];
    memcpy(wanted_inputs,original_inputs,sizeof wanted_inputs);
    memcpy(wanted_outputs,original_outputs,sizeof wanted_outputs);
    for(int i=0;i<20;i++) {
        wanted_inputs[i].stereo_observed=false;wanted_outputs[i].stereo_observed=false;
        for(int j=0;j<20;j++)assert(observations[i][j]==0);
    }
    wanted_inputs[19+left].mute=value;wanted_inputs[20+left].mute=value;
    assert(!memcmp(input_store,wanted_inputs,sizeof input_store));
    assert(!memcmp(output_store,wanted_outputs,sizeof output_store));
    ++cases;
}

int main(void)
{
    assert(ffucxii.inputslen==20 && ffucxii.outputslen==20);
    for(int pattern=0;pattern<2;pattern++)for(int left=1;left<21;left+=2) {
        initialize(pattern);
        command(left,left,1);
        command(left,left,1);
        command(left,left,0);
        command(left,left+1,0);
        command(left,left+1,0);
    }
    assert(cases==100 && frames==100);
    printf("PASS actual_observation_lifecycle_cases=%u frames=%u\n",cases,frames);
    return 0;
}
