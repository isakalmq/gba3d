// gbahost: a flat C API over libmgba so a host program (Python via ctypes,
// later a 3D engine) can own the emulator and step it one frame at a time.
//
// The host calls gh_set_keys() then gh_run_frame() and reads memory between
// frames, synchronously, with no sockets or threads involved. Memory is
// exposed as raw pointers to mGBA's own buffers (gh_block), so reading the
// game's state each frame costs nothing.
#include <mgba/flags.h>
#include <mgba/core/core.h>
#include <mgba/core/log.h>
#include <mgba/core/interface.h>
#include <mgba-util/vfs.h>

#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct gh {
    struct mCore* core;
    color_t* video;
    unsigned width, height;
};

static void quiet_log(struct mLogger* logger, int category, enum mLogLevel level, const char* fmt, va_list args) {
    (void) logger;
    (void) category;
    if (!(level & (mLOG_FATAL | mLOG_ERROR))) {
        return;
    }
    fputs("[mgba] ", stderr);
    vfprintf(stderr, fmt, args);
    fputc('\n', stderr);
}

static struct mLogger logger = { .log = quiet_log };

// Open `rom`. If `save` is non-NULL, battery saves are read from and written to
// that file (created if missing). Returns NULL on failure.
struct gh* gh_open(const char* rom, const char* save) {
    mLogSetDefaultLogger(&logger);
    struct mCore* core = mCoreFind(rom);
    if (!core || !core->init(core)) {
        return NULL;
    }
    mCoreInitConfig(core, NULL);

    struct gh* h = calloc(1, sizeof(*h));
    h->core = core;
    core->desiredVideoDimensions(core, &h->width, &h->height);
    h->video = calloc((size_t) h->width * h->height, sizeof(color_t));
    core->setVideoBuffer(core, h->video, h->width);
    core->setAudioBufferSize(core, 2048);

    if (!mCoreLoadFile(core, rom)) {
        core->deinit(core);
        free(h->video);
        free(h);
        return NULL;
    }
    if (save) {
        struct VFile* vf = VFileOpen(save, O_CREAT | O_RDWR);
        if (vf) {
            core->loadSave(core, vf);
        }
    }
    core->reset(core);
    return h;
}

void gh_close(struct gh* h) {
    if (!h) {
        return;
    }
    h->core->deinit(h->core);
    free(h->video);
    free(h);
}

void gh_reset(struct gh* h) { h->core->reset(h->core); }

// GBA key bits: A=0 B=1 Select=2 Start=3 Right=4 Left=5 Up=6 Down=7 R=8 L=9.
void gh_set_keys(struct gh* h, uint32_t keys) { h->core->setKeys(h->core, keys); }

void gh_run_frame(struct gh* h) { h->core->runFrame(h->core); }

uint32_t gh_frame(struct gh* h) { return h->core->frameCounter(h->core); }

// The last rendered frame, width*height pixels of 32-bit XBGR (R in the low byte).
const color_t* gh_video(struct gh* h, unsigned* width, unsigned* height) {
    *width = h->width;
    *height = h->height;
    return h->video;
}

int gh_color_bits(void) { return (int) (sizeof(color_t) * 8); }

// Pointer to one of mGBA's memory buffers by internal name ("wram", "iwram",
// "palette", "vram", "oam", "cart0", ...). Returns NULL if unknown.
void* gh_block(struct gh* h, const char* name, size_t* size, uint32_t* start) {
    const struct mCoreMemoryBlock* blocks;
    size_t n = h->core->listMemoryBlocks(h->core, &blocks);
    for (size_t i = 0; i < n; ++i) {
        if (strcmp(blocks[i].internalName, name) == 0) {
            *start = blocks[i].start;
            return h->core->getMemoryBlock(h->core, blocks[i].id, size);
        }
    }
    return NULL;
}

// Side-effect-free reads (debugger view of memory), for I/O registers.
uint32_t gh_raw_read16(struct gh* h, uint32_t address) { return h->core->rawRead16(h->core, address, -1); }

void gh_raw_write8(struct gh* h, uint32_t address, uint8_t value) { h->core->rawWrite8(h->core, address, -1, value); }

size_t gh_state_size(struct gh* h) { return h->core->stateSize(h->core); }

int gh_save_state(struct gh* h, void* buffer) { return h->core->saveState(h->core, buffer); }

int gh_load_state(struct gh* h, const void* buffer) { return h->core->loadState(h->core, buffer); }
