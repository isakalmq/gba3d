// shadowtest: a homebrew GBA ROM that exercises the display features the
// gba-shadow renderer has to reproduce. It cycles through 7 scenes (one per
// video mode / feature group), each lasting SCENE_LEN frames.
//
// Nothing here is game-like; it exists so the renderer can be checked
// pixel-for-pixel against mGBA without needing a commercial ROM.

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef signed short s16;
typedef signed int s32;

#define REG16(a) (*(volatile u16*)(a))
#define REG32(a) (*(volatile u32*)(a))

#define REG_DISPCNT   REG16(0x04000000)
#define REG_VCOUNT    REG16(0x04000006)
#define REG_BGCNT(n)  REG16(0x04000008 + (n) * 2)
#define REG_BGHOFS(n) REG16(0x04000010 + (n) * 4)
#define REG_BGVOFS(n) REG16(0x04000012 + (n) * 4)
#define REG_BGPA(n)   REG16(0x04000020 + ((n) - 2) * 16)
#define REG_BGPB(n)   REG16(0x04000022 + ((n) - 2) * 16)
#define REG_BGPC(n)   REG16(0x04000024 + ((n) - 2) * 16)
#define REG_BGPD(n)   REG16(0x04000026 + ((n) - 2) * 16)
#define REG_BGX(n)    REG32(0x04000028 + ((n) - 2) * 16)
#define REG_BGY(n)    REG32(0x0400002C + ((n) - 2) * 16)
#define REG_WIN0H     REG16(0x04000040)
#define REG_WIN1H     REG16(0x04000042)
#define REG_WIN0V     REG16(0x04000044)
#define REG_WIN1V     REG16(0x04000046)
#define REG_WININ     REG16(0x04000048)
#define REG_WINOUT    REG16(0x0400004A)
#define REG_MOSAIC    REG16(0x0400004C)
#define REG_BLDCNT    REG16(0x04000050)
#define REG_BLDALPHA  REG16(0x04000052)
#define REG_BLDY      REG16(0x04000054)

#define PAL_BG   ((volatile u16*)0x05000000)
#define PAL_OBJ  ((volatile u16*)0x05000200)
#define VRAM     ((volatile u16*)0x06000000)
#define OAM      ((volatile u16*)0x07000000)

#define RGB15(r, g, b) ((u16)(((r) & 31) | (((g) & 31) << 5) | (((b) & 31) << 10)))

#define DC_MODE(m)     (m)
#define DC_PAGE        0x0010
#define DC_OBJ_1D      0x0040
#define DC_BLANK       0x0080
#define DC_BG(n)       (0x0100 << (n))
#define DC_OBJ         0x1000
#define DC_WIN0        0x2000
#define DC_WIN1        0x4000
#define DC_OBJWIN      0x8000

#define BGCNT(prio, cb, sb, bpp8, size, wrap) \
    ((prio) | ((cb) << 2) | ((bpp8) << 7) | ((sb) << 8) | ((wrap) << 13) | ((size) << 14))

#define SCENE_LEN 240
#define NSCENES 7

// ---------------------------------------------------------------- helpers

// Integer sine, angle 0..255 = full turn, result in 8.8 fixed point.
static s32 isin(s32 a) {
    a &= 255;
    s32 neg = a >= 128;
    s32 p = a & 127;                 // 0..127 maps to 0..pi
    s32 q = p * (128 - p);
    s32 v = (4096 * q) / (81920 - 4 * q);
    return neg ? -v : v;
}
static s32 icos(s32 a) { return isin(a + 64); }

static void vsync(void) {
    while (REG_VCOUNT >= 160) {}
    while (REG_VCOUNT < 160) {}
}

static void vram_fill(u32 byte_off, u32 bytes, u16 v) {
    volatile u16* p = VRAM + byte_off / 2;
    for (u32 i = 0; i < bytes / 2; ++i) p[i] = v;
}

// Write one 8x8 tile from a pixel function. bpp8: 64 bytes, else 32 bytes.
typedef u32 (*pixfn)(u32 n, u32 x, u32 y);
static void put_tile(u32 byte_off, int bpp8, u32 n, pixfn f) {
    volatile u16* p = VRAM + byte_off / 2;
    for (u32 y = 0; y < 8; ++y) {
        if (bpp8) {
            for (u32 x = 0; x < 8; x += 2)
                p[y * 4 + x / 2] = (f(n, x, y) & 255) | ((f(n, x + 1, y) & 255) << 8);
        } else {
            for (u32 x = 0; x < 8; x += 4)
                p[y * 2 + x / 4] = (f(n, x, y) & 15) | ((f(n, x + 1, y) & 15) << 4) |
                                   ((f(n, x + 2, y) & 15) << 8) | ((f(n, x + 3, y) & 15) << 12);
        }
    }
}

