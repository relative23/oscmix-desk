/* Actual full translation unit and shared bounded sink; no callback copies. */
#define main unused_bulk_probe_main
#include "mix_bulk.c"
#undef main
#include <fenv.h>

static unsigned direct_count, callback_count, null_count, mono_count, endpoint_count,
    confirm_count;

/* Independent double-precision matrix oracle: p scales one output row;
 * width is diagonal/cross energy, with the row sums recovering its gain.
 * This is an expected mathematical state, never hardware readback. */
static void expected_matrix(double gain, int pan, int width, double m[2][2])
{
    double left = pan > 0 ? 1.0 - pan / 100.0 : 1.0;
    double right = pan < 0 ? 1.0 + pan / 100.0 : 1.0;
    double diagonal = gain * (100.0 + width) / 200.0;
    double cross = gain * (100.0 - width) / 200.0;
    m[0][0] = left * diagonal; m[0][1] = left * cross;
    m[1][0] = right * cross; m[1][1] = right * diagonal;
}
static void finite_matrix(void)
{
    assert(!(fetestexcept(FE_INVALID | FE_DIVBYZERO)));
    for (int o = 0; o < 20; o++) for (int i = 0; i < 40; i++)
        assert(isfinite(matrix[o][i]) && matrix[o][i] >= 0 && matrix[o][i] <= 2);
}
static void same_gain(double actual, double expected)
{
    assert(isfinite(actual));
    if (fabs(actual - expected) > fmax(1e-12, fabs(expected) * 1e-5))
        fprintf(stderr, "gain actual=%.12g expected=%.12g direct=%u callback=%u null=%u mono=%u\n", actual, expected, direct_count, callback_count, null_count, mono_count);
    assert(fabs(actual - expected) <= fmax(1e-12, fabs(expected) * 1e-5));
}
static void fresh(int stereo_output, int width)
{
    initialize(0, 0, true, stereo_output, false, width);
    /* Link observations remain absent, so these mathematical feedback cases
     * do not claim or emit a complete device-state OSC observation. */
    feclearexcept(FE_ALL_EXCEPT);
}
static void direct_roundtrip(void)
{
    const int widths[] = {-100, -75, -50, -25, 0, 25, 50, 75, 100};
    const double dbs[] = {-64.9, -60, -40, -20, 0, 6};
    for (unsigned w = 0; w < sizeof widths / sizeof *widths; w++)
        for (unsigned d = 0; d < sizeof dbs / sizeof *dbs; d++)
            for (int pan = -100; pan <= 100; pan++) {
                fresh(true, widths[w]);
                double gain = pow(10.0, dbs[d] / 20.0), expected[2][2];
                expected_matrix(gain, pan, widths[w], expected);
                struct level wanted = {.vol = (float)gain, .pan = pan, .width = widths[w]};
                struct level got = {.width = -777};
                setlevel(outputs, inputs, true, &wanted);
                calclevel(outputs, inputs, true, &got);
                finite_matrix(); same_gain(got.vol, gain);
                assert(got.pan == pan && got.width == widths[w]);
                for (int o = 0; o < 2; o++) for (int i = 0; i < 2; i++)
                    same_gain(matrix[o][i], expected[o][i]);
                ++direct_count;
            }
}
static int encode_db(double gain)
{
    return gain == 0 ? -650 : (int)lround(200.0 * log10(gain));
}
static int encode_pan(double left, double right)
{
    if (left == 0 && right == 0) return 0;
    return (int)lround(atan2(right, left) * 400.0 / PI - 100.0);
}
static void decode_column(int db, int pan, double *left, double *right)
{
    double gain = db <= -650 ? 0 : pow(10.0, db / 200.0);
    if (pan == -100) { *left = gain; *right = 0; }
    else if (pan == 100) { *left = 0; *right = gain; }
    else {
        double angle = (pan + 100) * PI / 400.0;
        *left = cos(angle) * gain; *right = sin(angle) * gain;
    }
}
static void expected_level(const double m[2][2], double *gain, int *pan, int *width)
{
    double left = m[0][0] + m[0][1], right = m[1][0] + m[1][1];
    *gain = fmax(left, right);
    if (*gain == 0) { *pan = 0; *width = 100; return; }
    *pan = (int)lround(100.0 * (right - left) / *gain);
    /* Width is the signed contrast of the stronger output row. */
    *width = left >= right ? (int)lround(100.0 * (m[0][0] - m[0][1]) / left)
                          : (int)lround(100.0 * (m[1][1] - m[1][0]) / right);
}
static void callback_roundtrip(void)
{
    const int widths[] = {-100, 0, 100}, pans[] = {-100, -99, -1, 0, 1, 99, 100};
    const double dbs[] = {-INFINITY, -65, -64.99, -64.9, -60, -20, 0, 6};
    char address[80];
    for (unsigned w = 0; w < 3; w++) for (unsigned p = 0; p < 7; p++)
        for (unsigned d = 0; d < 8; d++)
            for (int a = 0; a < 4; a++) for (int b = 0; b < 4; b++)
                for (int c = 0; c < 4; c++) for (int e = 0; e < 4; e++) {
                    if (a == b || a == c || a == e || b == c || b == e || c == e) continue;
                    fresh(true, widths[w]);
                    double want[2][2], expected[2][2];
                    expected_matrix(isinf(dbs[d]) ? 0 : pow(10.0, dbs[d] / 20.0), pans[p], widths[w], want);
                    /* Warm cache 0.37 dB from the reports, so every DB report is
                     * another client's: feedback order must not invent a replacement
                     * cold baseline. Reports that confirm the cache are below. */
                    struct level wanted = {.vol = isinf(dbs[d]) ? 0.25f
                        : powf(10.f, (dbs[d] - 0.37) / 20.f), .pan = pans[p], .width = widths[w]};
                    setlevel(outputs, inputs, true, &wanted); finite_matrix();
                    int db[2], pan[2], values[4], order[] = {a, b, c, e};
                    for (int i = 0; i < 2; i++) {
                        db[i] = encode_db(hypot(want[0][i], want[1][i]));
                        pan[i] = encode_pan(want[0][i], want[1][i]);
                        values[2*i] = db[i] & 0x7fff;
                        values[2*i+1] = (pan[i] & 0x7fff) | 0x8000;
                        decode_column(db[i], pan[i], &expected[0][i], &expected[1][i]);
                    }
                    for (int j = 0; j < 4; j++) {
                        calls = words = frames_max = 0;
                        struct context ctx = {.param = {.in = order[j] / 2, .out = 0},
                            .addr = address, .addrend = address + sizeof address};
                        newmix(&ctx, values[order[j]]);
                        finite_matrix();
                        assert(inputs[0].width >= -100 && inputs[0].width <= 100);
                    }
                    double gain; int expected_pan, expected_width;
                    expected_level(expected, &gain, &expected_pan, &expected_width);
                    struct level got = {.width = -777};
                    calclevel(outputs, inputs, true, &got);
                    finite_matrix(); same_gain(got.vol, gain);
                    assert(abs(got.pan - expected_pan) <= 1 && abs(got.width - expected_width) <= 1);
                    assert(inputs[0].width == got.width);
                    if (gain == 0) assert(got.pan == 0 && got.width == 100);
                    for (int o = 0; o < 2; o++) for (int i = 0; i < 2; i++)
                        same_gain(matrix[o][i], expected[o][i]);
                    ++callback_count;
                }
}
/* Reports carrying exactly what setmix publishes from the cache confirm it:
 * no coefficient is written back, and the cache keeps its exact values
 * instead of the report's 0.1 dB (the refresh echo, 800 words per refresh).
 * Levels stay above -65 dB per channel: a report at the floor or below still
 * makes the cell silent, as pan_restore checks. */
