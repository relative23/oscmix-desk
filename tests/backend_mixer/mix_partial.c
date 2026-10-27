#define FIXTURE_PARTIAL
#define main unused_value_main
#include "mix_bulk.c"
#undef main
int main(void)
{
    unsigned count=0;
    for(unsigned k=0;k<=8;k++){
        initialize(12,0,true,true,false,100);fail_after_words=k;
        if(!setjmp(partial_jump)){submit(0,12,0,-60.f,1);assert(!"transport failure returned");}
        assert(calls==1 && words==k && frames_max==47);
        assert(matrix[0][12]>0 && matrix[1][13]>0);
        assert(matrix[1][12]==0 && matrix[0][13]==0);
        assert(inputs[12].width==100 && !inputs[12].mute && !inputs[13].mute);
        ++count;
    }
    printf("PASS partial_transport_cases=%u cache_is_desired_not_readback=true no_automatic_retry=true\n",count);
    return 0;
}