// ---------------------------------------------------------------- graphics

static u32 bg4_pix(u32 n, u32 x, u32 y) {
    if (n == 0) return 0;
    switch (n & 3) {
    case 0: return ((x ^ y) + n) & 15;
    case 1: return ((x + y) % 5 == 0) ? 0 : (x + y + n) & 15;
    case 2: return (x == 0 || y == 0 || x == 7 || y == 7) ? ((n & 15) | 1) : 0;
    default: return (x == y || x == 7 - y) ? 15 : ((n + x) & 7);
    }
}
static u32 bg8_pix(u32 n, u32 x, u32 y) {
    if (n == 0) return 0;
    u32 v = (x * 16 + y * 3 + n * 11) & 255;
    return (v % 7 == 0) ? 0 : v;
}
static u32 aff_pix(u32 n, u32 x, u32 y) {
    if (((x + y + n) & 7) == 0) return 0;
    return (n * 4 + x + y * 2) & 255;
}
static u32 obj4_pix(u32 n, u32 x, u32 y) {
    s32 dx = (s32)x * 2 - 7, dy = (s32)y * 2 - 7;
    if (dx * dx + dy * dy > 40 + (s32)(n % 5) * 6) return 0;
    return ((n + x / 2 + y / 2) % 15) + 1;
}
static u32 objlow_pix(u32 n, u32 x, u32 y) {
    (void)x;
    return (y & 1) ? (n % 15) + 1 : 0;
}
static u32 obj8_pix(u32 n, u32 x, u32 y) {
    if (x == y) return 0;
    return ((x + y * 8 + n * 5) & 255) | 1;
}

static void load_palettes(void) {
    for (u32 k = 0; k < 256; ++k) {
        PAL_BG[k] = RGB15(k * 5, k * 3 + 10, 31 - (k & 31));
        PAL_OBJ[k] = RGB15(31 - (k & 31), k * 7, k * 2 + 4);
    }
    PAL_BG[0] = RGB15(2, 4, 10);  // backdrop
}

static void load_obj_tiles(void) {
    // OBJ VRAM starts at 0x10000; tile n lives at 0x10000 + n * 32.
    for (u32 n = 0; n < 64; ++n) put_tile(0x10000 + n * 32, 0, n, objlow_pix);
    for (u32 n = 512; n < 768; ++n) put_tile(0x10000 + n * 32, 0, n, obj4_pix);
    // 8bpp tiles: index counts 32-byte units, each tile uses two of them.
    for (u32 n = 0; n < 64; ++n) put_tile(0x10000 + (768 + n * 2) * 32, 1, n, obj8_pix);
}