static void confirmed_reports(void)
{
    const int widths[] = {-100, 0, 100}, pans[] = {-100, -1, 0, 1, 100};
    const double dbs[] = {-INFINITY, -40, -20.04, -3.05, 0, 5.97};
    char address[80];
    for (unsigned w = 0; w < 3; w++) for (unsigned p = 0; p < 5; p++)
        for (unsigned d = 0; d < 6; d++) for (int reverse = 0; reverse < 2; reverse++) {
            fresh(true, widths[w]);
            struct level wanted = {.vol = isinf(dbs[d]) ? 0 : powf(10.f, (float)dbs[d] / 20.f),
                .pan = pans[p], .width = widths[w]};
            setlevel(outputs, inputs, true, &wanted); finite_matrix();
            float before[2][2];
            memcpy(before[0], matrix[0], 2 * sizeof(float));
            memcpy(before[1], matrix[1], 2 * sizeof(float));
            int values[4];
            for (int i = 0; i < 2; i++) {
                struct level own;
                calclevel(outputs, inputs + i, false, &own);
                values[2*i] = dbvalue(own.vol > 0 ? 20.f * log10f(own.vol) : -INFINITY);
                values[2*i+1] = panvalue(own.pan);
            }
            calls = words = frames_max = 0;
            for (int j = 0; j < 4; j++) {
                int k = reverse ? 3 - j : j;
                struct context ctx = {.param = {.in = k / 2, .out = 0},
                    .addr = address, .addrend = address + sizeof address};
                newmix(&ctx, values[k]);
                finite_matrix();
            }
            assert(calls == 0 && words == 0);
            assert(!memcmp(before[0], matrix[0], sizeof before[0])
                && !memcmp(before[1], matrix[1], sizeof before[1]));
            ++confirm_count;
        }
}
static void zero_and_endpoints(void)
{
    for (int pan = -100; pan <= 100; pan++) {
        fresh(true, 100);
        struct level zero = {.vol = 0, .pan = pan, .width = 100}, got = {.width = -777};
        setlevel(outputs, inputs, false, &zero);
        calclevel(outputs, inputs, false, &got);
        finite_matrix(); assert(got.vol == 0 && got.pan == 0);
        calclevel(outputs, inputs, true, &got);
        assert(got.vol == 0 && got.pan == 0 && got.width == 100);
        ++null_count;
    }
    for (int pan = -100; pan <= 100; pan += 200) {
        fresh(true, 100);
        struct level level = {.vol = 0.001f, .pan = pan, .width = 100};
        setlevel(outputs, inputs, false, &level); finite_matrix();
        assert(matrix[pan == 100 ? 0 : 1][0] == 0);
        assert(matrix[pan == 100 ? 1 : 0][0] == level.vol);
        ++endpoint_count;
    }
}
static void mono_output_preserves_width(void)
{
    char address[80];
    for (int width = -100; width <= 100; width++) for (int alias = 0; alias < 2; alias++) {
        fresh(false, width);
        struct context ctx = {.param = {.in = alias, .out = 0},
            .addr = address, .addrend = address + sizeof address};
        newmix(&ctx, -600 & 0x7fff);
        calls = words = frames_max = 0;
        newmix(&ctx, 0x8000);
        finite_matrix();
        assert(inputs[0].width == width && inputs[1].width == width);
        ++mono_count;
    }
}
int main(void)
{
    direct_roundtrip(); callback_roundtrip(); confirmed_reports(); zero_and_endpoints();
    mono_output_preserves_width();
    unsigned total = direct_count + callback_count + null_count + mono_count + endpoint_count;
    assert(direct_count == 10854 && callback_count == 4032 && null_count == 201 && mono_count == 402 && endpoint_count == 2);
    assert(confirm_count == 180);
    printf("PASS actual_geometry_cases=%u direct=%u callback=%u null=%u mono_width_preservation=%u endpoints=%u confirmed_reports=%u\n",
        total, direct_count, callback_count, null_count, mono_count, endpoint_count, confirm_count);
    return 0;
}
