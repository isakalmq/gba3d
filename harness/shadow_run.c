// Headless mGBA runner: loads a ROM, attaches a Lua script (with the same
// `emu`, `callbacks`, `socket` and `console` globals the Qt frontend gives
// scripts), and runs a fixed number of frames as fast as possible.
//
// Used for automated testing of shadow_bridge.lua and for batch captures.
//
//   shadow-run ROM SCRIPT FRAMES
#include <mgba/flags.h>
#include <mgba/core/core.h>
#include <mgba/core/log.h>
#include <mgba/core/scripting.h>
#include <mgba/script/context.h>
#include <mgba/internal/script/lua.h>
#include <mgba-util/vfs.h>

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

static void log_cb(struct mLogger* logger, int category, enum mLogLevel level, const char* fmt, va_list args) {
    (void) logger;
    if (!(level & (mLOG_FATAL | mLOG_ERROR | mLOG_WARN | mLOG_INFO))) {
        return;
    }
    if (level & mLOG_WARN && category != mLogCategoryById("script")) {
        return;  // core stub warnings are noise here
    }
    fprintf(stderr, "[%s] ", mLogCategoryName(category));
    vfprintf(stderr, fmt, args);
    fputc('\n', stderr);
}

static void frame_ended(void* ctx) {
    mScriptContextTriggerCallback(ctx, "frame");
}

int main(int argc, char** argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s ROM SCRIPT FRAMES\n", argv[0]);
        return 2;
    }
    const char* rom = argv[1];
    const char* script = argv[2];
    long frames = strtol(argv[3], NULL, 10);

    struct mLogger logger = { .log = log_cb };
    mLogSetDefaultLogger(&logger);

    struct mCore* core = mCoreFind(rom);
    if (!core || !core->init(core)) {
        fprintf(stderr, "could not create a core for %s\n", rom);
        return 1;
    }
    mCoreInitConfig(core, NULL);

    unsigned width, height;
    core->desiredVideoDimensions(core, &width, &height);
    color_t* buffer = calloc((size_t) width * height, sizeof(color_t));
    core->setVideoBuffer(core, buffer, width);

    if (!mCoreLoadFile(core, rom)) {
        fprintf(stderr, "could not load %s\n", rom);
        return 1;
    }
    core->reset(core);

    struct mScriptContext context;
    mScriptContextInit(&context);
    mScriptContextAttachStdlib(&context);
    mScriptContextAttachSocket(&context);
    mScriptContextAttachCore(&context, core);
    mScriptContextAttachLogger(&context, &logger);
    struct mScriptEngineContext* lua = mScriptContextRegisterEngine(&context, mSCRIPT_ENGINE_LUA);

    struct mCoreCallbacks callbacks = { .context = &context, .videoFrameEnded = frame_ended };
    core->addCoreCallbacks(core, &callbacks);

    struct VFile* vf = VFileOpen(script, O_RDONLY);
    if (!vf) {
        fprintf(stderr, "could not open script %s\n", script);
        return 1;
    }
    if (!lua->load(lua, script, vf) || !lua->run(lua)) {
        fprintf(stderr, "script error: %s\n", lua->getError(lua));
        return 1;
    }
    vf->close(vf);
    mScriptContextTriggerCallback(&context, "start");

    for (long i = 0; i < frames; ++i) {
        core->runFrame(core);
    }

    mScriptContextTriggerCallback(&context, "shutdown");
    mScriptContextDeinit(&context);
    core->deinit(core);
    free(buffer);
    return 0;
}