static void load_tile_bg_data(void) {
    for (u32 n = 0; n < 128; ++n) put_tile(0x0000 + n * 32, 0, n, bg4_pix);   // cb0, 4bpp
    for (u32 n = 0; n < 128; ++n) put_tile(0x4000 + n * 64, 1, n, bg8_pix);   // cb1, 8bpp
    for (u32 n = 0; n < 64; ++n) put_tile(0x8000 + n * 64, 1, n, aff_pix);    // cb2, affine

    volatile u16* sb;
    // SB24: BG0 text 256x256
    sb = VRAM + (24 * 0x800) / 2;
    for (u32 i = 0; i < 32; ++i)
        for (u32 j = 0; j < 32; ++j)
            sb[i * 32 + j] = (((i * 7 + j * 3) % 127) + 1) | (((i + j) & 1) << 10) |
                             ((((i * j) >> 1) & 1) << 11) | (((i + j) % 16) << 12);
    // SB25-26: BG1 text 512x256, 8bpp
    sb = VRAM + (25 * 0x800) / 2;
    for (u32 b = 0; b < 2; ++b)
        for (u32 i = 0; i < 32; ++i)
            for (u32 j = 0; j < 32; ++j) {
                u32 gj = j + b * 32;
                u32 tile = ((i + gj) % 4 == 0) ? 0 : ((i * 5 + gj) % 127) + 1;
                sb[b * 1024 + i * 32 + j] = tile | ((gj & 1) << 10) | (((i >> 2) & 1) << 11);
            }
    // SB27-28: BG2 text 256x512
    sb = VRAM + (27 * 0x800) / 2;
    for (u32 i = 0; i < 64; ++i)
        for (u32 j = 0; j < 32; ++j)
            sb[i * 32 + j] = (((i + j * 3) % 60) + 1) | ((j % 16) << 12) | ((i & 1) << 11);
    // SB29: BG3 text 256x256, sparse
    sb = VRAM + (29 * 0x800) / 2;
    for (u32 i = 0; i < 32; ++i)
        for (u32 j = 0; j < 32; ++j)
            sb[i * 32 + j] = (i % 6 == 0 || j % 9 == 0) ? (((i + j) % 100) + 1) | ((i % 16) << 12) : 0;
    // SB30: affine map 256x256 (32x32 bytes); SB31: affine map 128x128 (16x16 bytes)
    sb = VRAM + (30 * 0x800) / 2;
    for (u32 k = 0; k < 32 * 32; k += 2) {
        u32 a = ((k / 32) * 3 + (k % 32)) % 64, b = ((k / 32) * 3 + (k % 32) + 1) % 64;
        sb[k / 2] = a | (b << 8);
    }
    sb = VRAM + (31 * 0x800) / 2;
    for (u32 k = 0; k < 16 * 16; k += 2) {
        u32 a = (k * 5) % 64, b = ((k + 1) * 5) % 64;
        sb[k / 2] = a | (b << 8);
    }
}

static void draw_mode3(void) {
    volatile u16* fb = VRAM;
    for (u32 y = 0; y < 160; ++y)
        for (u32 x = 0; x < 240; ++x)
            fb[y * 240 + x] = RGB15(x * 31 / 239, y * 31 / 159, (x ^ y) & 31);
}
static void draw_mode4(void) {
    for (u32 page = 0; page < 2; ++page) {
        volatile u16* fb = VRAM + (page * 0xA000) / 2;
        for (u32 y = 0; y < 160; ++y)
            for (u32 x = 0; x < 240; x += 2) {
                u32 a = (x + y + page * 64) & 255, b = (x + 1 + y + page * 64) & 255;
                if ((x / 8 + y / 8 + page) % 5 == 0) a = b = 0;  // transparent blocks
                fb[(y * 240 + x) / 2] = a | (b << 8);
            }
    }
}
static void draw_mode5(void) {
    for (u32 page = 0; page < 2; ++page) {
        volatile u16* fb = VRAM + (page * 0xA000) / 2;
        for (u32 y = 0; y < 128; ++y)
            for (u32 x = 0; x < 160; ++x)
                fb[y * 160 + x] = page ? RGB15(31 - x / 5, y / 4, 16) : RGB15(x / 5, 31 - y / 4, (x * y) >> 6);
    }
}

// ---------------------------------------------------------------- sprites

// Like most commercial games, sprites are built in a RAM copy of OAM during
// game logic and copied to the real OAM at the start of the next VBlank.
static u16 shadow_oam[512];

static void set_obj(u32 i, u16 a0, u16 a1, u16 a2) {
    shadow_oam[i * 4 + 0] = a0;
    shadow_oam[i * 4 + 1] = a1;
    shadow_oam[i * 4 + 2] = a2;
}
static void set_affine(u32 set, s32 pa, s32 pb, s32 pc, s32 pd) {
    shadow_oam[set * 16 + 3] = (u16)pa;
    shadow_oam[set * 16 + 7] = (u16)pb;
    shadow_oam[set * 16 + 11] = (u16)pc;
    shadow_oam[set * 16 + 15] = (u16)pd;
}
static void upload_oam(void) {
    for (u32 i = 0; i < 512; ++i) OAM[i] = shadow_oam[i];
}

// Entity table at a fixed EWRAM address, read by shadow/games/shadowtest.py.
struct Entity {
    u8 active;
    u8 kind;        // 1 = player
    u8 facing;      // 1 down, 2 up, 3 left, 4 right (Pokemon's DIR_* values)
    u8 oam_slot;
    s16 x, y;       // top-left of the sprite in screen pixels
    u16 width, height;
};
#define ENTITIES ((volatile struct Entity*)0x02000000)

// attr helpers
#define A0(y, aff, dbl_or_hide, mode, bpp8, shape) \
    ((u16)(((y) & 255) | ((aff) << 8) | ((dbl_or_hide) << 9) | ((mode) << 10) | ((bpp8) << 13) | ((shape) << 14)))
#define A1(x, hf, vf, size) ((u16)(((x) & 511) | ((hf) << 12) | ((vf) << 13) | ((size) << 14)))
#define A1AFF(x, set, size) ((u16)(((x) & 511) | ((set) << 9) | ((size) << 14)))
#define A2(tile, prio, pal) ((u16)((tile) | ((prio) << 10) | ((pal) << 12)))

static void update_sprites(int scene, s32 t) {
    for (u32 i = 0; i < 128; ++i) set_obj(i, A0(160, 0, 0, 0, 0, 0), 0, 0);

    set_affine(0, icos(t * 2), -isin(t * 2), isin(t * 2), icos(t * 2));
    s32 sc = 128 + (isin(t * 3) + 256) / 2;
    set_affine(1, sc, isin(t) / 8, 0, sc);

    // 0: 32x32 "player" moving across the left edge (exercises 9-bit X wrap)
    s32 px = (t * 2) % 300 - 30;
    set_obj(0, A0(20, 0, 0, 0, 0, 0), A1(px, 0, 0, 2), A2(512, 0, 1));
    ENTITIES[0].active = 1;
    ENTITIES[0].kind = 1;
    ENTITIES[0].facing = 4;
    ENTITIES[0].oam_slot = 0;
    ENTITIES[0].x = px;
    ENTITIES[0].y = 20;
    ENTITIES[0].width = 32;
    ENTITIES[0].height = 32;
    // 1: 16x16 hflip moving through the top edge (8-bit Y wrap)
    set_obj(1, A0(t % 200 - 20, 0, 0, 0, 0, 0), A1(100, 1, 0, 1), A2(520, 1, 2));
    // 2: 64x32 vflip
    set_obj(2, A0(100, 0, 0, 0, 0, 1), A1(150, 0, 1, 3), A2(530, 2, 3));
    // 3: 8x32 h+v flip
    set_obj(3, A0(90, 0, 0, 0, 0, 2), A1(20, 1, 1, 1), A2(560, 3, 4));
    // 4: 8bpp 32x16
    set_obj(4, A0(60 + isin(t * 4) / 16, 0, 0, 0, 1, 1), A1(60, 0, 0, 2), A2(768, 1, 0));
    // 5: affine 32x32 rotating
    set_obj(5, A0(30, 1, 0, 0, 0, 0), A1AFF(180, 0, 2), A2(512, 0, 0));
    // 6: affine double-size 16x16 (32x32 box), scaling + shear
    set_obj(6, A0(110, 1, 1, 0, 0, 0), A1AFF(130, 1, 1), A2(600, 1, 5));
    // 7: semi-transparent 32x32 overlapping others
    set_obj(7, A0(100, 0, 0, 1, 0, 0), A1(70 + isin(t * 2) / 6, 0, 0, 2), A2(640, 0, 6));
    // 8: OBJ-window sprite 64x64 (only shown as a window in scene 1)
    set_obj(8, A0(40, 0, 0, 2, 0, 0), A1(90 + isin(t) / 8, 0, 0, 3), A2(512, 0, 0));
    // 9: disabled (non-affine with bit 9 set) -> must not appear
    set_obj(9, A0(70, 0, 1, 0, 0, 0), A1(120, 0, 0, 2), A2(512, 0, 7));
    // 10: 16x8 using low OBJ tiles (invisible in bitmap modes 3-5)
    set_obj(10, A0(140, 0, 0, 0, 0, 1), A1(200, 0, 0, 0), A2(4, 0, 8));
    // 11: 64x64 hanging off bottom-right
    set_obj(11, A0(130, 0, 0, 0, 0, 0), A1(210, 0, 0, 3), A2(512, 2, 9));
    // 12: same priority as 0, overlapping it (OAM order decides)
    set_obj(12, A0(28, 0, 0, 0, 0, 0), A1((t * 2) % 300 - 22, 0, 0, 2), A2(576, 0, 10));
    (void)scene;
}

// ---------------------------------------------------------------- scenes

static void setup_scene(int s) {
    REG_DISPCNT = DC_BLANK;
    REG_MOSAIC = 0;
    load_palettes();
    vram_fill(0, 0x18000, 0);
    load_obj_tiles();
    if (s <= 3) load_tile_bg_data();
    else if (s == 4) draw_mode3();
    else if (s == 5) draw_mode4();
    else draw_mode5();
}

static void set_affine_bg(int n, s32 angle, s32 scale, s32 cx, s32 cy, s32 sx, s32 sy) {
    // scale: 8.8 texture pixels per screen pixel. Maps screen (sx,sy) to texture (cx,cy).
    s32 pa = icos(angle) * scale >> 8, pb = -isin(angle) * scale >> 8;
    s32 pc = isin(angle) * scale >> 8, pd = icos(angle) * scale >> 8;
    REG_BGPA(n) = (u16)pa; REG_BGPB(n) = (u16)pb;
    REG_BGPC(n) = (u16)pc; REG_BGPD(n) = (u16)pd;
    REG_BGX(n) = (u32)(cx * 256 - (pa * sx + pb * sy));
    REG_BGY(n) = (u32)(cy * 256 - (pc * sx + pd * sy));
}

static void update_scene(int s, s32 t) {
    switch (s) {
    case 0:  // mode 0, 1D mapping, 4 text BGs, WIN0, alpha blend
        REG_DISPCNT = DC_MODE(0) | DC_BG(0) | DC_BG(1) | DC_BG(2) | DC_BG(3) | DC_OBJ | DC_OBJ_1D | DC_WIN0;
        REG_BGCNT(0) = BGCNT(2, 0, 24, 0, 0, 0);
        REG_BGCNT(1) = BGCNT(1, 1, 25, 1, 1, 0);
        REG_BGCNT(2) = BGCNT(3, 0, 27, 0, 2, 0);
        REG_BGCNT(3) = BGCNT(0, 0, 29, 0, 0, 0);
        REG_BGHOFS(0) = t; REG_BGVOFS(0) = t / 2;
        REG_BGHOFS(1) = -t * 2; REG_BGVOFS(1) = t;
        REG_BGHOFS(2) = t / 3; REG_BGVOFS(2) = -t;
        REG_BGHOFS(3) = 0; REG_BGVOFS(3) = t;
        REG_WIN0H = ((40 + t % 80) << 8) | (120 + t % 80);
        REG_WIN0V = (30 << 8) | 100;
        REG_WININ = 0x01 | 0x04 | 0x10 | 0x20;
        REG_WINOUT = 0x3F;
        REG_BLDCNT = 0x08 | (1 << 6) | ((0x01 | 0x02 | 0x20) << 8);
        REG_BLDALPHA = 10 | (6 << 8);
        break;
    case 1:  // mode 0, 2D mapping, OBJ window + WIN1, darken
        REG_DISPCNT = DC_MODE(0) | DC_BG(0) | DC_BG(1) | DC_BG(3) | DC_OBJ | DC_WIN1 | DC_OBJWIN;
        REG_BGCNT(0) = BGCNT(1, 0, 24, 0, 0, 0);
        REG_BGCNT(1) = BGCNT(2, 1, 25, 1, 1, 0);
        REG_BGCNT(3) = BGCNT(3, 0, 29, 0, 0, 0);
        REG_BGHOFS(0) = -t; REG_BGVOFS(0) = t * 3;
        REG_BGHOFS(1) = t; REG_BGVOFS(1) = 0;
        REG_BGHOFS(3) = t * 2; REG_BGVOFS(3) = t;
        REG_WIN1H = (10 << 8) | 90;
        REG_WIN1V = ((20 + t % 60) << 8) | (80 + t % 60);
        REG_WININ = (0x02 | 0x10 | 0x20) << 8;
        REG_WINOUT = 0x3F | ((0x01 | 0x20) << 8);
        REG_BLDCNT = 0x02 | 0x10 | (3 << 6);
        REG_BLDY = 8;
        break;
    case 2:  // mode 1, BG2 affine rotating with wrap, brighten fade
        REG_DISPCNT = DC_MODE(1) | DC_BG(0) | DC_BG(1) | DC_BG(2) | DC_OBJ | DC_OBJ_1D;
        REG_BGCNT(0) = BGCNT(1, 0, 24, 0, 0, 0);
        REG_BGCNT(1) = BGCNT(3, 1, 25, 1, 1, 0);
        REG_BGCNT(2) = BGCNT(2, 2, 30, 1, 1, 1);
        REG_BGHOFS(0) = t; REG_BGVOFS(0) = 0;
        REG_BGHOFS(1) = 0; REG_BGVOFS(1) = t;
        set_affine_bg(2, t, 200 + isin(t * 2) / 2, 128, 128, 120, 80);
        REG_BLDCNT = 0x04 | 0x10 | (2 << 6);
        REG_BLDY = (t / 8) % 17;
        break;
    case 3:  // mode 2, two affine BGs (wrap / no wrap), semi-transparent OBJ blend
        REG_DISPCNT = DC_MODE(2) | DC_BG(2) | DC_BG(3) | DC_OBJ | DC_OBJ_1D;
        REG_BGCNT(2) = BGCNT(3, 2, 30, 1, 1, 1);
        REG_BGCNT(3) = BGCNT(1, 2, 31, 1, 0, 0);
        set_affine_bg(2, -t, 256, 128, 128, 120, 80);
        set_affine_bg(3, t / 2, 160, 64, 64, 60 + t / 4, 80);
        REG_BLDCNT = (0x04 | 0x08) << 8;
        REG_BLDALPHA = 8 | (8 << 8);
        break;
    case 4:  // mode 3 bitmap
        REG_DISPCNT = DC_MODE(3) | DC_BG(2) | DC_OBJ | DC_OBJ_1D;
        REG_BGCNT(2) = BGCNT(1, 0, 0, 0, 0, 0);
        set_affine_bg(2, 0, 256, 0, 0, 0, 0);
        {   // a small moving rectangle redrawn every frame
            volatile u16* fb = VRAM;
            s32 rx = (t * 3) % 200, ry = 40 + (t % 60);
            for (s32 y = ry; y < ry + 12; ++y)
                for (s32 x = rx; x < rx + 20; ++x) fb[y * 240 + x] = RGB15(31, t & 31, 0);
        }
        REG_BLDCNT = 0x04 << 8;
        REG_BLDALPHA = 12 | (4 << 8);
        break;
    case 5:  // mode 4 paletted bitmap with page flipping + WIN0
        REG_DISPCNT = DC_MODE(4) | DC_BG(2) | DC_OBJ | DC_OBJ_1D | DC_WIN0 | (((t / 30) & 1) ? DC_PAGE : 0);
        REG_BGCNT(2) = BGCNT(2, 0, 0, 0, 0, 0);
        set_affine_bg(2, 0, 256, 0, 0, 0, 0);
        REG_WIN0H = (60 << 8) | 180;
        REG_WIN0V = ((10 + t % 50) << 8) | (70 + t % 50);
        REG_WININ = 0x10;
        REG_WINOUT = 0x3F;
        REG_BLDCNT = 0;
        break;
    default:  // mode 5 small bitmap scaled to fill the screen, slight rotation
        REG_DISPCNT = DC_MODE(5) | DC_BG(2) | DC_OBJ | DC_OBJ_1D | (((t / 40) & 1) ? DC_PAGE : 0);
        REG_BGCNT(2) = BGCNT(0, 0, 0, 0, 0, 0);
        REG_BGPA(2) = 170; REG_BGPB(2) = isin(t) / 16;
        REG_BGPC(2) = 0;   REG_BGPD(2) = 204;
        REG_BGX(2) = 0; REG_BGY(2) = 0;
        REG_BLDCNT = 0x04 | (3 << 6);
        REG_BLDY = (t / 10) % 17;
        break;
    }
}

int main(void) {
    u32 frame = 0;
    int scene = -1;
    for (;;) {
        int s = (frame / SCENE_LEN) % NSCENES;
        s32 t = frame % SCENE_LEN;
        if (s != scene) {
            vsync();
            setup_scene(s);       // takes several frames; display stays blanked
            scene = s;
        }
        update_sprites(s, t);     // game logic: entity table + shadow OAM for frame t
        vsync();
        upload_oam();             // VBlank: show what the logic built
        update_scene(s, t);
        ++frame;
    }
}

// gcc may emit calls to these even with -ffreestanding.
void* memset(void* d, int c, unsigned n) {
    u8* p = d;
    while (n--) *p++ = (u8)c;
    return d;
}
void* memcpy(void* d, const void* s, unsigned n) {
    u8* p = d;
    const u8* q = s;
    while (n--) *p++ = *q++;
    return d;
}
